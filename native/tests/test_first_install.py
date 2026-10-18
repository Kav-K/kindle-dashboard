"""Execute first-install shell against a disposable synthetic Kindle tree.

The only adaptations replace absolute device paths, root identity and sync.
Package hashing, copies, refusals and credential transfer run as real commands.
No real Kindle, /var/local, process or system service is accessed.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("first_install_builder", ROOT / "tools/build_kindle.py")
builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
def choose_shell():
    candidates = [shutil.which("sh")]
    if os.name == "nt" and os.environ.get("ProgramFiles"):
        candidates.append(str(Path(os.environ["ProgramFiles"]) / "Git/usr/bin/sh.exe"))
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        env = dict(os.environ, PATH=str(Path(candidate).parent) + os.pathsep + os.environ.get("PATH", ""))
        probe = subprocess.run([candidate, "-c", "command -v sha256sum >/dev/null && command -v cmp >/dev/null && command -v find >/dev/null"],
                               env=env, capture_output=True, timeout=5)
        if probe.returncode == 0:
            return candidate
    return None


SHELL = choose_shell()


@unittest.skipUnless(SHELL, "A POSIX shell and coreutils are required for first-install fixtures")
class FirstInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.us = self.base / "mnt/us"; self.local = self.base / "var/local"
        self.local.mkdir(parents=True); (self.us / "koreader/plugins").mkdir(parents=True)
        self.usid = self.base / "usid"; self.usid.write_text("TEST000000000001\n")
        config = {"public_url": "https://dashboard.example.net/kindle-dashboard", "kindle": {"serial": "TEST000000000001"}}
        self.output = self.base / "package"
        builder.build(config, self.output, secrets={"reader_token": "reader_" * 8, "controller_token": "control_" * 8})
        shutil.copytree(self.output / "usb", self.us, dirs_exist_ok=True)
        self.install = self.us / "inkboard-install"
        self.active = self.us / "koreader/plugins/kindleprinter.koplugin"
        self.private = self.local / "kindle-printer-dashboard"
        body = (self.install / "install.sh").read_text(encoding="utf-8")
        body = body.replace("/proc/usid", self.usid.as_posix()).replace("/mnt/us", self.us.as_posix()).replace("/var/local", self.local.as_posix())
        self.script = self.base / "fixture-install.sh"
        self.script.write_text("id(){ printf '0\\n'; }; sync(){ :; }\n" + body, encoding="utf-8", newline="\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self):
        env = dict(os.environ, PATH=str(Path(SHELL).parent) + os.pathsep + os.environ.get("PATH", ""))
        return subprocess.run([SHELL, str(self.script)], env=env, capture_output=True, text=True, timeout=15)

    def test_verified_install_transfers_credentials_removes_handoff_and_is_idempotent(self):
        first = self.run_install(); self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        for name in ("_meta.lua", "main.lua", "VERSION"):
            self.assertEqual((self.active / name).read_bytes(), (self.install / "kindleprinter.koplugin" / name).read_bytes())
        self.assertEqual((self.private / "token").read_text().strip(), "reader_" * 8)
        self.assertFalse((self.install / "token").exists()); self.assertFalse((self.install / "control-token").exists())
        self.assertTrue((self.us / "koreader/patches/1-inkboard-file-ota-recovery.lua").is_file())
        second = self.run_install(); self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_wrong_device_cannot_install(self):
        self.usid.write_text("TEST000000000002\n")
        result = self.run_install(); self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.private.exists()); self.assertFalse(self.active.exists())

    def test_corrupt_source_rejected_before_private_or_active_writes(self):
        (self.install / "kindleprinter.koplugin/main.lua").write_text("CORRUPT")
        result = self.run_install(); self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.private.exists()); self.assertFalse(self.active.exists())

    def test_different_existing_plugin_is_preserved(self):
        self.active.mkdir(); (self.active / "main.lua").write_text("OTHER CLIENT")
        result = self.run_install(); self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.active / "main.lua").read_text(), "OTHER CLIENT")
        self.assertFalse((self.private / "token").exists())

    def test_known_identical_partial_file_set_can_finish_without_overwriting(self):
        self.active.mkdir()
        source = self.install / "kindleprinter.koplugin/main.lua"
        shutil.copyfile(source, self.active / "main.lua")
        result = self.run_install(); self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.active / "main.lua").read_bytes(), source.read_bytes())
        self.assertTrue((self.active / "VERSION").is_file())

    def test_unknown_existing_plugin_file_is_preserved_and_blocks_all_copies(self):
        self.active.mkdir(); extra = self.active / "unexpected.txt"; extra.write_text("PRESERVE")
        result = self.run_install(); self.assertNotEqual(result.returncode, 0)
        self.assertEqual(extra.read_text(), "PRESERVE")
        self.assertEqual({path.name for path in self.active.iterdir()}, {"unexpected.txt"})
        self.assertFalse((self.private / "ota-file-recovery.lua").exists())
        self.assertFalse((self.private / "token").exists())

    def test_unknown_existing_plugin_directory_is_preserved_and_blocks_all_copies(self):
        self.active.mkdir(); extra = self.active / "unexpected"; extra.mkdir()
        nested = extra / "keep.txt"; nested.write_text("PRESERVE")
        result = self.run_install(); self.assertNotEqual(result.returncode, 0)
        self.assertEqual(nested.read_text(), "PRESERVE")
        self.assertEqual({path.name for path in self.active.iterdir()}, {"unexpected"})
        self.assertFalse((self.private / "ota-file-recovery.lua").exists())

    def test_hidden_existing_plugin_entry_is_preserved_and_blocks_all_copies(self):
        self.active.mkdir(); extra = self.active / ".unexpected"; extra.write_text("PRESERVE")
        result = self.run_install(); self.assertNotEqual(result.returncode, 0)
        self.assertEqual(extra.read_text(), "PRESERVE")
        self.assertEqual({path.name for path in self.active.iterdir()}, {".unexpected"})
        self.assertFalse((self.private / "ota-file-recovery.lua").exists())

    def test_malformed_credential_stops_before_plugin_and_never_echoes_secret(self):
        secret = "do not echo credential"
        (self.install / "token").write_text(secret)
        result = self.run_install(); self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertFalse(self.active.exists()); self.assertFalse((self.private / "token").exists())

    def test_different_existing_private_helper_is_preserved(self):
        self.private.mkdir(); helper = self.private / "ota-file-recovery.lua"; helper.write_text("OTHER HELPER")
        result = self.run_install(); self.assertNotEqual(result.returncode, 0)
        self.assertEqual(helper.read_text(), "OTHER HELPER"); self.assertFalse(self.active.exists())
