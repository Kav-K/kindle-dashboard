"""Portable package, security and full native-client contract fixtures.

Every fixture is synthetic. No dashboard services or devices are contacted.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from copy import deepcopy
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_kindle import BuildError, build, render_client, validate_profile, MAX_FILE


def profile():
    return {"public_url": "https://dashboard.example.net/kindle-dashboard", "kindle": {
        "serial": "TEST000000000001", "printer_ids": ["printer-a", "printer-b"], "timezone": "UTC"}}


class BuildTests(unittest.TestCase):
    def test_manifest_hashes_all_three_exact_files_and_private_helper(self):
        files, helper, manifest = render_client(profile())
        self.assertEqual(set(files), {"main.lua", "_meta.lua", "VERSION"})
        for item in manifest["files"]:
            self.assertEqual(item["size"], len(files[item["name"]]))
            self.assertEqual(item["sha256"], hashlib.sha256(files[item["name"]]).hexdigest())
        self.assertIn(hashlib.sha256(helper).hexdigest().encode(), files["main.lua"])
        self.assertLessEqual(len(files["main.lua"]), MAX_FILE)
        self.assertLessEqual(len(helper), 32768)

    def test_build_is_byte_reproducible_and_ota_never_contains_tokens(self):
        secrets = {"reader_token": "r" * 43, "controller_token": "c" * 43, "ingest_token": "i" * 43}
        with tempfile.TemporaryDirectory() as tmp:
            outputs = [Path(tmp) / "a", Path(tmp) / "b"]
            for out in outputs:
                build(profile(), out, secrets=secrets)
                self.assertEqual((out / "usb/inkboard-install/token").read_text().strip(), secrets["reader_token"])
                for file in (out / "client_updates").rglob("*"):
                    if file.is_file():
                        self.assertNotIn(secrets["reader_token"].encode(), file.read_bytes())
                        self.assertNotIn(secrets["controller_token"].encode(), file.read_bytes())
            self.assertEqual((outputs[0] / "inkboard-usb.zip").read_bytes(), (outputs[1] / "inkboard-usb.zip").read_bytes())
            with zipfile.ZipFile(outputs[0] / "inkboard-usb.zip") as archive:
                self.assertIn("extensions/inkboard/menu.json", archive.namelist())
                self.assertIn("documents/InstallInkboard.sh", archive.namelist())
                self.assertFalse(any(name.startswith("/") or ".." in name.split("/") for name in archive.namelist()))

    def test_fresh_output_only_and_no_file_on_invalid_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "package"
            config = profile(); config["kindle"]["serial"] = "invalid"
            with self.assertRaises(BuildError): build(config, out)
            self.assertFalse(out.exists())
            build(profile(), out)
            before = (out / "build-receipt.json").read_bytes()
            with self.assertRaises(BuildError): build(profile(), out)
            self.assertEqual(before, (out / "build-receipt.json").read_bytes())

    def test_url_injection_and_tls_downgrades_rejected(self):
        invalid = ["http://dashboard.example.net/kindle-dashboard", "https://127.0.0.1/kindle-dashboard",
                   "https://user:pass@dashboard.example.net/kindle-dashboard", "https://dashboard.example.net:444/kindle-dashboard",
                   "https://dashboard.example.net/kindle-dashboard?secret=x", "https://dashboard.example.net/kindle-dashboard#x",
                   'https://dashboard.example.net/\\";os.execute("x")', "https://dashboard..example.net/kindle-dashboard",
                   "https://Dashboard.example.net/kindle-dashboard", "https://dashboard.example.net/../evil"]
        for url in invalid:
            with self.subTest(url=url):
                config = profile(); config["public_url"] = url
                with self.assertRaises(BuildError): validate_profile(config)

    def test_profile_fields_and_versions_are_not_lua_injection(self):
        for key, values in {"serial": ["", "x" * 16, 'TEST00000000000"'], "printer_ids": [["a", "a"], ["../x"], ["a", "b", "c"], [True]],
                            "timezone": ["../../UTC", "UTC\n", 'UTC"'], "features": [{"doctor": "yes"}, {"arbitrary": True}]}.items():
            for value in values:
                config = profile(); config["kindle"][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(BuildError): validate_profile(config)
        for version in ("latest", "1.2.3\n", '1.2.3"', "1.2.3/../../"):
            with self.assertRaises(BuildError): render_client(profile(), version)

    def test_configurations_bind_device_tls_and_printer_identity_independently(self):
        a = profile(); b = profile()
        b["kindle"]["serial"] = "TEST000000000002"
        b["kindle"]["printer_ids"] = ["another", "spare"]
        b["public_url"] = "https://another.example.org/kindle-dashboard"
        af, ah, am = render_client(a); bf, bh, bm = render_client(b)
        self.assertNotEqual(ah, bh); self.assertNotEqual(af["main.lua"], bf["main.lua"])
        self.assertNotEqual(am["device_serial"], bm["device_serial"])
        for old in (b"TEST000000000001", b"dashboard.example.net", b"printer-a"):
            self.assertNotIn(old, bf["main.lua"])
        self.assertIn(b"https://another.example.org/kindle-dashboard", bf["main.lua"])

    def test_optional_module_flags_default_off(self):
        files, _, _ = render_client(profile())
        self.assertIn(b"doctor=false", files["main.lua"])
        config = profile(); config["kindle"]["features"] = {"doctor": True}
        self.assertIn(b"doctor=true", render_client(config)[0]["main.lua"])

    def test_token_input_is_separate_bounded_and_never_echoed_in_error(self):
        for secrets in ({}, {"reader_token": "x" * 43, "controller_token": "x" * 43},
                        {"reader_token": "x\n" * 30, "controller_token": "y" * 43}):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(BuildError) as caught: build(profile(), Path(tmp) / "out", secrets=secrets)
                self.assertNotIn("x" * 43, str(caught.exception))

    def test_kindles_use_hub_compatible_url_safe_token_subset(self):
        from inkboard.config import valid_role_token
        for length in (32, 43, 256):
            tokens = {"reader_token": "r" * (length - 2) + "_-", "controller_token": "c" * length}
            self.assertTrue(all(valid_role_token(value) for value in tokens.values()))
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "package"
                build(profile(), output, secrets=tokens)
                self.assertEqual((output / "usb/inkboard-install/token").read_text().strip(), tokens["reader_token"])
        for reader in ("r" * 31, "r" * 257, "r" * 32 + "!", "r" * 32 + "\"", "r" * 32 + "$"):
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "package"
                with self.subTest(length=len(reader)), self.assertRaises(BuildError) as error:
                    build(profile(), output, secrets={"reader_token": reader, "controller_token": "c" * 43})
                self.assertIn("32–256", str(error.exception))
                self.assertNotIn(reader, str(error.exception))
                self.assertFalse(output.exists())

    def test_cli_errors_do_not_echo_credential_or_source_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"; bad = Path(tmp) / "private.json"
            config.write_text(json.dumps(profile())); bad.write_text('{"secret":"do-not-echo-this" INVALID')
            result = subprocess.run([sys.executable, str(ROOT / "tools/build_kindle.py"), "--config", str(config),
                                     "--secrets", str(bad), "--out", str(Path(tmp) / "out")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("do-not-echo-this", result.stderr + result.stdout)


try:
    spec = importlib.util.spec_from_file_location("inkboard_native_harness", ROOT / "native/tests/harness.py")
    HARNESS = importlib.util.module_from_spec(spec); spec.loader.exec_module(HARNESS)
except ImportError:
    HARNESS = None


@unittest.skipIf(HARNESS is None, "Install .[test] for full LuaJIT contract tests")
class NativeContracts(unittest.TestCase):
    def setUp(self):
        self.files, self.helper, _ = render_client(profile())
        self.client = HARNESS.Client(self.files["main.lua"].decode())

    def test_full_client_loads_without_any_host_io_and_keeps_feature_methods(self):
        for name in ("showSelfHealth", "setBrightnessLevel", "showEverydaySettings", "showCamera", "sendCommand", "showPlexStreams",
                     "showDoctorMenu", "renderAlerts", "showTrafficMenu", "checkWifiUpdate"):
            self.assertIsNotNone(self.client.kind[name], name)
        self.assertEqual(len(self.client.h.requests), 0)

    def test_actual_native_printer_parser_accepts_the_hub_wire_contract(self):
        self.client.respond({"relay_age_seconds": 1, "printers": [{"id": "printer-a", "name": "Workshop", "job_name": "Fixture",
            "job_identity": "run:" + "a" * 32 + ":task:1", "state": "printing", "online": True, "stale": False,
            "control_link_ready": True, "control_capabilities": {"pause": True, "stop": True, "speed": True}, "chamber_light": "on", "speed_level": 2}]})
        printers, reason, _ = self.client.call("getPrinters")
        self.assertIsNone(reason); self.assertEqual(printers[1].name, "Workshop")
        self.assertTrue(printers[1].control_capabilities.pause)
        self.assertEqual(self.client.h.requests[1].path, "/api/v1/status")

    def test_stale_printer_never_claims_ready_controls(self):
        self.client.respond({"relay_age_seconds": 31, "printers": [{"id": "printer-a", "name": "Workshop", "online": True,
             "stale": False, "control_link_ready": True, "control_capabilities": {"pause": True}}]})
        printers, _, _ = self.client.call("getPrinters")
        self.assertFalse(printers[1].can_control); self.assertFalse(printers[1].control_capabilities.pause)

    def test_native_command_receipts_distinguish_delivery_from_reported_outcome(self):
        pending = self.client.table({"id": "10000000-0000-4000-8000-000000000001", "printer_id": "printer-a", "action": "pause"})
        result = {"id": pending.id, "printer_id": pending.printer_id, "action": pending.action, "state": "accepted", "detail": "delivery_acknowledged"}
        self.assertTrue(self.client.call("applyPrinterCommandResult", pending, self.client.table(result)))
        self.assertEqual(pending.status, "uncertain"); self.assertTrue(pending.locked)
        result["detail"] = "state_confirmed"
        self.assertTrue(self.client.call("applyPrinterCommandResult", pending, self.client.table(result)))
        self.assertEqual(pending.status, "confirmed"); self.assertFalse(pending.locked)

    def test_other_request_identity_cannot_confirm_a_saved_command(self):
        pending = self.client.table({"id": "10000000-0000-4000-8000-000000000001", "printer_id": "printer-a", "action": "pause"})
        result = self.client.table({"id": pending.id, "printer_id": "printer-b", "action": "pause", "state": "accepted", "detail": "state_confirmed"})
        self.assertFalse(self.client.call("applyPrinterCommandResult", pending, result)); self.assertIsNone(pending.status)

    def test_actual_native_empty_alert_schema_has_a_valid_view(self):
        self.client.respond({"schema_version": 1, "generated_at": "2027-01-15T00:00:00Z",
            "quiet_hours": {"enabled": False, "start_hour": 22, "end_hour": 8, "timezone": "UTC", "active": False},
            "counts": {"unread": 0, "open": 0, "total": 0, "attention": 0}, "source_status": [], "alerts": []})
        context = self.client.call("getAlerts")
        self.assertIsNone(context.failure); self.assertEqual(context.data.counts.total, 0)

    def test_actual_native_site_and_traffic_routes_are_generic(self):
        self.client.respond({"schema_version": 1, "key": "site", "services": [], "metrics": {}, "top_paths": []})
        data = self.client.call("getStatusDetails", "site")
        self.assertEqual(data.key, "site"); self.assertEqual(self.client.h.requests[1].path, "/api/v1/status/site")
        self.client.respond({"schema_version": 1, "key": "traffic", "source": "origin", "window": "5m", "view": "pages"})
        self.assertEqual(self.client.call("getTrafficDetails", 1).key, "traffic")

    def test_self_health_uses_local_battery_and_no_polling(self):
        state = self.client.call("selfHealthState")
        self.assertEqual(state.capacity, 87); self.assertEqual(state.wifi, "Connected")
        self.assertEqual(len(self.client.h.requests), 0)

    def test_unconfigured_advanced_module_explains_itself_without_request(self):
        self.client.call("showDoctorMenu")
        self.assertIn("not configured", self.client.h.notices[1]); self.assertEqual(len(self.client.h.requests), 0)

    def test_actual_public_printer_card_hit_regions_match_scaled_renderer(self):
        self.client.lua.globals()["__app"] = self.client.app
        self.client.lua.execute('''
function __app:buildPrinterMenu()return {{printer={id='printer-a'}},{printer={id='printer-b'}}}end
function __app:showPrinterControls(id)__h.opened=id end
function __app:showQuickControls()__h.opened='menu'end
__app.view_kind='png';__app.image_rect={x=20,y=100,w=632,h=840,png_w=1264,png_h=1680}
''')
        for y, expected in ((238, "printer-a"), (787, "printer-a"), (788, "menu"),
                            (814, "printer-b"), (1363, "printer-b"), (1364, "menu"), (1540, "menu")):
            position = self.client.table({"x": 20 + 70 / 2, "y": 100 + y / 2})
            self.client.call("showPrinterActionAt", position)
            self.assertEqual(self.client.h.opened, expected)

    def test_native_http_request_guard_validates_flat_job_binding(self):
        self.client.h.offline = True
        ident = "10000000-0000-4000-8000-000000000001"
        command = {"request_id": ident, "printer_id": "printer-a", "action": "pause",
                   "job_identity": "run:" + "a" * 32 + ":task:1"}
        self.client.respond(command)
        actual = self.client.kind.requestRemote
        body, reason = actual(self.client.app, "/api/v1/commands", "POST", "/inert-controller", "fixture", "application/json", 8192)
        self.assertIsNone(body); self.assertEqual(reason, "Wi-Fi is disconnected")
        for bad in ("run:a:task:1", "run:" + "a" * 32 + ":task:0", "run:" + "a" * 32 + ":task:1/../", 10):
            command["job_identity"] = bad; self.client.respond(command)
            body, reason = actual(self.client.app, "/api/v1/commands", "POST", "/inert-controller", "fixture", "application/json", 8192)
            self.assertIsNone(body); self.assertEqual(reason, "Invalid job binding")

    def test_actual_native_pause_submission_serializes_observed_job_identity(self):
        identity = "run:" + "a" * 32 + ":task:1"
        self.client.respond({"relay_age_seconds": 0, "printers": [{"id": "printer-a", "name": "Fixture", "job_name": "Synthetic job",
            "state": "printing", "online": True, "stale": False, "job_identity": identity,
            "control_link_ready": True, "control_capabilities": {"pause": True}}]})
        printers, _, _ = self.client.call("getPrinters")
        self.client.app.settings.base_url = profile()["public_url"]
        self.client.lua.globals()["__app"] = self.client.app
        self.client.lua.execute('''
io.open=function(path)assert(path=='/inert-controller');return {read=function()return string.rep('a',43)end,close=function()end}end
function __app:closePrinterControlChildren()end
function __app:refreshPrinterFeedbackMenu()end
''')
        self.client.call("sendCommand", printers[1], "pause")
        self.assertEqual(self.client.h.encoded.job_identity, identity)
        self.assertEqual(self.client.h.encoded.printer_id, "printer-a")
        self.assertEqual(self.client.h.encoded.action, "pause")

    def test_public_native_refuses_all_legacy_plaintext_or_proxy_routes(self):
        self.client.app.settings.base_url = profile()["public_url"]
        actual = self.client.kind.requestRemote
        for route in ("direct", "tailnet", "auto"):
            self.client.app.settings.proxy_mode = route
            body, reason = actual(self.client.app, "/api/v1/status", "GET", "/inert-reader", None, "application/json", 8192)
            self.assertIsNone(body)
            self.assertEqual(reason, "URL does not match the selected connection route")

    def test_actual_native_tls_factory_checks_chain_dns_san_and_origin(self):
        self.client.lua.globals()["__class"] = self.client.kind
        self.client.lua.execute('''
local factory
for n=1,100 do local name,value=debug.getupvalue(__class.requestRemote,n)
if not name then break end;if name=='verified_https_factory'then factory=value;break end end
assert(factory,'TLS factory missing')
function __h.tls_case(chain,san,host)
local cert={extensions=function()return {['2.5.29.17']={dNSName={san}}}end}
local conn={sock={getpeerverification=function()return chain end,getpeercertificate=function()return cert end},
connect=function()return true end,close=function()end}
local https={tcp=function(options)__h.tls_options=options;return function()return conn end end}
local make=factory(https,'/inert-ca-bundle');return make():connect(host,443)
end
''')
        self.assertTrue(self.client.h.tls_case(True, "dashboard.example.net", "dashboard.example.net"))
        self.assertEqual(self.client.h.tls_options.verify, "peer")
        self.assertEqual(self.client.h.tls_options.cafile, "/inert-ca-bundle")
        for chain, san, host in ((False, "dashboard.example.net", "dashboard.example.net"),
                                 (True, "other.example.net", "dashboard.example.net"),
                                 (True, "dashboard.example.net", "other.example.net")):
            result = self.client.h.tls_case(chain, san, host)
            self.assertIsInstance(result, tuple)
            self.assertIsNone(result[0])

    def test_native_device_identity_read_is_bounded_and_normalizes_procfs_terminator(self):
        self.client.lua.globals()["__class"] = self.client.kind
        self.client.lua.execute('''
local read_serial
for n=1,100 do local name,value=debug.getupvalue(__class.checkWifiUpdate,n)
if not name then break end;if name=='device_serial'then read_serial=value;break end end
assert(read_serial,'Device identity reader missing')
function __h.serial_case(value)
io.open=function(path,mode)
assert(path=='/proc/usid' and mode=='r')
return {read=function(_,limit)assert(limit==129);return value end,close=function()end}
end
return read_serial()
end
''')
        value = self.client.h.serial_case("TEST000000000001\x00\r\n")
        self.assertEqual(value[0] if isinstance(value, tuple) else value, "TEST000000000001")
        self.assertIsNone(self.client.h.serial_case("x" * 129))


@unittest.skipIf(HARNESS is None, "Install the test extra for full LuaJIT HTTP contract tests")
class NativeHttpContracts(unittest.TestCase):
    """Actual disposable HTTP hub responses consumed by the complete Lua client."""
    def setUp(self):
        sys.path.insert(0, str(ROOT / "src"))
        from inkboard.hub import Hub
        from inkboard.server import Server
        from inkboard.store import Store, utc
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.config = profile()
        self.config.update(name="Synthetic dashboard", refresh_seconds=15, stale_after_seconds=60,
                           modules=[{"id": "demo", "type": "demo", "name": "Demo printers", "enabled": True}])
        self.secrets = {"reader_token": "r" * 43, "controller_token": "c" * 43, "ingest_token": "i" * 43}
        self.store = Store(self.directory / "state")
        self.hub = Hub(self.config, self.secrets, self.store)
        self.hub.poll_once("demo")
        self.server = Server(("127.0.0.1", 0), self.hub, self.directory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = HARNESS.Client(render_client(self.config)[0]["main.lua"].decode())
        self.client.lua.globals()["__http_fixture"] = self.transport
        self.client.lua.execute('''function __h.connect_http(app)
function app:requestRemote(path,method,token,payload)
local status,value=__http_fixture(path,method,token,payload)
__h.payload=value;return 'fixture-http',status
end end''')
        self.client.h.connect_http(self.client.app)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)
        self.hub.close()
        self.store.db.close()
        self.tmp.cleanup()

    def transport(self, path, method, token_path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        role = "controller_token" if token_path == "/inert-controller" else "reader_token"
        try:
            connection.request(method, "/kindle-dashboard" + path, body=payload,
                headers={"Authorization": "Bearer " + self.secrets[role], "Content-Type": "application/json"})
            response = connection.getresponse(); raw = response.read()
            value = json.loads(raw)
            return response.status, self.client.table(value)
        finally:
            connection.close()

    def test_real_http_demo_printers_and_site_details_validate_in_native(self):
        printers, reason, _ = self.client.call("getPrinters")
        self.assertIsNone(reason); self.assertEqual(len(printers), 2)
        self.assertFalse(printers[1].control_capabilities.pause)
        for key in ("llm", "site"):
            data = self.client.call("getStatusDetails", key)
            self.assertEqual(data.key, key)

    def test_real_http_empty_alert_inbox_is_not_an_unavailable_state(self):
        context = self.client.call("getAlerts")
        self.assertIsNone(context.failure)
        self.assertEqual(context.data.schema_version, 1)

    def test_real_http_traffic_and_unconfigured_plex_have_typed_views(self):
        self.assertEqual(self.client.call("getTrafficDetails", 1).key, "traffic")
        state = self.client.call("getPlexStatus")
        self.assertEqual(state.schema_version, 1)

    def test_real_http_plex_projection_validates_now_playing_and_freshness(self):
        from inkboard.store import utc
        module = {"id": "media", "type": "plex", "name": "Fixture media", "enabled": True}
        self.hub.module_configs["media"] = module
        self.store.snapshot("media", {**module, "observed_at": utc(), "available": True,
            "capabilities": [], "metrics": {}, "details": {"plex_available": True, "streams": [
                {"id": "fixture-stream", "state": "playing", "title": "Synthetic film", "type": "movie",
                 "player": "Fixture", "position_ms": 60000, "duration_ms": 600000,
                 "progress": 10, "transcoding": True, "transcode_progress": 20,
                 "bandwidth_kbps": 1500, "location": "local"}]}})
        context = self.client.call("getPlexNowPlaying")
        self.assertIsNone(context.failure)
        self.assertTrue(self.client.call("plexNowPlayingFresh", context))
        self.assertEqual(context.state.plex.now_playing.sessions[1].title, "Synthetic film")
        self.assertFalse(context.state.plex.now_playing.sessions[1].can_stop)

    def test_real_http_degraded_source_alert_validates_without_ai_or_duplicate_failure(self):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
        current = self.store.snapshots()["demo"]
        current.update(available=False, observed_at=old, failure_started_at=old)
        self.store.snapshot("demo", current)
        first = self.client.call("getAlerts")
        second = self.client.call("getAlerts")
        self.assertIsNone(first.failure); self.assertIsNone(second.failure)
        self.assertEqual(first.data.counts.total, 1)
        self.assertEqual(second.data.alerts[1].id, first.data.alerts[1].id)
        self.assertEqual(first.data.alerts[1].ai.status, "disabled")

    def test_real_http_alert_ack_and_clear_receipts_complete_in_native_without_sticking(self):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
        current = self.store.snapshots()["demo"]
        current.update(available=False, observed_at=old, failure_started_at=old)
        self.store.snapshot("demo", current)
        context = self.client.call("getAlerts")
        payload = {"request_id": "10000000-0000-4000-8000-000000000011", "action": "acknowledge", "alert_id": context.data.alerts[1].id}
        pending = self.client.table({"schema": 1, "payload": payload, "started_at": 1800000000})
        self.client.app.settings.alert_action_receipt = pending
        code, result = self.transport("/api/v1/alerts/actions", "POST", "/inert-controller", json.dumps(payload))
        self.assertEqual(code, 200)
        self.assertTrue(self.client.call("applyAlertReceipt", result, pending))
        self.assertIsNone(self.client.app.settings.alert_action_receipt)
        context = self.client.call("getAlerts")
        self.assertTrue(context.data.alerts[1].acknowledged)
        payload = {"request_id": "10000000-0000-4000-8000-000000000012", "action": "archive_all", "cutoff": context.data.archive_cutoff}
        pending = self.client.table({"schema": 1, "payload": payload, "started_at": 1800000000})
        self.client.app.settings.alert_action_receipt = pending
        code, result = self.transport("/api/v1/alerts/actions", "POST", "/inert-controller", json.dumps(payload))
        self.assertEqual(code, 200)
        self.assertTrue(self.client.call("applyAlertReceipt", result, pending))
        self.assertIsNone(self.client.app.settings.alert_action_receipt)
        self.assertEqual(self.client.call("getAlerts").data.counts.total, 0)
        self.assertEqual(self.client.call("getAlertHistory").data.counts.total, 1)

    def test_real_http_ota_bytes_equal_the_built_manifest(self):
        out = self.directory / "package"
        built = build(self.config, out)
        # The service serves this one selected, complete build, never the USB secrets.
        import shutil
        shutil.copytree(out / "client_updates", self.directory / "client_updates")
        code, observed = self.transport("/api/client-update/manifest", "GET", "/inert-reader", None)
        self.assertEqual(code, 200); self.assertEqual(observed.device_serial, built["device_serial"])
        for item in built["files"]:
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
            try:
                connection.request("GET", "/kindle-dashboard/api/client-update/file/" + built["version"] + "/" + item["name"],
                                   headers={"Authorization": "Bearer " + self.secrets["reader_token"]})
                response = connection.getresponse(); raw = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
                self.assertEqual(len(raw), item["size"])
            finally:
                connection.close()


def load_tests(loader, standard, pattern):
    # Retain the API2 state-machine and native-adapter failure matrices as public,
    # self-contained fixtures. These execute only in-memory I/O/FFI substitutes.
    if HARNESS is not None:
        for name in ("test_ota_fsm", "test_ota_native", "test_first_install"):
            spec = importlib.util.spec_from_file_location(name, ROOT / "native/tests" / (name + ".py"))
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            standard.addTests(loader.loadTestsFromModule(module))
    return standard


if __name__ == "__main__":
    unittest.main(verbosity=2)
