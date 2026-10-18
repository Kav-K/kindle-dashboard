"""Adapter orchestration. Cached reads never contact a printer or media server."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import queue
import re
import threading
import time
import uuid

from .store import Store, encode, utc


def age(timestamp) -> float:
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return float("inf")
        delta = time.time() - value.timestamp()
        return delta if delta >= -30 else float("inf")
    except (ValueError, TypeError, AttributeError):
        return float("inf")


class Demo:
    def __init__(self, config):
        self.config = config
    def read(self):
        return {"id": self.config["id"], "type": "demo", "name": self.config["name"], "observed_at": utc(),
                "available": True, "summary": "Simulated data — no physical device is connected", "metrics": {"printers": 2},
                "capabilities": [], "details": {"printers": [
                    {"id": "demo-one", "name": "Demo printer One", "state": "printing", "progress": 63, "job_name": "Sample enclosure", "remaining_minutes": 24, "nozzle_temperature": 215, "bed_temperature": 60, "layer": 126, "total_layers": 200},
                    {"id": "demo-two", "name": "Demo printer Two", "state": "idle", "progress": 0, "job_name": "Ready for a new print", "remaining_minutes": 0, "nozzle_temperature": 23, "bed_temperature": 23}
                ]}}
    def close(self):
        pass


class Hub:
    def __init__(self, config: dict, secrets: dict, store: Store, adapter_factory=None):
        self.config, self.secrets, self.store = config, secrets, store
        self.stop_event = threading.Event()
        self.adapters = {}
        self.module_configs = {module["id"]: module for module in config["modules"] if module.get("enabled", True)}
        self.threads = []
        self.command_queue = queue.Queue(maxsize=32)
        if adapter_factory is None:
            from .integrations import create_adapter
            adapter_factory = create_adapter
        for module in self.module_configs.values():
            if module["type"] == "alerts" or module.get("remote", False):
                if module.get("remote", False) and module["id"] not in self.store.snapshots():
                    self.store.snapshot(module["id"], self.unavailable(module, "Waiting for the remote collector's first observation."))
                continue
            try:
                self.adapters[module["id"]] = Demo(module) if module["type"] == "demo" else adapter_factory(module, secrets)
            except Exception:
                self.store.snapshot(module["id"], self.unavailable(module, "Module configuration is invalid; run inkboard check."))

    @staticmethod
    def unavailable(module, message):
        return {"id": module["id"], "type": module["type"], "name": module.get("name", module["id"]), "observed_at": None,
                "available": False, "summary": message, "metrics": {}, "capabilities": [], "details": {}, "failure_started_at": utc()}

    def validate_snapshot(self, ident, document, *, remote=False):
        if not isinstance(document, dict) or document.get("id") != ident or type(document.get("available")) is not bool:
            raise ValueError("Invalid observation envelope")
        if document.get("available") and age(document.get("observed_at")) == float("inf"):
            raise ValueError("Invalid observation timestamp")
        if not isinstance(document.get("metrics", {}), dict) or not isinstance(document.get("details", {}), dict):
            raise ValueError("Observation details and metrics must be objects")
        def capabilities(value):
            return isinstance(value, list) and len(value) <= 32 and all(isinstance(item, str) and re.fullmatch(r"[a-z_]{1,40}", item) for item in value) and len(set(value)) == len(value)
        if not capabilities(document.get("capabilities", [])):
            raise ValueError("Invalid capabilities")
        def bounded(value, depth=0):
            if depth > 8:
                raise ValueError("Observation is nested too deeply")
            if value is None or type(value) in {bool, int}:
                return
            if isinstance(value, float):
                if not math.isfinite(value):
                    raise ValueError("Observation numbers must be finite")
            elif isinstance(value, str):
                if len(value) > 4096 or any(ord(char) < 32 for char in value):
                    raise ValueError("Observation strings must be bounded text")
            elif isinstance(value, dict):
                if len(value) > 128 or any(not isinstance(k, str) or len(k) > 80 for k in value):
                    raise ValueError("Observation object is too large")
                for item in value.values():
                    bounded(item, depth + 1)
            elif isinstance(value, list):
                if len(value) > 128:
                    raise ValueError("Observation list is too large")
                for item in value:
                    bounded(item, depth + 1)
            else:
                raise ValueError("Observation contains unsupported values")
        bounded(document)
        if len(encode(document).encode("utf-8")) > 262144:
            raise ValueError("Observation exceeds size limit")
        details = document.get("details", {})
        for key in ("printers", "streams", "libraries", "requests", "services", "drives"):
            values = details.get(key, [])
            if not isinstance(values, list) or len(values) > (16 if key == "printers" else 100 if key == "requests" else 64) or any(not isinstance(row, dict) for row in values):
                raise ValueError("Invalid observation rows")
        for row in details.get("printers", []):
            if not isinstance(row.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", row["id"]) or type(row.get("available", True)) is not bool or not capabilities(row.get("capabilities", [])):
                raise ValueError("Invalid printer observation")
            if row.get("job_identity") is not None and not re.fullmatch(r"run:[0-9a-f]{32}:task:[1-9][0-9]{0,9}", str(row["job_identity"])):
                raise ValueError("Invalid printer job identity")
            if row.get("progress") is not None and (type(row["progress"]) not in {int, float} or not 0 <= row["progress"] <= 100):
                raise ValueError("Invalid printer progress")
        if "degraded" in details and type(details["degraded"]) is not bool:
            raise ValueError("Invalid degraded indicator")
        for row in details.get("streams", []):
            for key in ("id", "title", "player", "state", "location", "type"):
                if key in row and not isinstance(row[key], str):
                    raise ValueError("Invalid stream text")
            for key, maximum in (("progress", 100), ("transcode_progress", 100), ("position_ms", 315576000000), ("duration_ms", 315576000000), ("bandwidth_kbps", 1000000000)):
                if row.get(key) is not None and (type(row[key]) not in {int, float} or not 0 <= row[key] <= maximum):
                    raise ValueError("Invalid stream metric")
            if "transcoding" in row and type(row["transcoding"]) is not bool:
                raise ValueError("Invalid transcoding indicator")
        module = self.module_configs[ident]
        value = {"id": ident, "type": module["type"], "name": module.get("name", ident), "observed_at": document.get("observed_at"), "available": document["available"], "summary": str(document.get("summary", ""))[:240], "metrics": document.get("metrics", {}), "details": details, "capabilities": [] if remote else document.get("capabilities", [])}
        if remote:
            value = json.loads(encode(value))
            for row in value["details"].get("printers", []):
                row["capabilities"] = []
        return value

    def poll_once(self, ident: str):
        module = self.module_configs[ident]
        try:
            document = self.adapters[ident].read()
            document = self.validate_snapshot(ident, document)
            previous = self.store.snapshots().get(ident, {})
            if document.get("available") and not document.get("details", {}).get("degraded"):
                document["failure_started_at"] = None
            else:
                document["failure_started_at"] = previous.get("failure_started_at") or utc()
            self.store.snapshot(ident, document)
        except Exception:
            # Adapter exceptions may embed credential-bearing URLs. Never render or log them.
            previous = self.store.snapshots().get(ident)
            failed = self.unavailable(module, "Source unavailable. Check its network, credentials and service health.")
            failed["failure_started_at"] = (previous or {}).get("failure_started_at") or utc()
            if previous:
                failed["observed_at"] = previous.get("observed_at")
                failed["details"] = previous.get("details", {})
                failed["metrics"] = previous.get("metrics", {})
            self.store.snapshot(ident, failed)

    def _poll(self, ident):
        while not self.stop_event.is_set():
            self.poll_once(ident)
            if self.stop_event.wait(self.config["refresh_seconds"]):
                break

    def start(self):
        for ident in self.adapters:
            thread = threading.Thread(target=self._poll, args=(ident,), name="inkboard-source-" + ident, daemon=True)
            self.threads.append(thread)
            thread.start()
        worker = threading.Thread(target=self._commands, name="inkboard-commands", daemon=True)
        self.threads.append(worker)
        worker.start()

    def snapshot(self) -> dict:
        snapshots = self.store.snapshots()
        modules = []
        for ident, config in self.module_configs.items():
            if config["type"] == "alerts":
                continue
            value = snapshots.get(ident, self.unavailable(config, "Waiting for first observation."))
            value["stale"] = age(value.get("observed_at")) > self.config["stale_after_seconds"]
            value["age_seconds"] = min(2147483647, max(0, int(age(value.get("observed_at"))))) if age(value.get("observed_at")) != float("inf") else None
            if value["stale"] or not value.get("available"):
                value["capabilities"] = []
            modules.append(value)
        return {"schema_version": 1, "name": self.config["name"], "generated_at": utc(), "modules": modules}

    def printers(self) -> dict:
        printers = []
        history = {}
        for module in self.snapshot()["modules"]:
            for original in module.get("details", {}).get("printers", [])[:16]:
                if not isinstance(original, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(original.get("id", ""))):
                    continue
                printer = dict(original)
                individual_age = age(original.get("observed_at", module.get("observed_at")))
                printer.update(module_id=module["id"], online=bool(module["available"] and original.get("available", True) and not module["stale"] and individual_age <= self.config["stale_after_seconds"]), stale=bool(module["stale"] or individual_age > self.config["stale_after_seconds"]))
                controls = set(original.get("capabilities", module.get("capabilities", []))) & set(module.get("capabilities", [])) if printer["online"] else set()
                printer["control_link_ready"] = bool(controls)
                printer["control_capabilities"] = {action: action in controls for action in ("pause", "resume", "stop", "speed", "light", "reprint")}
                printer.setdefault("name", printer["id"])
                printer.setdefault("state", "unknown")
                printer.setdefault("job_name", printer.get("job", ""))
                printer.setdefault("chamber_light", printer.get("light") or "unknown")
                printer.setdefault("speed_level", printer.get("speed") or 1)
                printer.setdefault("nozzle_temperature", printer.get("nozzle_c"))
                printer.setdefault("bed_temperature", printer.get("bed_c"))
                printer.setdefault("last_seen_at", original.get("observed_at", module.get("observed_at")))
                printer.setdefault("stale_seconds", min(31536000, int(individual_age)) if individual_age != float("inf") else None)
                if any(row["id"] == printer["id"] for row in printers):
                    # Ambiguous target identity must not expose a control surface.
                    for prior in printers:
                        if prior["id"] == printer["id"]:
                            prior["control_link_ready"] = False
                            prior["control_capabilities"] = {}
                    continue
                printers.append(printer)
                history[printer["id"]] = []
        selected = self.config.get("kindle", {}).get("printer_ids", [])
        order = {ident: index for index, ident in enumerate(selected)}
        printers.sort(key=lambda row: order.get(row["id"], 100))
        return {"schema_version": 1, "generated_at": utc(), "printers": printers, "history": history, "relay_age_seconds": 0}

    def alerts(self, history=False):
        sources, current = [], set()
        all_notices = self.store.notices()
        for module in self.snapshot()["modules"]:
            healthy = module.get("available") and not module.get("stale") and not module.get("details", {}).get("degraded")
            sources.append({"id": module["id"], "state": "online" if healthy else "stale" if module.get("stale") else "unavailable", "checked_at": module.get("observed_at"), "age_seconds": module.get("age_seconds")})
            ident = str(uuid.uuid5(uuid.NAMESPACE_URL, "inkboard:source:" + module["id"]))
            existing = all_notices.get(ident)
            started = module.get("failure_started_at") or module.get("observed_at")
            if not healthy and started and age(started) >= max(90, self.config["refresh_seconds"] * 3):
                current.add(ident)
                if not existing or existing.get("state") == "resolved":
                    summary = "One or more devices or services in this module have no current observation. Cached data remains visible; unavailable device controls are disabled."
                    existing = {"id": ident, "kind": "source_stale", "section": "services", "severity": "warning", "state": "open", "title": module["name"] + " needs attention", "summary": summary, "occurred_at": started, "last_seen_at": utc(), "occurrences": 1, "unread": True, "acknowledged": False, "muted": False, "notify": False, "snoozed_until": None, "ai": {"status": "disabled", "confidence": "unknown", "analyzed_at": None, "summary": None, "likely_cause": None, "suggested_checks": []}, "target": {"section": "services", "entity_id": module["id"]}, "actions": ["open_services"], "evidence": [{"label": "Last source observation", "value": str(module.get("observed_at") or "None received")}], "checks": ["Check the source device is awake and reachable from the collector.", "Run inkboard check to verify configuration and secret references.", "Check the integration's TLS identity and credentials without weakening TLS verification.", "Refresh after recovery and verify a newer source observation."]}
                else:
                    existing["last_seen_at"] = module.get("observed_at") or existing["last_seen_at"]
                self.store.notice(ident, existing)
                all_notices[ident] = existing
            elif healthy and existing and existing.get("state") == "open":
                existing.update(state="resolved", last_seen_at=utc())
                self.store.notice(ident, existing)
                all_notices[ident] = existing
        rows = sorted((value for value in all_notices.values() if history or (value.get("state") == "open" and not value.get("archived", False))), key=lambda row: row["occurred_at"], reverse=True)[:60]
        quiet = self.store.setting("quiet") or {"enabled": False, "start_hour": 22, "end_hour": 8, "timezone": self.config.get("kindle", {}).get("timezone", "UTC")}
        from zoneinfo import ZoneInfo
        hour = datetime.now(ZoneInfo(quiet["timezone"])).hour
        quiet["active"] = bool(quiet["enabled"] and (quiet["start_hour"] <= hour < quiet["end_hour"] if quiet["start_hour"] < quiet["end_hour"] else hour >= quiet["start_hour"] or hour < quiet["end_hour"]))
        members = [{"id": row["id"], "occurred_at": row["occurred_at"]} for row in rows]
        cutoff = str(uuid.uuid5(uuid.NAMESPACE_OID, "inkboard:alerts:" + encode(members)))
        self.store.setting("cutoff:" + cutoff, {"members": members, "created_at": utc()})
        return {"schema_version": 1, "generated_at": utc(), "view": "history" if history else "inbox", "includes_archived": history, "archived_count": sum(row.get("archived", False) for row in all_notices.values()), "quiet_hours": quiet, "archive_cutoff": cutoff, "counts": {"unread": sum(row["unread"] for row in rows), "open": sum(row["state"] == "open" for row in rows), "total": len(rows), "attention": sum(row["unread"] and row["state"] == "open" and not row["muted"] and (row["snoozed_until"] is None or datetime.fromisoformat(row["snoozed_until"].replace("Z", "+00:00")).timestamp() <= time.time()) for row in rows)}, "source_status": sources[:16], "alerts": rows}

    def status_details(self, key):
        modules = [m for m in self.snapshot()["modules"] if m.get("type") == "http_status" and self.module_configs[m["id"]].get("native_tab", key) == key]
        services = []
        for module in modules:
            for row in module.get("details", {}).get("services", []):
                state = "stale" if module["stale"] else "online" if row.get("available") else "unavailable"
                services.append({"id": str(row.get("id", ""))[:64], "name": str(row.get("name", "Unknown"))[:80], "availability": state, "status": state, "latency_ms": row.get("latency_ms"), "observed_at": row.get("observed_at"), "message": str(row.get("message", ""))[:240]})
        return {"schema_version": 1, "key": key, "generated_at": utc(), "stale": not modules or any(m["stale"] for m in modules), "services": services[:64], "metrics": {}, "top_paths": []}

    def alert_action(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"request_id", "action", "alert_id", "minutes", "cutoff", "quiet_hours"}:
            raise ValueError("Invalid alert action")
        ident = payload.get("request_id")
        try:
            if str(uuid.UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid alert request id") from None
        fingerprint = hashlib.sha256(encode(payload).encode()).hexdigest()
        with self.store.lock, self.store.db:
            prior = self.store.known_command(ident, fingerprint)
            if prior:
                if "receipt" not in prior:
                    raise ValueError("Receipt detail expired; this request will not be applied again")
                return prior["receipt"]
            action = payload.get("action")
            rows = self.store.notices()
            targets = []
            if action in {"mark_read", "mark_unread", "acknowledge", "snooze", "mute", "unmute"}:
                row = rows.get(payload.get("alert_id"))
                if row is None:
                    raise ValueError("Unknown alert")
                targets = [row]
                if action == "snooze" and payload.get("minutes") not in {15, 60, 240, 1440}:
                    raise ValueError("Unsupported snooze interval")
            elif action == "mark_all_read":
                targets = list(rows.values())
            elif action == "archive_all":
                cutoff = payload.get("cutoff")
                saved = self.store.setting("cutoff:" + str(cutoff))
                if saved is None or age(saved["created_at"]) > 3600:
                    raise ValueError("Clear-all snapshot expired; refresh the inbox")
                targets = [rows[m["id"]] for m in saved["members"] if m["id"] in rows and rows[m["id"]]["occurred_at"] == m["occurred_at"]]
            elif action == "set_quiet_hours":
                q = payload.get("quiet_hours")
                if not isinstance(q, dict) or set(q) != {"enabled", "start_hour", "end_hour", "timezone"} or type(q["enabled"]) is not bool or any(type(q[k]) is not int or not 0 <= q[k] <= 23 for k in ("start_hour", "end_hour")) or q["start_hour"] == q["end_hour"] or q["timezone"] != self.config.get("kindle", {}).get("timezone", "UTC"):
                    raise ValueError("Invalid quiet hours")
                self.store.db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,document=excluded.document", ("quiet", time.time(), encode(q)))
            else:
                raise ValueError("This alert action is not configured")
            for row in targets:
                if action in {"mark_read", "mark_all_read", "acknowledge", "archive_all"}:
                    row["unread"] = False
                if action == "mark_unread":
                    row["unread"] = True
                elif action == "acknowledge":
                    row["acknowledged"] = True
                elif action == "snooze":
                    from datetime import timedelta
                    row["snoozed_until"] = (datetime.now(timezone.utc) + timedelta(minutes=payload["minutes"])).isoformat(timespec="seconds").replace("+00:00", "Z")
                elif action in {"mute", "unmute"}:
                    row["muted"] = action == "mute"
                elif action == "archive_all":
                    row["archived"] = True
                self.store.db.execute("UPDATE notices SET updated=?,document=? WHERE id=?", (time.time(), encode(row), row["id"]))
            receipt = {"schema_version": 1, "request_id": ident, "receipt_id": str(uuid.uuid4()), "action": action, "alert_id": payload.get("alert_id"), "status": "applied"}
            if action == "archive_all":
                receipt.update(cutoff=payload["cutoff"], archived_count=len(targets))
            wrapper = {"id": ident, "printer_id": "", "module_id": "alerts", "action": action, "state": "accepted", "detail": "state_confirmed", "receipt": receipt}
            self.store.create_command(ident, fingerprint, wrapper)
            return receipt

    def plex_status(self):
        modules = [m for m in self.snapshot()["modules"] if m.get("type") == "plex"]
        module = modules[0] if modules else None
        details = module.get("details", {}) if module else {}
        fresh = bool(module and not module["stale"])
        online = fresh and details.get("plex_available", module.get("available")) is True if module else False
        streams = details.get("streams", [])[:64]
        sessions = []
        for row in streams[:16]:
            state = row.get("state") if row.get("state") in {"playing", "paused", "buffering"} else "unknown"
            transcoding = row.get("transcoding") is True
            session = {"id": "ps_" + hashlib.sha256((module["id"] + ":" + str(row.get("id"))).encode()).hexdigest(),
                "state": state, "playback_mode": "transcode" if transcoding else "unknown", "video_decision": "unknown", "audio_decision": "unknown",
                "location": row.get("location") if row.get("location") in {"local", "remote", "relay"} else "unknown", "can_stop": False,
                "transcode": {"active": transcoding, "hardware_decode": None, "hardware_encode": None, "throttled": None, "progress_percent": row.get("transcode_progress"), "speed": None},
                "title": str(row.get("title", "Unknown"))[:160], "type": str(row.get("type", ""))[:32], "show_title": "", "user": "", "player": str(row.get("player", "Unknown"))[:100], "product": "", "video_codec": "", "audio_codec": "", "video_resolution": "",
                "season": None, "episode": None, "offset_ms": row.get("position_ms"), "duration_ms": row.get("duration_ms"), "progress_percent": row.get("progress"), "bandwidth_kbps": row.get("bandwidth_kbps")}
            sessions.append(session)
        observed = details.get("plex_observed_at") or (module.get("observed_at") if module else utc())
        if observed and age(observed) > self.config["stale_after_seconds"]:
            online = False
        summary = {"playing": sum(r.get("state") == "playing" for r in streams), "paused": sum(r.get("state") == "paused" for r in streams), "buffering": sum(r.get("state") == "buffering" for r in streams), "transcoding": sum(r.get("transcoding") is True for r in streams), "bandwidth_complete": all(isinstance(r.get("bandwidth_kbps"), (int, float)) for r in streams), "bandwidth_kbps": sum(r.get("bandwidth_kbps") or 0 for r in streams)}
        block = {"schema_version": 1, "observed_at": observed, "sessions": sessions, "displayed_sessions": len(sessions), "total_sessions": len(streams), "observed_sessions": len(streams), "truncated": len(streams) > len(sessions), "summary_complete": len(streams) < 64, "summary": summary}
        pending = [{"id": row["id"], "title": row.get("title", "Unknown"), "tmdb_id": row.get("media_id"), "media_type": row.get("media_type"), "requester": ""} for row in details.get("requests", []) if row.get("request_status") == 1 and type(row.get("id")) is int]
        return {"schema_version": 1, "generated_at": utc(), "stale": not fresh, "age_seconds": module.get("age_seconds") if module else None,
            "plex": {"state": "online" if online else "unavailable", "server_name": module["name"] if module else "Not configured", "active_streams": len(streams) if online else None, "streams": streams, "now_playing": block, "libraries": details.get("libraries", [])},
            "seerr": {"state": "online" if fresh and details.get("seerr_available") else "unconfigured" if "seerr_available" not in details else "unavailable", "pending_count": len(pending), "pending": pending},
            "drives": details.get("drives", []), "services": details.get("services", []), "requests": details.get("requests", [])}

    def submit(self, payload: dict) -> tuple[dict, bool]:
        if self.stop_event.is_set():
            raise ValueError("Hub is stopping; no new command was queued")
        if not isinstance(payload, dict) or set(payload) - {"request_id", "printer_id", "module_id", "action", "parameters", "value", "confirm", "job_identity"} or ("printer_id" in payload and "module_id" in payload):
            raise ValueError("Invalid command object")
        ident = payload.get("request_id")
        try:
            if str(uuid.UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("request_id must be a lowercase UUID") from None
        action = payload.get("action")
        if not isinstance(action, str) or not re.fullmatch(r"[a-z_]{1,40}", action):
            raise ValueError("Invalid command action")
        fingerprint = hashlib.sha256(encode(payload).encode("utf-8")).hexdigest()
        previous = self.store.known_command(ident, fingerprint)
        if previous:
            return previous, False
        target = payload.get("printer_id", payload.get("module_id", ""))
        parameters = payload.get("parameters", {})
        if not isinstance(parameters, dict) or len(encode(parameters)) > 4096:
            raise ValueError("Invalid command parameters")
        parameters = dict(parameters)
        parameters.update({key: payload[key] for key in ("value", "confirm", "job_identity") if key in payload})
        module_id = payload.get("module_id")
        if payload.get("printer_id"):
            matches = [row for row in self.printers()["printers"] if row["id"] == target]
            if len(matches) != 1:
                raise ValueError("Printer identity is unavailable")
            printer = matches[0]
            module_id = printer["module_id"]
            parameters["printer_id"] = target
            if printer.get("job_identity") and action in {"pause", "resume", "stop", "speed"}:
                if parameters.get("job_identity") != printer["job_identity"]:
                    raise ValueError("The displayed print changed; refresh controls before sending")
            if not printer["control_link_ready"] or not printer["control_capabilities"].get(action):
                raise ValueError("This printer control is unavailable or the source is stale")
        snapshots = {row["id"]: row for row in self.snapshot()["modules"]}
        module = snapshots.get(module_id)
        if module is None or module_id not in self.adapters or action not in module.get("capabilities", []):
            raise ValueError("This action is unavailable or the source is stale")
        if self.module_configs[module_id]["type"] == "bambu" and not payload.get("printer_id"):
            raise ValueError("Printer actions require an explicit top-level printer_id")
        if action == "stop" and parameters.get("confirm") != "STOP":
            raise ValueError("Stop requires explicit confirmation")
        if action == "reprint" and parameters.get("confirm") != "BED_CLEAR":
            raise ValueError("Reprint requires explicit bed-clear confirmation")
        if self.command_queue.full():
            raise ValueError("Command queue is full; inspect existing operations first")
        value = {"id": ident, "printer_id": target, "module_id": module_id, "action": action, "state": "queued", "detail": "Waiting to send", "created_at": utc(), "updated_at": utc()}
        value, created = self.store.create_command(ident, fingerprint, value)
        if created:
            try:
                self.command_queue.put_nowait((ident, module_id, action, parameters))
            except queue.Full:
                self.store.set_command(ident, state="rejected", detail="Command queue is full; nothing sent")
        return value, created

    def _commands(self):
        while not self.stop_event.is_set():
            try:
                ident, module_id, action, parameters = self.command_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                receipt = self.store.command(ident)
                elapsed = age(receipt.get("created_at")) if receipt else float("inf")
                if not 0 <= elapsed <= (15 if parameters.get("printer_id") else 60):
                    self.store.set_command(ident, state="expired", detail="Delivery window expired before dispatch; nothing sent")
                    continue
                current = {row["id"]: row for row in self.snapshot()["modules"]}.get(module_id)
                if not current or action not in current.get("capabilities", []):
                    self.store.set_command(ident, state="rejected", detail="Source became stale before sending; nothing sent")
                    continue
                if parameters.get("printer_id"):
                    printer = next((row for row in self.printers()["printers"] if row["id"] == parameters["printer_id"]), None)
                    if not printer or not printer["online"] or not printer["control_capabilities"].get(action) or (parameters.get("job_identity") and parameters["job_identity"] != printer.get("job_identity")):
                        self.store.set_command(ident, state="rejected", detail="Printer or job changed before sending; nothing sent")
                        continue
                self.store.set_command(ident, state="claimed", detail="Sending to device")
                outcome = self.adapters[module_id].command(action, parameters, ident)
                status = outcome.get("outcome", outcome.get("status", "uncertain"))
                state = {"confirmed": "accepted", "rejected": "rejected"}.get(status, "unknown")
                detail = "state_confirmed" if state == "accepted" else "Device outcome uncertain. Check the reported state before sending again."
                if state == "rejected":
                    detail = "Device rejected this action; check its reported state."
                extra = {}
                if isinstance(outcome.get("data"), dict) and len(encode(outcome["data"])) < 32768:
                    extra["result"] = outcome["data"]
                self.store.set_command(ident, state=state, detail=detail, **extra)
                self.poll_once(module_id)
            except Exception:
                self.store.set_command(ident, state="unknown", detail="No reliable confirmation. Check the device before sending a new command.")
            finally:
                self.command_queue.task_done()

    def ingest(self, payload: dict):
        if not isinstance(payload, dict) or set(payload) != {"module_id", "snapshot"}:
            raise ValueError("Expected module_id and snapshot")
        ident, document = payload["module_id"], payload["snapshot"]
        if ident not in self.module_configs or not self.module_configs[ident].get("remote", False):
            raise ValueError("Remote ingestion is not enabled for this module")
        if not isinstance(document, dict) or document.get("id") != ident or type(document.get("available")) is not bool or age(document.get("observed_at")) > 300:
            raise ValueError("Invalid or stale observation")
        # Remote collectors cannot grant control: commands need a local authenticated adapter.
        document = self.validate_snapshot(ident, document, remote=True)
        previous = self.store.snapshots().get(ident)
        if previous and age(previous.get("observed_at")) < age(document.get("observed_at")) - 1:
            raise ValueError("Observation is older than the cached snapshot")
        if not document["available"] or document["details"].get("degraded"):
            document["failure_started_at"] = (previous or {}).get("failure_started_at") or utc()
        self.store.snapshot(ident, document)

    def close(self):
        self.stop_event.set()
        for adapter in self.adapters.values():
            try:
                adapter.close()
            except Exception:
                pass
        deadline = time.monotonic() + 12
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self.threads)
