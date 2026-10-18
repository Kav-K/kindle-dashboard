"""Synthetic MQTT/TLS/camera fixtures: never connect to a real printer."""
import hashlib
import io
import json
import ssl
import struct
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from inkboard.integrations.bambu import BambuAdapter, IntegrationError, PrinterSession, auth_packet, read_exact, tls_settings, validate_jpeg, verify_peer


def printer_config(**changes):
    result = {"id": "printer-one", "name": "Workshop printer", "host": "printer.example.invalid",
              "serial": "EXAMPLE123456", "access_code_secret": "printer_code", "tls_fingerprint": "0" * 64,
              "mode": "lan_developer", "camera_enabled": True, "timeout_seconds": 0.25}
    result.update(changes)
    return result


def session(**changes):
    return PrinterSession(printer_config(**changes), {"printer_code": "fixture"}, True)


def feed(obj, fields, *, root="print", retain=False, topic=None):
    obj._on_message(obj.client, None, SimpleNamespace(topic=topic or obj.report_topic, retain=retain,
                                                     payload=json.dumps({root: fields}).encode()))


class Client:
    def __init__(self, printer=None, response=None, rc=0):
        self.printer, self.response, self.rc = printer, response, rc
        self.published, self.subscriptions = [], []
    def publish(self, topic, payload, **kwargs):
        self.published.append((topic, json.loads(payload), kwargs))
        if self.response:
            self.response(self.printer, json.loads(payload))
        return SimpleNamespace(rc=self.rc)
    def subscribe(self, topic, **kwargs): self.subscriptions.append((topic, kwargs))
    def disconnect(self): pass
    def loop_stop(self): pass


def online(obj, state="RUNNING"):
    obj.client = Client(obj)
    obj._connected = obj._subscribed = True
    feed(obj, {"gcode_state": state, "mc_percent": 25, "mc_remaining_time": 10,
               "layer_num": 2, "total_layer_num": 8, "nozzle_temper": 210, "bed_temper": 60,
               "lights_report": [{"node": "chamber_light", "mode": "on"}]})
    return obj


def params(obj, **extra):
    return {"printer_id": obj.id, "job_identity": obj.snapshot()["job_identity"], **extra}


class Stream:
    def __init__(self, data=b"", cert=b"certificate", common_name="EXAMPLE123456"):
        self.data, self.cert, self.common_name, self.sent, self.timeouts = data, cert, common_name, [], []
    def getpeercert(self, binary_form=False):
        return self.cert if binary_form else {"subject": ((("commonName", self.common_name),),)}
    def settimeout(self, timeout): self.timeouts.append(timeout)
    def recv(self, size):
        part, self.data = self.data[:min(size, 7)], self.data[min(size, 7):]
        return part
    def sendall(self, value): self.sent.append(value)
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Context:
    def __init__(self, stream): self.stream = stream
    def wrap_socket(self, *args, **kwargs): return self.stream


class BambuTests(unittest.TestCase):
    def test_missing_identity_configuration_fails_closed(self):
        with self.assertRaisesRegex(IntegrationError, "tls_fingerprint"):
            session(tls_fingerprint="")
        with self.assertRaises(IntegrationError): session(tls_fingerprint="abc")
        with self.assertRaises(IntegrationError): session(tls_fingerprint="", tls_ca_file="/nonexistent/example.pem")

    def test_pin_is_checked_exactly_before_credentials(self):
        stream = Stream()
        verify_peer(stream, hashlib.sha256(stream.cert).hexdigest(), "ignored")
        with self.assertRaisesRegex(IntegrationError, "fingerprint"):
            verify_peer(stream, "0" * 64, "ignored")
        self.assertEqual(stream.sent, [])

    def test_ca_identity_requires_exact_one_serial_cn(self):
        verify_peer(Stream(), None, "EXAMPLE123456")
        with self.assertRaises(IntegrationError): verify_peer(Stream(common_name="OTHER123456"), None, "EXAMPLE123456")

    def test_camera_auth_packet_is_fixed_length_and_bounded(self):
        packet = auth_packet("fixture")
        self.assertEqual(len(packet), 80)
        self.assertEqual(struct.unpack("<IIII", packet[:16]), (0x40, 0x3000, 0, 0))
        self.assertEqual(packet[16:48].rstrip(b"\0"), b"bblp")
        for value in ("", "x" * 33, "\u00e9"):
            with self.assertRaises(IntegrationError): auth_packet(value)

    def test_cloud_mode_never_advertises_direct_print_controls(self):
        obj = online(session(mode="cloud"))
        self.assertEqual(obj.snapshot()["capabilities"], [])
        with self.assertRaisesRegex(IntegrationError, "Cloud-mode"):
            obj.command("pause", params(obj))
        self.assertEqual(obj.client.published, [])

    def test_read_merges_deltas_and_never_exposes_access_code_host_or_serial(self):
        obj = online(session())
        feed(obj, {"mc_percent": 30, "nozzle_temper": 215, "private_field": "do-not-return"})
        snapshot = obj.snapshot()
        self.assertEqual(snapshot["state"], "printing")
        self.assertEqual(snapshot["progress"], 30)
        self.assertEqual(snapshot["nozzle_c"], 215)
        encoded = json.dumps(snapshot)
        for secret in ("fixture", "EXAMPLE123456", "printer.example.invalid", "do-not-return"):
            self.assertNotIn(secret, encoded)

    def test_retained_wrong_topic_invalid_payload_and_invalid_fields_do_not_become_live(self):
        obj = session()
        obj._connected = True
        feed(obj, {"gcode_state": "RUNNING"}, retain=True)
        feed(obj, {"gcode_state": "RUNNING"}, topic="device/another/report")
        feed(obj, {"gcode_state": "unrecognized", "mc_percent": True})
        feed(obj, {"gcode_state": {"unexpected": True}, "mc_percent": "25"})
        obj._on_message(None, None, SimpleNamespace(topic=obj.report_topic, retain=False, payload=b"{"))
        obj._on_message(None, None, SimpleNamespace(topic=obj.report_topic, retain=False, payload=b"x" * 262145))
        self.assertFalse(obj.snapshot()["available"])

    def test_missing_or_stale_state_keeps_controls_unavailable(self):
        obj = session()
        obj._connected = True
        obj.client = Client(obj)
        feed(obj, {"nozzle_temper": 210})
        self.assertEqual(obj.snapshot()["capabilities"], [])
        feed(obj, {"gcode_state": "RUNNING"})
        obj._last_report_wall -= 100
        self.assertFalse(obj.snapshot()["available"])
        self.assertEqual(obj.snapshot()["capabilities"], [])

    def test_sleep_and_clock_changes_do_not_make_report_fresh(self):
        obj = online(session())
        obj._last_report_wall += 100
        self.assertFalse(obj.snapshot()["available"])

    def test_job_binding_survives_pause_resume_and_changes_on_new_job_and_reconnect(self):
        obj = online(session())
        identity = obj.snapshot()["job_identity"]
        self.assertRegex(identity, r"^run:[0-9a-f]{32}:task:1$")
        feed(obj, {"gcode_state": "PAUSE"})
        feed(obj, {"gcode_state": "RUNNING"})
        self.assertEqual(obj.snapshot()["job_identity"], identity)
        feed(obj, {"gcode_state": "FINISH"})
        feed(obj, {"gcode_state": "RUNNING"})
        self.assertNotEqual(obj.snapshot()["job_identity"], identity)
        previous = obj.snapshot()["job_identity"]
        obj._on_connect(obj.client, None, None, 0, None)
        self.assertIsNone(obj.snapshot()["job_identity"])
        self.assertFalse(obj.snapshot()["available"])
        feed(obj, {"gcode_state": "RUNNING"})
        self.assertNotEqual(obj.snapshot()["job_identity"], previous)

    def test_old_job_command_cannot_target_a_new_print(self):
        obj = online(session())
        parameters = params(obj)
        feed(obj, {"gcode_state": "FINISH"})
        feed(obj, {"gcode_state": "RUNNING"})
        with self.assertRaisesRegex(IntegrationError, "active print changed"):
            obj.command("pause", parameters)
        self.assertEqual(obj.client.published, [])

    def test_pause_confirmed_only_by_new_state_report(self):
        obj = online(session())
        obj.client.response = lambda printer, document: feed(printer, {"gcode_state": "PAUSE"})
        result = obj.command("pause", params(obj))
        self.assertEqual(result["status"], "confirmed")
        _, payload, options = obj.client.published[-1]
        self.assertEqual(payload["print"]["command"], "pause")
        self.assertEqual(options, {"qos": 0, "retain": False})

    def test_resume_stop_speed_and_light_encode_only_allowlisted_payloads(self):
        cases = [("resume", "PAUSE", {}, {"gcode_state": "RUNNING"}),
                 ("stop", "RUNNING", {"confirm": "STOP"}, {"gcode_state": "IDLE"}),
                 ("speed", "RUNNING", {"value": 3}, {"spd_lvl": 3}),
                 ("light", "RUNNING", {"value": "off"}, {"lights_report": [{"node": "chamber_light", "mode": "off"}]})]
        for action, state, extra, report in cases:
            with self.subTest(action=action):
                obj = online(session(), state)
                obj.client.response = lambda printer, document, report=report: feed(printer, report)
                self.assertEqual(obj.command(action, params(obj, **extra))["status"], "confirmed")

    def test_invalid_stop_speed_values_and_arbitrary_commands_never_publish(self):
        obj = online(session())
        cases = [("stop", {}), ("speed", {"value": True}), ("speed", {"value": 0}),
                 ("speed", {"value": 5}), ("speed", {"value": "3"}), ("light", {"value": "blink"}),
                 ("gcode", {"value": "G28"}), ("pause", {"value": "anything"})]
        for action, extra in cases:
            with self.subTest(action=action), self.assertRaises(IntegrationError): obj.command(action, params(obj, **extra))
        self.assertEqual(obj.client.published, [])

    def test_command_ack_is_sent_not_confirmed_and_rejection_is_not_hidden(self):
        obj = online(session())
        def accepted(printer, document):
            payload = document["print"]
            feed(printer, {"sequence_id": payload["sequence_id"], "command": payload["command"], "result": "success"})
        obj.client.response = accepted
        self.assertEqual(obj.command("pause", params(obj))["status"], "sent")
        def rejected(printer, document):
            payload = document["print"]
            feed(printer, {"sequence_id": payload["sequence_id"], "command": payload["command"], "result": "failed", "gcode_state": "PAUSE"})
        obj.client.response = rejected
        self.assertEqual(obj.command("pause", params(obj))["status"], "rejected")

    def test_timeout_and_publish_connection_race_stay_uncertain(self):
        for rc in (0, 4):
            obj = online(session())
            obj.client.rc = rc
            self.assertEqual(obj.command("pause", params(obj))["status"], "uncertain")
            self.assertEqual(len(obj.client.published), 1)
            self.assertIsNone(obj._pending)

    def test_bootstrap_status_rate_limit_and_subscription_order(self):
        obj = session()
        obj.client = Client(obj)
        obj._on_connect(obj.client, None, None, 0, None)
        self.assertEqual(obj.client.published, [])
        obj.request_full_status()
        self.assertEqual(obj.client.published, [])
        obj._on_subscribe(obj.client, None, 1, [], None)
        obj.request_full_status()
        self.assertEqual(len(obj.client.published), 1)
        self.assertEqual(obj.client.published[0][1]["pushing"]["command"], "pushall")

    def test_disconnect_clears_control_eligibility(self):
        obj = online(session())
        obj._on_disconnect(None, None, None, 1, None)
        self.assertFalse(obj.snapshot()["available"])
        self.assertEqual(obj.snapshot()["capabilities"], [])

    def test_read_exact_checks_deadline_and_complete_stream(self):
        self.assertEqual(read_exact(Stream(b"abcdef"), 6, time.monotonic() + 1), b"abcdef")
        with self.assertRaises(TimeoutError): read_exact(Stream(b"abcdef"), 6, time.monotonic() - 1)
        with self.assertRaises(IntegrationError): read_exact(Stream(b"abc"), 6, time.monotonic() + 1)

    def test_camera_pin_mismatch_sends_no_auth(self):
        obj = session()
        stream = Stream()
        obj.context = Context(stream)
        with patch("inkboard.integrations.bambu.socket.create_connection", return_value=Stream()):
            with self.assertRaisesRegex(IntegrationError, "fingerprint"): obj.camera_bytes()
        self.assertEqual(stream.sent, [])

    def test_camera_validates_frame_and_cache_and_keeps_timestamp(self):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (4, 3), (0, 100, 255)).save(output, format="JPEG")
        jpeg = output.getvalue()
        obj = session()
        stream = Stream(struct.pack("<IIII", len(jpeg), 0, 1, 0) + jpeg)
        obj.fingerprint = hashlib.sha256(stream.cert).hexdigest()
        obj.context = Context(stream)
        with patch("inkboard.integrations.bambu.socket.create_connection", return_value=Stream()) as connect:
            first = obj.camera_bytes()
            second = obj.camera_bytes()
            self.assertEqual(first, second)
            self.assertEqual(first[0], jpeg)
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(stream.sent, [auth_packet("fixture")])

    def test_camera_invalid_headers_and_oversized_frames_fail_before_payload_read(self):
        for header in ((2_000_000, 0, 1, 0), (200, 1, 1, 0), (200, 0, 0, 0)):
            obj = session()
            stream = Stream(struct.pack("<IIII", *header) + b"x" * 200)
            obj.fingerprint = hashlib.sha256(stream.cert).hexdigest()
            obj.context = Context(stream)
            with patch("inkboard.integrations.bambu.socket.create_connection", return_value=Stream()):
                with self.assertRaisesRegex(IntegrationError, "invalid frame header"): obj.camera_bytes()

    def test_invalid_jpeg_rejected(self):
        with self.assertRaises(IntegrationError): validate_jpeg(b"not an image")
        with self.assertRaises(IntegrationError): validate_jpeg(b"\xff\xd8" + b"x" * 200 + b"\xff\xd9")

    def test_missing_optional_mqtt_dependency_does_not_crash_module(self):
        obj = session()
        with patch.dict(sys.modules, {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}): obj.start()
        self.assertIn("bambu extra", obj.snapshot()["control_hint"])
        self.assertIsNone(obj.client)

    def test_module_duplicate_id_and_unknown_target_rejected(self):
        config = {"id": "printers", "type": "bambu", "controls_enabled": True, "printers": [printer_config()]}
        obj = BambuAdapter(config, {"printer_code": "fixture"})
        self.assertEqual(obj.command("pause", {"printer_id": "unknown"}, "request-001")["status"], "rejected")
        config["printers"].append(printer_config())
        with self.assertRaises(IntegrationError): BambuAdapter(config, {"printer_code": "fixture"})

    def test_offline_printer_does_not_disable_another_printer(self):
        config = {"id": "printers", "type": "bambu", "controls_enabled": True,
                  "printers": [printer_config(), printer_config(id="printer-two")]}
        obj = BambuAdapter(config, {"printer_code": "fixture"})
        first = online(obj.printers["printer-one"])
        with patch.object(first, "start"), patch.object(obj.printers["printer-two"], "start"):
            result = obj.read()
        self.assertTrue(result["available"])
        self.assertTrue(result["details"]["degraded"])
        self.assertEqual(result["metrics"]["offline"], 1)
        self.assertIn("pause", result["capabilities"])
        self.assertIn("pause", result["details"]["printers"][0]["capabilities"])
        self.assertEqual(result["details"]["printers"][1]["capabilities"], [])

    def test_explicit_bootstrap_wait_accepts_first_live_report(self):
        config = {"id": "printers", "type": "bambu", "printers": [printer_config()]}
        obj = BambuAdapter(config, {"printer_code": "fixture"})
        printer = obj.printers["printer-one"]
        printer.client = Client(printer)
        printer._connected = printer._subscribed = True
        with patch.object(printer, "start"), patch("inkboard.integrations.bambu.time.sleep",
                   side_effect=lambda delay: feed(printer, {"gcode_state": "IDLE"})):
            result = obj.wait_ready(timeout=0.25)
        self.assertTrue(result["available"])
        self.assertEqual(result["details"]["printers"][0]["state"], "idle")
        self.assertTrue(all("pushing" in document for _, document, _ in printer.client.published))

    def test_explicit_bootstrap_wait_does_not_hang_for_missing_dependency(self):
        config = {"id": "printers", "type": "bambu", "printers": [printer_config()]}
        obj = BambuAdapter(config, {"printer_code": "fixture"})
        with patch.dict(sys.modules, {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            result = obj.wait_ready(timeout=0.25)
        self.assertFalse(result["available"])
        self.assertIsNone(obj.printers["printer-one"].client)


if __name__ == "__main__": unittest.main()
