"""Offline configuration, persistence, and real loopback HTTP integration tests."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from PIL import Image
from inkboard.config import ConfigError, initialize, load, validate
from inkboard.hub import Hub
from inkboard.server import create_server
from inkboard.store import Store, utc


TOKENS = {"reader_token": "r" * 43, "controller_token": "c" * 43, "ingest_token": "i" * 43}


def config():
    return {"schema_version": 1, "name": "Fixture dashboard", "listen": {"host": "127.0.0.1", "port": 8787}, "public_url": "", "refresh_seconds": 5, "stale_after_seconds": 15,
            "modules": [{"id": "printers", "type": "bambu", "name": "Fixture printers"}]}


class FakeAdapter:
    def __init__(self, module, secrets):
        self.config, self.calls, self.closed = module, [], False
        self.available = True
        self.capabilities = ["pause", "resume", "stop", "speed", "light"]
        self.identity = "run:" + "a" * 32 + ":task:1"
        self.state = "printing"

    def read(self):
        return {"id": self.config["id"], "type": self.config["type"], "name": self.config["name"], "observed_at": utc(), "available": self.available,
                "summary": "Synthetic device", "metrics": {}, "capabilities": self.capabilities,
                "details": {"printers": [{"id": "one", "name": "Fixture One", "available": self.available, "state": self.state, "job": "Fixture job", "job_identity": self.identity, "progress": 50, "capabilities": self.capabilities, "light": "on", "speed": 2, "nozzle_c": 210, "bed_c": 60}]}}

    def command(self, action, parameters, request_id):
        self.calls.append((action, parameters, request_id))
        if action == "pause":
            self.state = "paused"
        elif action == "resume":
            self.state = "printing"
        return {"status": "confirmed", "message": "Synthetic confirmation"}

    def close(self):
        self.closed = True


class SetupTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows actual allow-ACE enforcement")
    def test_windows_explicit_everyone_read_is_rejected(self):
        from inkboard.config import check_private_access
        import subprocess
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "private"
            initialize(home, demo=True)
            subprocess.run(["icacls", str(home / "secrets.json"), "/grant", "*S-1-1-0:R"], capture_output=True, check=True)
            with self.assertRaises(ConfigError):
                check_private_access(home / "secrets.json")
    def test_init_private_distinct_tokens_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "private"
            initialize(home, demo=True)
            settings, tokens = load(home)
            self.assertEqual(settings["modules"][0]["type"], "demo")
            self.assertEqual(len(set(tokens.values())), 3)
            before = (home / "secrets.json").read_bytes()
            with self.assertRaises(ConfigError):
                initialize(home, demo=True)
            self.assertEqual(before, (home / "secrets.json").read_bytes())

    def test_empty_existing_directory_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ConfigError):
                initialize(Path(temporary), demo=True)

    def test_invalid_name_does_not_reserve_directory_and_retry_works(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "new private setup"
            for name in ("", "x" * 81, None):
                with self.subTest(name=name), self.assertRaises(ConfigError):
                    initialize(home, name=name)
                self.assertFalse(home.exists())
            initialize(home, name="Valid retry")
            self.assertEqual(load(home)[0]["name"], "Valid retry")

    def test_default_setup_is_offline_valid(self):
        from inkboard.cli import check_setup
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "private"
            initialize(home)
            self.assertTrue(check_setup(*load(home)))

    def test_unexpected_schema_and_inline_nested_credentials_rejected(self):
        for mutation in (lambda c: c.update(schema_version=2), lambda c: c.update(schema_version=True), lambda c: c.update(extra=True),
                         lambda c: c["modules"][0].update(token="private"), lambda c: c["modules"][0].update(seerr={"api_key": "private"}),
                         lambda c: c.update(refresh_seconds=True), lambda c: c.update(stale_after_seconds=5),
                         lambda c: c["listen"].update(host="example.invalid"), lambda c: c["listen"].update(host=[]),
                         lambda c: c["modules"][0].update(type=[]), lambda c: c["modules"][0].update(remote="false")):
            value = config()
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate(value)

    def test_modules_validate_and_disabled_provider_does_not_need_credentials(self):
        value = config()
        value["modules"] = [{"id": "media", "type": "plex", "enabled": False}]
        from inkboard.cli import check_setup
        self.assertTrue(check_setup(validate(value), TOKENS))
        value["modules"].append({"id": "media", "type": "plex"})
        with self.assertRaises(ConfigError):
            validate(value)

    def test_secret_references_and_kindles_are_nonsecret(self):
        value = config()
        value["modules"][0]["token_secret"] = "printer_code"
        value["kindle"] = {"serial": "EXAMPLE012345678", "printer_ids": ["one", "two"], "timezone": "UTC", "features": {"doctor": False}}
        validate(value)
        value["kindle"]["printer_ids"] = ["one", "one"]
        with self.assertRaises(ConfigError):
            validate(value)

    @unittest.skipIf(os.name == "nt", "POSIX mode enforcement; Windows uses actual ACL inspection")
    def test_group_readable_secrets_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "private"
            initialize(home, demo=True)
            os.chmod(home / "secrets.json", 0o644)
            with self.assertRaises(ConfigError):
                load(home)

    def test_public_url_never_accepts_credentials_or_cleartext(self):
        for url in ("http://public.example.com", "https://user:pass@public.example.com", "https://public.example.com/?token=abc", "https://public.example.com/elsewhere"):
            value = config()
            value["public_url"] = url
            with self.subTest(url=url), self.assertRaises(ConfigError):
                validate(value)


class HubFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.config = config()
        self.store = Store(self.home / "state")
        self.hub = Hub(self.config, TOKENS, self.store, FakeAdapter)
        self.hub.poll_once("printers")

    def tearDown(self):
        self.hub.close()
        self.store.close()
        self.temporary.cleanup()

    def payload(self, action="pause"):
        return {"request_id": str(uuid.uuid4()), "printer_id": "one", "action": action, "job_identity": self.hub.adapters["printers"].identity}


class HubTests(HubFixture, unittest.TestCase):
    def test_old_displayed_job_cannot_rebind_to_new_print(self):
        payload = self.payload()
        self.hub.adapters["printers"].identity = "run:" + "a" * 32 + ":task:2"
        self.hub.poll_once("printers")
        with self.assertRaises(ValueError):
            self.hub.submit(payload)
        self.assertEqual(self.hub.adapters["printers"].calls, [])

    def test_queued_printer_intent_expires_without_dispatch(self):
        payload = self.payload()
        self.hub.submit(payload)
        self.store.set_command(payload["request_id"], created_at="2000-01-01T00:00:00Z")
        self.hub.start()
        self.hub.command_queue.join()
        self.assertEqual(self.store.command(payload["request_id"])["state"], "expired")
        self.assertEqual(self.hub.adapters["printers"].calls, [])

    def test_generic_module_route_cannot_bypass_printer_job_binding(self):
        payload = {"request_id": str(uuid.uuid4()), "module_id": "printers", "action": "pause", "parameters": {"printer_id": "one"}}
        with self.assertRaises(ValueError):
            self.hub.submit(payload)

    def test_normalization_uses_actual_fields_and_capabilities(self):
        row = self.hub.printers()["printers"][0]
        self.assertEqual((row["job_name"], row["nozzle_temperature"], row["bed_temperature"], row["chamber_light"], row["speed_level"]), ("Fixture job", 210, 60, "on", 2))
        self.assertTrue(row["control_link_ready"])

    def test_stale_source_preserves_view_but_disables_controls(self):
        snapshot = self.store.snapshots()["printers"]
        snapshot["observed_at"] = "2000-01-01T00:00:00Z"
        self.store.snapshot("printers", snapshot)
        row = self.hub.printers()["printers"][0]
        self.assertTrue(row["stale"])
        self.assertEqual(row["job_name"], "Fixture job")
        self.assertFalse(row["control_link_ready"])
        with self.assertRaises(ValueError):
            self.hub.submit(self.payload())

    def test_per_printer_capabilities_are_not_module_union(self):
        snapshot = self.store.snapshots()["printers"]
        snapshot["details"]["printers"][0]["capabilities"] = ["light"]
        self.store.snapshot("printers", snapshot)
        self.assertFalse(self.hub.printers()["printers"][0]["control_capabilities"]["pause"])
        with self.assertRaises(ValueError):
            self.hub.submit(self.payload())

    def test_confirmed_command_dispatched_once_and_job_bound(self):
        payload = self.payload()
        first, fresh = self.hub.submit(payload)
        self.assertTrue(fresh)
        self.hub.start()
        self.hub.command_queue.join()
        receipt = self.store.command(payload["request_id"])
        self.assertEqual((receipt["state"], receipt["detail"]), ("accepted", "state_confirmed"))
        replay, fresh = self.hub.submit(dict(reversed(list(payload.items()))))
        self.assertFalse(fresh)
        self.assertEqual(replay["id"], first["id"])
        calls = self.hub.adapters["printers"].calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["job_identity"], self.hub.adapters["printers"].identity)
        payload["action"] = "resume"
        with self.assertRaises(ValueError):
            self.hub.submit(payload)

    def test_job_changes_before_dispatch_never_send(self):
        payload = self.payload()
        self.hub.submit(payload)
        self.hub.adapters["printers"].identity = "run:" + "a" * 32 + ":task:2"
        self.hub.poll_once("printers")
        self.hub.start()
        self.hub.command_queue.join()
        self.assertEqual(self.store.command(payload["request_id"])["state"], "rejected")
        self.assertEqual(self.hub.adapters["printers"].calls, [])

    def test_explicit_destructive_confirmation_required(self):
        payload = self.payload("stop")
        with self.assertRaises(ValueError):
            self.hub.submit(payload)
        payload["confirm"] = "STOP"
        self.assertEqual(self.hub.submit(payload)[0]["state"], "queued")

    def test_crash_restart_marks_claimed_uncertain_and_never_resends(self):
        payload = self.payload()
        self.hub.submit(payload)
        self.store.set_command(payload["request_id"], state="claimed")
        self.store.close()
        self.store = Store(self.home / "state")
        self.hub.store = self.store
        self.assertEqual(self.store.command(payload["request_id"])["state"], "unknown")
        result, created = self.hub.submit(payload)
        self.assertFalse(created)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(self.hub.adapters["printers"].calls, [])

    def test_tombstones_prevent_replay_after_receipt_pruning(self):
        first = None
        for index in range(1002):
            ident = str(uuid.uuid4())
            value = {"id": ident, "printer_id": "one", "module_id": "printers", "action": "pause", "state": "accepted", "detail": "state_confirmed"}
            self.store.create_command(ident, "f" * 64, value)
            first = first or ident
        receipt = self.store.command(first)
        self.assertEqual(receipt["state"], "expired")
        same, created = self.store.create_command(first, "f" * 64, value)
        self.assertFalse(created)
        self.assertEqual(same["state"], "expired")
        with self.assertRaises(ValueError):
            self.store.known_command(first, "e" * 64)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM commands").fetchone()[0], 1000)

    def test_transient_failures_do_not_create_alerts_but_persistent_faults_do(self):
        snapshot = self.store.snapshots()["printers"]
        snapshot.update(available=False, failure_started_at=utc())
        self.store.snapshot("printers", snapshot)
        self.assertEqual(self.hub.alerts()["alerts"], [])
        snapshot["failure_started_at"] = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.store.snapshot("printers", snapshot)
        alerts = self.hub.alerts()["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["ai"]["status"], "disabled")
        self.hub.poll_once("printers")
        self.assertEqual(self.hub.alerts()["alerts"], [])
        self.assertEqual(self.hub.alerts(history=True)["alerts"][0]["state"], "resolved")

    def test_ingest_remote_only_no_control_escalation(self):
        snapshot = self.store.snapshots()["printers"]
        with self.assertRaises(ValueError):
            self.hub.ingest({"module_id": "printers", "snapshot": snapshot})
        self.hub.module_configs["printers"]["remote"] = True
        self.hub.ingest({"module_id": "printers", "snapshot": snapshot})
        self.assertEqual(self.store.snapshots()["printers"]["capabilities"], [])
        snapshot["observed_at"] = "2099-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            self.hub.ingest({"module_id": "printers", "snapshot": snapshot})

    def test_bad_ingest_shapes_cannot_poison_cached_modules(self):
        original = self.store.snapshots()["printers"]
        self.hub.module_configs["printers"]["remote"] = True
        for mutation in (lambda s: s.update(details=[]), lambda s: s.update(metrics=[]), lambda s: s.update(capabilities=[{}]), lambda s: s["details"].update(printers=["bad"]), lambda s: s["details"].update(streams=[{"state": {}, "bandwidth_kbps": "bad"}])):
            snapshot = deepcopy(original)
            mutation(snapshot)
            with self.assertRaises(ValueError):
                self.hub.ingest({"module_id": "printers", "snapshot": snapshot})
            self.assertEqual(self.store.snapshots()["printers"], original)

    def test_alert_actions_are_idempotent_and_archive_preserves_new_incident(self):
        snapshot = self.store.snapshots()["printers"]
        snapshot.update(available=False, failure_started_at="2020-01-01T00:00:00Z")
        self.store.snapshot("printers", snapshot)
        first = self.hub.alerts()
        ident = first["alerts"][0]["id"]
        payload = {"request_id": str(uuid.uuid4()), "action": "mark_read", "alert_id": ident}
        receipt = self.hub.alert_action(payload)
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual(self.hub.alert_action(payload), receipt)
        self.assertEqual(self.hub.alerts()["counts"]["unread"], 0)
        self.hub.alert_action({"request_id": str(uuid.uuid4()), "action": "snooze", "alert_id": ident, "minutes": 60})
        self.assertEqual(self.hub.alerts()["counts"]["attention"], 0)
        # A recovered and later recurring incident is not part of the old clear-all snapshot.
        self.hub.poll_once("printers")
        self.hub.alerts()
        snapshot["failure_started_at"] = "2021-01-01T00:00:00Z"
        self.store.snapshot("printers", snapshot)
        self.hub.alerts()
        result = self.hub.alert_action({"request_id": str(uuid.uuid4()), "action": "archive_all", "cutoff": first["archive_cutoff"]})
        self.assertEqual(result["archived_count"], 0)
        self.assertEqual(self.hub.alerts()["counts"]["total"], 1)


class HTTPTests(HubFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = create_server(self.hub, self.home, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port) + "/kindle-dashboard"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        super().tearDown()

    def request(self, path, role="reader", payload=None, headers=None):
        fields = {} if role is None else {"Authorization": "Bearer " + TOKENS[role + "_token"]}
        if payload is not None:
            fields["Content-Type"] = "application/json"
        fields.update(headers or {})
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=fields)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def test_public_health_and_ui_no_private_data(self):
        self.assertEqual(self.request("/healthz", None)[0], 200)
        status, body, headers = self.request("/", None)
        self.assertEqual(status, 200)
        self.assertNotIn(TOKENS["reader_token"].encode(), body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(b"localStorage", body)

    def test_private_routes_require_reader_and_ingest_is_not_reader(self):
        self.assertEqual(self.request("/api/v1/status", None)[0], 401)
        self.assertEqual(self.request("/api/v1/status", "ingest")[0], 401)
        self.assertEqual(self.request("/api/v1/status", "reader")[0], 200)
        self.assertEqual(self.request("/api/v1/status", "controller")[0], 200)
        self.assertEqual(self.request("/api/v1/commands", "reader", self.payload())[0], 401)

    def test_http_printer_command_feedback_round_trip(self):
        self.hub.start()
        payload = self.payload()
        self.assertEqual(self.request("/api/v1/commands", "controller", payload)[0], 202)
        self.hub.command_queue.join()
        code, body, _ = self.request("/api/v1/commands/" + payload["request_id"])
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["state"], "accepted")
        self.assertEqual(self.request("/api/v1/commands", "controller", payload)[0], 200)
        self.assertEqual(len(self.hub.adapters["printers"].calls), 1)

    def test_origin_and_host_are_restricted(self):
        self.assertEqual(self.request("/api/v1/commands", "controller", self.payload(), {"Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.request("/api/v1/status", headers={"Host": "attacker.invalid"})[0], 400)

    def test_rendered_png_is_full_color_and_private(self):
        self.assertEqual(self.request("/dashboard.png", None)[0], 401)
        for path in ("/dashboard.png", "/status/plex.png", "/status/llm.png", "/status/site.png", "/status/traffic.png"):
            status, body, headers = self.request(path)
            self.assertEqual(status, 200, path)
            image = Image.open(io.BytesIO(body))
            self.assertEqual((image.size, image.mode), ((1264, 1680), "RGB"))
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_native_read_envelopes_and_unconfigured_actions_are_explicit(self):
        for path in ("/api/v1/plex/status", "/api/v1/status/llm", "/api/v1/status/site", "/api/v1/status/traffic?window=5m&view=pages", "/api/v1/alerts"):
            status, body, _ = self.request(path)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["schema_version"], 1)
        self.assertEqual(self.request("/api/v1/doctor/one/analyze", "controller", {"a": 1})[0], 503)
        self.assertEqual(self.hub.adapters["printers"].calls, [])

    def test_remote_ingestion_auth_role(self):
        snapshot = self.store.snapshots()["printers"]
        self.hub.module_configs["printers"]["remote"] = True
        value = {"module_id": "printers", "snapshot": snapshot}
        self.assertEqual(self.request("/api/v1/ingest", "reader", value)[0], 401)
        self.assertEqual(self.request("/api/v1/ingest", "controller", value)[0], 401)
        self.assertEqual(self.request("/api/v1/ingest", "ingest", value)[0], 200)

    def test_camera_unconfigured_fails_clear_and_path_traversal_rejected(self):
        self.assertEqual(self.request("/api/v1/camera/one")[0], 503)
        self.assertEqual(self.request("/../secrets.json")[0], 400)
        self.assertEqual(self.request("/%2e%2e/secrets.json")[0], 400)


if __name__ == "__main__":
    unittest.main()
