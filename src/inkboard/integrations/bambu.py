"""Bambu P1S local MQTT monitoring and opt-in LAN Developer Mode controls.

This module does not log into Bambu Cloud, emulate a signed first-party client,
extract proprietary keys, start a print, or change a printer's network mode.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import socket
import ssl
import struct
import sys
import threading
import time
import uuid
from .common import Adapter, IntegrationError, integer, number, secret, text, utc_now


MAX_REPORT = 262144
MAX_FRAME = 1_048_576
STATES = {"RUNNING": "printing", "PAUSE": "paused", "FINISH": "finished", "FAILED": "failed",
          "IDLE": "idle", "PREPARE": "preparing", "SLICING": "preparing", "INIT": "idle"}


def tls_settings(config):
    """Use explicit certificate pinning or a user-provided CA plus serial CN."""
    fingerprint = config.get("tls_fingerprint", "")
    ca_file = config.get("tls_ca_file")
    if fingerprint:
        fingerprint = str(fingerprint).lower().replace(":", "")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise IntegrationError("tls_fingerprint must be a SHA256 certificate fingerprint.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        # CERT_NONE here is paired with a mandatory exact pin check *before*
        # the camera auth or MQTT CONNECT containing an access code is sent.
        return context, fingerprint
    if not isinstance(ca_file, str) or not ca_file:
        raise IntegrationError("Configure tls_fingerprint or tls_ca_file for each printer.")
    try:
        context = ssl.create_default_context(cafile=ca_file)
    except (OSError, ssl.SSLError):
        raise IntegrationError("The printer CA file could not be loaded.") from None
    context.check_hostname = False
    if sys.version_info >= (3, 13) and hasattr(ssl, "VERIFY_X509_STRICT"):
        # Some printer CA certificates predate Python's strict extension policy.
        # Keep chain validation and the exact serial identity below.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context, None


def verify_peer(stream, fingerprint, serial):
    if fingerprint:
        actual = hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, fingerprint):
            raise IntegrationError("The printer TLS certificate does not match its configured fingerprint.")
    else:
        certificate = stream.getpeercert()
        names = [value for group in certificate.get("subject", ()) for key, value in group if key == "commonName"]
        if names != [serial]:
            raise IntegrationError("The printer certificate identity does not match its configured serial.")


def auth_packet(access_code):
    try:
        code = access_code.encode("ascii")
    except UnicodeError:
        raise IntegrationError("The camera access code must be ASCII.") from None
    if not 1 <= len(code) <= 32:
        raise IntegrationError("The camera access code must be at most 32 bytes.")
    return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + code.ljust(32, b"\0")


def read_exact(stream, length, deadline):
    result = bytearray()
    while len(result) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Camera frame deadline exceeded")
        stream.settimeout(remaining)
        chunk = stream.recv(length - len(result))
        if not chunk:
            raise IntegrationError("The camera closed its connection.")
        result.extend(chunk)
    return bytes(result)


def validate_jpeg(frame):
    if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
        raise IntegrationError("The camera did not return a complete JPEG.")
    try:
        from PIL import Image
    except ImportError:
        raise IntegrationError("Install the dashboard's Pillow dependency to validate printer snapshots.") from None
    try:
        with Image.open(io.BytesIO(frame)) as image:
            if image.format != "JPEG" or not 1 <= image.width <= 4096 or not 1 <= image.height <= 4096 or image.width * image.height > 12_000_000:
                raise IntegrationError("The camera image dimensions exceed the safety limit.")
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise IntegrationError("The camera JPEG could not be validated.") from None
    return frame


class PrinterSession:
    def __init__(self, config, secrets, controls):
        self.config = config
        self.id = config.get("id")
        self.name = config.get("name", self.id)
        self.host = config.get("host")
        self.serial = config.get("serial")
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.id):
            raise IntegrationError("Each printer needs a lowercase id containing letters, digits or hyphens.")
        if not isinstance(self.host, str) or not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", self.host):
            raise IntegrationError("Configure a printer IP address or hostname, without a URL.")
        if not isinstance(self.serial, str) or not re.fullmatch(r"[A-Za-z0-9]{6,32}", self.serial):
            raise IntegrationError("Configure the printer's exact serial number.")
        self.code = secret(secrets, config.get("access_code_secret"))
        auth_packet(self.code)
        self.context, self.fingerprint = tls_settings(config)
        self.mqtt_port = integer(config.get("mqtt_port", 8883), 1, 65535)
        self.camera_port = integer(config.get("camera_port", 6000), 1, 65535)
        self.timeout = config.get("timeout_seconds", 4)
        if type(self.timeout) not in (int, float) or not 0.25 <= self.timeout <= 10:
            raise IntegrationError("Printer timeout_seconds must be between 0.25 and 10.")
        self.camera_enabled = config.get("camera_enabled", False) is True
        self.mode = config.get("mode", "cloud")
        if self.mode not in ("cloud", "lan_developer"):
            raise IntegrationError("Printer mode must be cloud or lan_developer.")
        self.controls = controls and config.get("controls_enabled", True) is True and self.mode == "lan_developer"
        self.request_topic = "device/" + self.serial + "/request"
        self.report_topic = "device/" + self.serial + "/report"
        self.client = None
        self._start_lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._condition = threading.Condition()
        self._camera_lock = threading.Lock()
        self._frame = None
        self._connected = False
        self._subscribed = False
        self._closed = False
        self._fields = {}
        self._field_times = {}
        self._observed_at = None
        self._last_report = 0
        self._last_report_wall = 0
        self._last_pushall = 0
        self._error = "Waiting for the first authenticated printer report."
        self._pending = None
        self._run_id = uuid.uuid4().hex
        self._job_sequence = 0
        self._job_active = False
        self._job_signature = {}

    def start(self):
        with self._start_lock:
            if self.client is not None or self._closed:
                return
            try:
                import paho.mqtt.client as mqtt
            except ImportError:
                self._error = "Install the bambu extra to enable local MQTT monitoring."
                return
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                 client_id="inkboard-" + uuid.uuid4().hex[:20], clean_session=True,
                                 protocol=mqtt.MQTTv311, reconnect_on_failure=True)
            client.username_pw_set("bblp", self.code)
            client.tls_set_context(self.context)
            # The IP cannot match the serial CN. Our socket-open hook checks the
            # exact pin or CA-validated serial before paho sends MQTT CONNECT.
            client.tls_insecure_set(True)
            client.connect_timeout = self.timeout
            client.reconnect_delay_set(min_delay=2, max_delay=60)
            client.max_inflight_messages_set(1)
            client.max_queued_messages_set(1)
            client.on_socket_open = self._socket_open
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_subscribe = self._on_subscribe
            client.on_message = self._on_message
            client.suppress_exceptions = True
            self.client = client
            client.connect_async(self.host, self.mqtt_port, keepalive=30)
            client.loop_start()

    def _socket_open(self, client, userdata, stream):
        try:
            verify_peer(stream, self.fingerprint, self.serial)
        except Exception:
            stream.close()
            self._error = "The printer's TLS identity could not be verified. Check its configured certificate."
            raise

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            with self._condition:
                self._connected = False
                self._error = "The printer rejected its local credential or authorization."
            return
        # Reconnects must not make an old cached full report current. Forget
        # fields and require a fresh bootstrap before controls become eligible.
        with self._condition:
            self._connected = True
            self._subscribed = False
            self._fields.clear()
            self._field_times.clear()
            self._run_id = uuid.uuid4().hex
            self._job_sequence = 0
            self._job_active = False
            self._job_signature.clear()
            self._last_report = self._last_report_wall = 0
            self._error = "Connected; waiting for a new printer report."
        client.subscribe(self.report_topic, qos=0)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if any(getattr(reason, "is_failure", False) for reason in reason_codes):
            self._error = "The printer denied its status subscription."
            client.disconnect()
            return
        self._subscribed = True
        self.request_full_status()

    def request_full_status(self):
        if not self._connected or not self._subscribed or self.client is None:
            return
        now = time.monotonic()
        if now - self._last_pushall >= 300 or not self._last_pushall:
            self._last_pushall = now
            self.client.publish(self.request_topic, json.dumps({"pushing": {"sequence_id": "0", "command": "pushall",
                           "version": 1, "push_target": 1}}, separators=(",", ":")), qos=0, retain=False)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self._condition:
            self._connected = False
            self._subscribed = False
            self._error = "Printer disconnected; monitoring will reconnect with backoff."
            self._condition.notify_all()

    def _on_message(self, client, userdata, message):
        if message.topic != self.report_topic or message.retain or len(message.payload) > MAX_REPORT:
            return
        try:
            document = json.loads(message.payload)
        except (ValueError, UnicodeError):
            return
        if not isinstance(document, dict):
            return
        fields = document.get("print")
        now, wall = time.monotonic(), time.time()
        with self._condition:
            if isinstance(fields, dict):
                state = fields.get("gcode_state")
                signature = {key: str(fields[key]) for key in ("task_id", "subtask_id", "gcode_file")
                             if key in fields and isinstance(fields[key], (int, str))
                             and not isinstance(fields[key], bool) and len(str(fields[key])) <= 128}
                changed = any(key in self._job_signature and self._job_signature[key] != value
                              for key, value in signature.items())
                if state in ("RUNNING", "PREPARE", "PAUSE", "SLICING") or (changed and self._job_active):
                    if not self._job_active or changed:
                        self._job_sequence += 1
                    self._job_active = True
                    self._job_signature.update(signature)
                elif state in ("IDLE", "FINISH", "FAILED"):
                    self._job_active = False
                    self._job_signature.update(signature)
                selected = {}
                if isinstance(fields.get("gcode_state"), str) and fields["gcode_state"] in STATES:
                    selected["gcode_state"] = fields["gcode_state"]
                for key in ("mc_percent", "mc_remaining_time", "layer_num", "total_layer_num", "nozzle_temper",
                            "bed_temper", "spd_lvl", "print_error"):
                    if key in fields and number(fields[key]) is not None:
                        selected[key] = fields[key]
                if isinstance(fields.get("subtask_name"), str):
                    selected["subtask_name"] = text(fields["subtask_name"], 120)
                if isinstance(fields.get("lights_report"), list):
                    lights = [{"node": "chamber_light", "mode": row["mode"]}
                              for row in fields["lights_report"][:16] if isinstance(row, dict)
                              and row.get("node") == "chamber_light" and row.get("mode") in ("on", "off")]
                    if lights:
                        selected["lights_report"] = lights
                if selected:
                    self._fields.update(selected)
                    self._field_times.update({key: now for key in selected})
                    self._last_report, self._last_report_wall = now, wall
                    self._observed_at, self._error = utc_now(), ""
            pending = self._pending
            if pending:
                response = document.get(pending["root"])
                if (isinstance(response, dict) and str(response.get("sequence_id", "")) == pending["sequence"]
                        and response.get("command") == pending["command"]):
                    outcome = str(response.get("result", "")).lower()
                    if outcome in ("fail", "failed", "error") or response.get("reason"):
                        pending["result"] = "rejected"
                    elif outcome == "success":
                        pending["accepted"] = True
                if pending["result"] != "rejected" and isinstance(fields, dict) and self._effect(pending, fields):
                    pending["result"] = "confirmed"
                self._condition.notify_all()

    @staticmethod
    def _effect(pending, fields):
        action, value = pending["action"], pending["value"]
        if action == "pause":
            return fields.get("gcode_state") == "PAUSE"
        if action == "resume":
            return fields.get("gcode_state") == "RUNNING"
        if action == "stop":
            return fields.get("gcode_state") in ("IDLE", "FINISH", "FAILED")
        if action == "speed":
            return type(fields.get("spd_lvl")) is int and fields["spd_lvl"] == value
        if action == "light" and isinstance(fields.get("lights_report"), list):
            return any(isinstance(row, dict) and row.get("node") == "chamber_light" and row.get("mode") == value
                       for row in fields["lights_report"][:16])
        return False

    def _fresh(self):
        return (self._connected and self._last_report > 0 and 0 <= time.monotonic() - self._last_report <= 60
                and -5 <= time.time() - self._last_report_wall <= 60)

    def snapshot(self):
        with self._condition:
            fresh = self._fresh()
            fields = self._fields
            state = STATES.get(fields.get("gcode_state"), "unknown")
            # A P1S streams deltas, so an unchanged gcode_state need not appear
            # in every temperature report. Retain it for one full-status cycle
            # within the same authenticated session; reconnect clears all state.
            state_fresh = 0 <= time.monotonic() - self._field_times.get("gcode_state", -999) <= 330
            light = None
            for row in fields.get("lights_report", [])[:16] if isinstance(fields.get("lights_report"), list) else []:
                if isinstance(row, dict) and row.get("node") == "chamber_light" and row.get("mode") in ("on", "off"):
                    light = row["mode"]
            capabilities = []
            if self.controls and fresh and state_fresh:
                capabilities = ["light"]
                if state == "printing":
                    capabilities += ["pause", "stop", "speed"]
                elif state == "paused":
                    capabilities += ["resume", "stop"]
            hint = self._error or ("Controls require an explicitly configured LAN Developer Mode printer."
                    if not self.controls else "")
            return {"id": self.id, "name": text(self.name, 100), "available": fresh,
                    "state": state if fresh else "unknown", "observed_at": self._observed_at,
                    "job_identity": f"run:{self._run_id}:task:{self._job_sequence}" if self._job_sequence else None,
                    "job": text(fields.get("subtask_name"), 120), "progress": number(fields.get("mc_percent"), 0, 100),
                    "remaining_minutes": number(fields.get("mc_remaining_time")), "layer": number(fields.get("layer_num")),
                    "total_layers": number(fields.get("total_layer_num")), "nozzle_c": number(fields.get("nozzle_temper"), 0, 400),
                    "bed_c": number(fields.get("bed_temper"), 0, 150), "speed": number(fields.get("spd_lvl"), 1, 4),
                    "light": light, "error_code": number(fields.get("print_error")), "capabilities": capabilities,
                    "camera_available": self.camera_enabled, "control_hint": hint}

    def command(self, action, parameters):
        if not self.controls:
            raise IntegrationError("Direct controls are disabled. Cloud-mode telemetry does not imply control authorization.")
        allowed = {"pause", "resume", "stop", "speed", "light"}
        if action not in allowed or set(parameters) - {"printer_id", "job_identity", "value", "confirm"}:
            raise IntegrationError("Unknown printer action or parameter.")
        value = parameters.get("value")
        if action == "stop" and (parameters.get("confirm") != "STOP" or value is not None):
            raise IntegrationError("Stopping a print requires explicit STOP confirmation.")
        if action in ("pause", "resume") and (value is not None or "confirm" in parameters):
            raise IntegrationError("This action does not take a value or confirmation.")
        if action == "speed":
            integer(value, 1, 4)
        if action == "light" and value not in ("on", "off"):
            raise IntegrationError("Light must be on or off.")
        if not self._command_lock.acquire(blocking=False):
            raise IntegrationError("A command for this printer is already awaiting feedback.")
        try:
            with self._condition:
                row = self.snapshot()
                if action not in row["capabilities"] or self.client is None:
                    raise IntegrationError("A fresh, eligible printer state is required before sending this action.")
                if action != "light" and (not row["job_identity"] or parameters.get("job_identity") != row["job_identity"]):
                    raise IntegrationError("The active print changed. Refresh and select its current controls.")
                sequence = str(int(uuid.uuid4().hex[:7], 16))
                if action in ("pause", "resume", "stop"):
                    root, payload = "print", {"sequence_id": sequence, "command": action, "param": ""}
                elif action == "speed":
                    root, payload = "print", {"sequence_id": sequence, "command": "print_speed", "param": str(value)}
                else:
                    root, payload = "system", {"sequence_id": sequence, "command": "ledctrl", "led_node": "chamber_light",
                        "led_mode": value, "led_on_time": 500, "led_off_time": 500, "loop_times": 1, "interval_time": 1000}
                pending = {"root": root, "sequence": sequence, "command": payload["command"],
                           "action": action, "value": value, "accepted": False, "result": None}
                self._pending = pending
                # QoS0 plus no disconnected command queue prevents transport
                # reconnects from replaying a physical action automatically.
                result = self.client.publish(self.request_topic, json.dumps({root: payload}, separators=(",", ":")), qos=0, retain=False)
                if result.rc != 0:
                    return {"status": "uncertain", "message": "The printer connection changed while sending. Inspect its actual state before trying again."}
                deadline = time.monotonic() + min(3, self.timeout)
                while pending["result"] is None and self._connected and time.monotonic() < deadline:
                    self._condition.wait(max(0, deadline - time.monotonic()))
                if pending["result"] == "confirmed":
                    return {"status": "confirmed", "message": "A new printer report confirms the requested state."}
                if pending["result"] == "rejected":
                    return {"status": "rejected", "message": "The printer rejected this command. Check its authorization mode and screen."}
                if pending["accepted"]:
                    return {"status": "sent", "message": "The printer accepted the command; its actual state has not confirmed completion yet."}
                return {"status": "uncertain", "message": "The command was sent without conclusive feedback. Inspect the actual state before trying again."}
        finally:
            with self._condition:
                self._pending = None
            self._command_lock.release()

    def camera_bytes(self):
        if not self.camera_enabled:
            raise IntegrationError("The camera is disabled in this printer's configuration.")
        if not self._camera_lock.acquire(blocking=False):
            raise IntegrationError("A camera capture is already in progress.")
        try:
            if self._frame and 0 <= time.monotonic() - self._frame[2] < 10 and -5 <= time.time() - self._frame[3] < 10:
                return self._frame[0], self._frame[1]
            deadline = time.monotonic() + self.timeout
            try:
                with socket.create_connection((self.host, self.camera_port), timeout=self.timeout) as plain:
                    plain.settimeout(max(0.01, deadline - time.monotonic()))
                    with self.context.wrap_socket(plain, server_hostname=self.serial) as stream:
                        verify_peer(stream, self.fingerprint, self.serial)
                        # Credentials follow certificate identity verification.
                        stream.sendall(auth_packet(self.code))
                        header = read_exact(stream, 16, deadline)
                        length, track, flags, reserved = struct.unpack("<IIII", header)
                        if not 100 <= length <= MAX_FRAME or (track, flags, reserved) != (0, 1, 0):
                            raise IntegrationError("The camera returned an invalid frame header.")
                        frame = validate_jpeg(read_exact(stream, length, deadline))
            except (OSError, TimeoutError):
                raise IntegrationError("The camera could not be reached or authenticated within its timeout.") from None
            stamp = utc_now()
            self._frame = (frame, stamp, time.monotonic(), time.time())
            return frame, stamp
        finally:
            self._camera_lock.release()

    def close(self):
        self._closed = True
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()


class BambuAdapter(Adapter):
    def __init__(self, config, secrets):
        super().__init__(config, secrets)
        printers = config.get("printers")
        if not isinstance(printers, list) or not 1 <= len(printers) <= 8:
            raise IntegrationError("A Bambu module requires between 1 and 8 printers.")
        self.printers = {}
        for item in printers:
            if not isinstance(item, dict):
                raise IntegrationError("Each printer configuration must be an object.")
            printer = PrinterSession(item, secrets, config.get("controls_enabled", False) is True)
            if printer.id in self.printers:
                raise IntegrationError("Printer ids must be unique within the module.")
            self.printers[printer.id] = printer

    def read(self):
        for printer in self.printers.values():
            printer.start()
            printer.request_full_status()
        rows = [printer.snapshot() for printer in self.printers.values()]
        available = sum(row["available"] for row in rows)
        stamps = [row["observed_at"] for row in rows if row["available"] and row["observed_at"]]
        return self.snapshot(available=available > 0, summary=f"{available}/{len(rows)} printers reporting",
                metrics={"online": available, "offline": len(rows) - available, "total": len(rows),
                         "printing": sum(row["state"] == "printing" for row in rows)},
                details={"printers": rows, "degraded": available < len(rows)},
                capabilities=sorted({action for row in rows for action in row["capabilities"]}),
                observed_at=min(stamps) if stamps else None)

    def wait_ready(self, timeout=8):
        """Explicit first-run/one-shot bootstrap wait, never the normal poll.

        MQTT startup is asynchronous. A CLI connectivity check must allow the
        TLS handshake and first report to arrive before closing its listener.
        This sends only the existing rate-limited status query, never controls.
        """
        if type(timeout) not in (int, float) or not 0.25 <= timeout <= 15:
            raise IntegrationError("Bootstrap wait must be between 0.25 and 15 seconds.")
        deadline = time.monotonic() + timeout
        while True:
            result = self.read()
            if (all(row["available"] for row in result["details"]["printers"])
                    or all(printer.client is None or printer._closed for printer in self.printers.values())
                    or time.monotonic() >= deadline):
                return result
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def command(self, action, parameters, request_id):
        if not isinstance(parameters, dict) or parameters.get("printer_id") not in self.printers:
            return {"status": "rejected", "message": "Select a configured printer."}
        return self._once(action, parameters, request_id,
                          lambda: self.printers[parameters["printer_id"]].command(action, parameters))

    def camera_bytes(self, printer_id):
        if printer_id not in self.printers:
            raise IntegrationError("Unknown printer.")
        return self.printers[printer_id].camera_bytes()

    def close(self):
        for printer in self.printers.values():
            printer.close()
