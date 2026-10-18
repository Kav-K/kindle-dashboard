"""Offline regression checks for header-safe role credentials and atomic edits."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from inkboard import config
from inkboard.cli import main


ROLES = ("reader_token", "controller_token", "ingest_token")
TOKENS = {role: role + "_" + "a" * 40 for role in ROLES}


class RoleTokenTests(unittest.TestCase):
    def test_visible_ascii_bounds_without_controls_spaces_or_unicode(self):
        for value in ("a" * 31, "a" * 513, "a" * 31 + " ", "a" * 31 + "\n", "a" * 31 + "\r",
                      "a" * 31 + "\t", "a" * 31 + "\x00", "a" * 31 + "\x7f", "a" * 31 + "\u00e9", None, 42):
            with self.subTest(kind=type(value).__name__):
                self.assertFalse(config.valid_role_token(value))
        for value in ("a" * 32, "a" * 512, "Aa09-._~+/=" * 4):
            self.assertTrue(config.valid_role_token(value))

    def setup_files(self, directory):
        settings = {"schema_version": 1, "name": "Role fixture", "listen": {"host": "127.0.0.1", "port": 8787},
                    "public_url": "", "refresh_seconds": 15, "stale_after_seconds": 60,
                    "modules": [{"id": "alerts", "type": "alerts", "name": "Health"}]}
        (directory / "config.json").write_text(json.dumps(settings), encoding="utf-8")
        (directory / "secrets.json").write_text(json.dumps(TOKENS), encoding="utf-8")
        os.chmod(directory / "secrets.json", 0o600)
        return settings

    def test_load_rejects_unusable_value_for_each_role_without_echo(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.setup_files(directory)
            for role in ROLES:
                for suffix in ("\u00e9", "\n", "\r", "\x00", "\x7f", " "):
                    tokens = {**TOKENS, role: "fixture_" + "x" * 40 + suffix}
                    (directory / "secrets.json").write_text(json.dumps(tokens), encoding="utf-8")
                    # Permission behavior has separate real-ACL tests. This
                    # fixture isolates the shared header/comparison validation.
                    with patch.object(config, "check_private_access"):
                        with self.assertRaises(config.ConfigError) as caught:
                            config.load(directory)
                    self.assertNotIn(tokens[role], str(caught.exception))

    def test_invalid_role_setter_keeps_existing_file_and_does_not_echo_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.setup_files(directory)
            initial = (directory / "secrets.json").read_bytes()
            settings = json.loads((directory / "config.json").read_text(encoding="utf-8"))
            for role in ROLES:
                for value in ("fixture_" + "x" * 40 + "\u00e9", "fixture_" + "x" * 40 + "\x7f", " " + "x" * 40):
                    output = io.StringIO()
                    with patch("inkboard.cli.load", return_value=(settings, dict(TOKENS))):
                        with patch("builtins.input", return_value="REPLACE"), patch("getpass.getpass", return_value=value):
                            with redirect_stdout(output), redirect_stderr(output):
                                result = main(["set-secret", "--directory", str(directory), "--name", role])
                    self.assertEqual(result, 2)
                    self.assertEqual((directory / "secrets.json").read_bytes(), initial)
                    self.assertNotIn(value, output.getvalue())
                    self.assertEqual(list(directory.glob(".secrets-*.tmp")), [])


if __name__ == "__main__": unittest.main()
