"""Independent adversarial checks of hub boundaries and setup safety.

All fixtures are disposable: synthetic credentials, local SQLite and owned
loopback HTTP only. No production worker, secret store or remote API is used.
"""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from inkboard.config import ConfigError, initialize, load, validate
from inkboard.hub import Hub
from inkboard.server import create_server
from inkboard.store import Store, utc


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("peer_public_audit", ROOT / "tools" / "public_audit.py")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)
TOKENS = {"reader_token": "reader_" + "a" * 40, "controller_token": "controller_" + "b" * 40,
          "ingest_token": "ingest_" + "c" * 40}
JOB = "run:" + "1" * 32 + ":task:1"


def configuration(remote=False):
    return {"schema_version": 1, "name": "Peer fixture", "listen": {"host": "127.0.0.1", "port": 8787},
            "public_url": "", "refresh_seconds": 5, "stale_after_seconds": 15,
            "modules": [{"id": "printer", "type": "bambu", "name": "Synthetic printer", "remote": remote}]}


def observation():
    return {"id": "printer", "type": "bambu", "name": "Synthetic printer", "observed_at": utc(),
            "available": True, "summary": "Reporting", "metrics": {}, "capabilities": ["pause", "light"],
            "details": {"printers": [{"id": "one", "available": True, "state": "printing", "job_identity": JOB,
                         "observed_at": utc(), "progress": 25, "capabilities": ["pause", "light"]}]}}


class SyntheticAdapter:
    def __init__(self, module, secrets):
        self.document, self.calls = observation(), []
    def read(self): return deepcopy(self.document)
    def command(self, action, parameters, request_id):
        self.calls.append((action, parameters, request_id))
        return {"status": "confirmed", "message": "Synthetic state confirmed"}
    def close(self): pass


class PeerHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / "state")
        self.config = configuration()
        self.hub = Hub(self.config, TOKENS, self.store, SyntheticAdapter)
        self.hub.poll_once("printer")
    def tearDown(self):
        self.hub.close()
        self.store.close()
        self.temp.cleanup()
    def payload(self):
        return {"request_id": str(uuid.uuid4()), "printer_id": "one", "action": "pause", "job_identity": JOB}

    def test_malformed_adapter_does_not_replace_good_view_or_expose_exception(self):
        original = self.hub.printers()["printers"][0]
        adapter = self.hub.adapters["printer"]
        for mutation in (lambda d: d.update(details=[]), lambda d: d.update(capabilities=[{}]),
                         lambda d: d["details"].update(printers=["not a printer"]),
                         lambda d: d["details"]["printers"][0].update(progress=float("nan"))):
            adapter.document = observation()
            mutation(adapter.document)
            self.hub.poll_once("printer")
            row = self.hub.printers()["printers"][0]
            self.assertEqual(row["progress"], original["progress"])
            self.assertFalse(row["online"])
            self.assertFalse(row["control_link_ready"])
        def throws(): raise RuntimeError("private-value " + TOKENS["reader_token"])
        adapter.read = throws
        self.hub.poll_once("printer")
        self.assertNotIn(TOKENS["reader_token"], json.dumps(self.hub.snapshot()))
        self.assertNotIn("private-value", json.dumps(self.hub.snapshot()))

    def test_remote_ingest_cannot_grant_local_control(self):
        remote_config = configuration(remote=True)
        remote = Hub(remote_config, TOKENS, self.store, SyntheticAdapter)
        try:
            remote.ingest({"module_id": "printer", "snapshot": observation()})
            document = remote.snapshot()["modules"][0]
            self.assertEqual(document["capabilities"], [])
            self.assertEqual(document["details"]["printers"][0]["capabilities"], [])
            self.assertFalse(remote.printers()["printers"][0]["control_link_ready"])
            with self.assertRaises(ValueError): remote.submit(self.payload())
        finally: remote.close()

    def test_remote_malformed_envelope_and_backwards_observation_preserve_previous(self):
        remote = Hub(configuration(remote=True), TOKENS, self.store, SyntheticAdapter)
        try:
            remote.ingest({"module_id": "printer", "snapshot": observation()})
            initial = self.store.snapshots()["printer"]
            for malformed in ({**observation(), "details": []}, {**observation(), "capabilities": [{}]},
                              {**observation(), "observed_at": "2000-01-01T00:00:00Z"}):
                with self.assertRaises(ValueError): remote.ingest({"module_id": "printer", "snapshot": malformed})
                self.assertEqual(self.store.snapshots()["printer"], initial)
        finally: remote.close()

    def test_queued_intent_expires_without_late_physical_operation(self):
        payload = self.payload()
        self.hub.submit(payload)
        self.store.set_command(payload["request_id"], created_at=(datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat())
        worker = threading.Thread(target=self.hub._commands, daemon=True)
        worker.start()
        self.hub.command_queue.join()
        self.hub.stop_event.set()
        worker.join(2)
        self.assertIn(self.store.command(payload["request_id"])["state"], ("expired", "rejected"))
        self.assertEqual(self.hub.adapters["printer"].calls, [])

    def test_generic_module_route_cannot_omit_print_identity(self):
        payload = {"request_id": str(uuid.uuid4()), "module_id": "printer", "action": "pause",
                   "parameters": {"printer_id": "one"}}
        with self.assertRaises(ValueError): self.hub.submit(payload)
        self.assertTrue(self.hub.command_queue.empty())

    def test_duplicate_printer_ids_are_not_an_ambiguous_command_target(self):
        adapter = self.hub.adapters["printer"]
        adapter.document["details"]["printers"].append(deepcopy(adapter.document["details"]["printers"][0]))
        self.hub.poll_once("printer")
        with self.assertRaises(ValueError): self.hub.submit(self.payload())
        self.assertEqual(adapter.calls, [])

    def test_role_tokens_and_provider_secrets_never_enter_public_snapshot(self):
        for value in TOKENS.values(): self.assertNotIn(value, json.dumps(self.hub.snapshot()))
        self.assertNotIn("secrets", self.hub.snapshot())

    def test_future_or_naive_timestamps_never_grant_control(self):
        for observed in ("2999-01-01T00:00:00Z", "2026-01-01T00:00:00"):
            self.hub.adapters["printer"].document["observed_at"] = observed
            self.hub.poll_once("printer")
            with self.assertRaises(ValueError): self.hub.submit(self.payload())


class PeerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state")
        self.hub = Hub(configuration(remote=True), TOKENS, self.store, SyntheticAdapter)
        self.server = create_server(self.hub, Path(self.temp.name), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.hub.close()
        self.store.close()
        self.temp.cleanup()
    def request(self, path, role=None, method="GET", payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        values = {} if role is None else {"Authorization": "Bearer " + TOKENS[role + "_token"]}
        values.update(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload)
            values["Content-Type"] = "application/json"
        try:
            connection.request(method, "/kindle-dashboard" + path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, response.read(300000)
        finally: connection.close()

    def test_ingest_role_cannot_read_or_control_and_reader_cannot_ingest(self):
        self.assertEqual(self.request("/api/v1/modules", "ingest")[0], 401)
        payload = {"module_id": "printer", "snapshot": observation()}
        for role in ("reader", "controller"):
            self.assertEqual(self.request("/api/v1/ingest", role, "POST", payload)[0], 401)
        self.assertEqual(self.request("/api/v1/ingest", "ingest", "POST", payload)[0], 200)
        command = {"request_id": str(uuid.uuid4()), "printer_id": "one", "action": "pause", "job_identity": JOB}
        self.assertEqual(self.request("/api/v1/commands", "ingest", "POST", command)[0], 401)
        self.assertEqual(self.request("/api/v1/commands", "reader", "POST", command)[0], 401)

    def test_unconfigured_action_does_not_fall_through_to_any_worker(self):
        status, body = self.request("/api/v1/plex/commands", "controller", "POST", {"action": "delete_media"})
        self.assertEqual(status, 503)
        self.assertIn(b"nothing was sent", body)
        self.assertTrue(self.hub.command_queue.empty())

    def test_wrong_host_and_cross_origin_write_are_rejected(self):
        self.assertEqual(self.request("/api/v1/modules", "reader", headers={"Host": "attacker.example.com"})[0], 400)
        status, _ = self.request("/api/v1/ingest", "ingest", "POST",
            {"module_id": "printer", "snapshot": observation()}, {"Origin": "https://attacker.example.com"})
        self.assertEqual(status, 403)

    def test_unauthenticated_shell_contains_no_generated_credentials(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertTrue(all(value.encode() not in body for value in TOKENS.values()))
        self.assertEqual(self.request("/api/client-update/manifest")[0], 401)


class PeerPublicationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Uses real Windows ACLs on disposable synthetic files")
    def test_explicit_everyone_read_on_synthetic_secret_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private"
            initialize(path, demo=True)
            target = path / "secrets.json"
            # These are newly generated inert fixture tokens, never a service's
            # credentials. Only this disposable file gets the adversarial ACE.
            result = subprocess.run(["icacls", str(target), "/grant", "*S-1-1-0:R"],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            with self.assertRaises(ConfigError): load(path)

    @unittest.skipUnless(os.name == "nt", "Uses real Windows ACLs on a disposable empty database")
    def test_explicit_everyone_read_on_synthetic_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            database = Store(directory)
            database.close()
            result = subprocess.run(["icacls", str(directory / "inkboard.sqlite3"), "/grant", "*S-1-1-0:R"],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            with self.assertRaises(ConfigError): Store(directory)

    def test_staged_secret_cannot_hide_behind_clean_worktree_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, timeout=10)
            path = root / "module.py"
            path.write_text("fixture = '" + "gh" + "p_" + "Z" * 36 + "'\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "module.py"], check=True, capture_output=True, timeout=10)
            path.write_text("fixture = 'clean'\n", encoding="utf-8")
            _, issues = AUDIT.audit(root, staged=True)
            self.assertTrue(issues)
            self.assertNotIn("Z" * 36, str(issues))

    def test_repeat_setup_cannot_overwrite_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private"
            initialize(path, demo=True)
            before = (path / "secrets.json").read_bytes()
            with self.assertRaises(ConfigError): initialize(path, demo=True)
            self.assertEqual((path / "secrets.json").read_bytes(), before)
            self.assertEqual(len(set(load(path)[1].values())), 3)


class PeerBootstrapTests(unittest.TestCase):
    def fixture(self):
        calls = []
        class Bootstrap:
            def read(self):
                raise AssertionError("The one-shot path must wait for an asynchronous first observation")
            def wait_ready(self, timeout):
                calls.append(("wait", timeout))
                return observation()
            def command(self, *args):
                raise AssertionError("A read-only connection check must not issue a command")
            def close(self):
                calls.append(("close",))
        return Bootstrap(), calls

    def test_connect_check_waits_for_first_mqtt_observation_before_close(self):
        from inkboard.cli import check_setup
        adapter, calls = self.fixture()
        with patch("inkboard.integrations.create_adapter", return_value=adapter):
            with redirect_stdout(io.StringIO()):
                self.assertTrue(check_setup(configuration(), TOKENS, connect=True))
        self.assertEqual(calls, [("wait", 8), ("close",)])

    def test_one_shot_collector_waits_before_publishing_and_closes(self):
        from inkboard.collector import collect
        adapter, calls = self.fixture()
        with patch("inkboard.collector.create_adapter", return_value=adapter):
            with patch("inkboard.collector.push") as push:
                with redirect_stdout(io.StringIO()):
                    result = collect(configuration(), TOKENS, "https://hub.example.com/kindle-dashboard", once=True)
        self.assertEqual(result, 0)
        self.assertEqual(calls, [("wait", 8), ("close",)])
        self.assertEqual(push.call_count, 1)
        self.assertEqual(push.call_args.args[2], "printer")
        self.assertTrue(push.call_args.args[3]["available"])


if __name__ == "__main__": unittest.main()
