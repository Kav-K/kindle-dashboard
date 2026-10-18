"""Bootstrap error paths and shell quoting, using an inert Python stand-in.

No dependencies are downloaded and no actual virtual environment is created.
The real clean-environment install is exercised by tools/verify_setup.py.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("sh")
POWERSHELL = shutil.which("powershell")


@unittest.skipUnless(SHELL, "POSIX shell not available")
class ShellBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="inkboard-bootstrap-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkout with spaces and ' quote"
        (self.root / "tools").mkdir(parents=True)
        shutil.copyfile(ROOT / "tools/bootstrap.sh", self.root / "tools/bootstrap.sh")
        self.fixture = self.root / "python-fixture.sh"
        self.fixture.write_text("""#!/bin/sh
set -eu
if [ "$1" = '-c' ]; then
    [ "${FIXTURE_VERSION_OK:-yes}" = yes ]
    exit
fi
if [ "$1" = '-m' ] && [ "$2" = venv ]; then
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
    exit 0
fi
if [ "$1" = '-m' ] && [ "$2" = pip ]; then
    printf '%s' "$4" > "$FIXTURE_PACKAGE_FILE"
    [ "${FIXTURE_PIP_OK:-yes}" = yes ]
    exit
fi
exit 91
""", encoding="utf-8", newline="\n")
        self.fixture.chmod(0o700)
        self.package = self.root / "package-argument.txt"
        self.environment = dict(os.environ, PYTHON=self.fixture.as_posix(), FIXTURE_PACKAGE_FILE=self.package.as_posix())

    def run_bootstrap(self, *arguments, **environment):
        return subprocess.run([SHELL, (self.root / "tools/bootstrap.sh").as_posix(), *arguments],
                              cwd=self.root, env=dict(self.environment, **environment),
                              capture_output=True, text=True, timeout=15)

    def test_python_version_failure_does_not_create_environment(self):
        result = self.run_bootstrap(FIXTURE_VERSION_OK="no")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11", result.stderr)
        self.assertFalse((self.root / ".venv").exists())

    def test_help_and_unknown_arguments_do_not_create_environment(self):
        for arguments, expected in ((["--help"], 0), (["--typo"], 2), (["--bambu", "extra"], 2), ([""], 2)):
            with self.subTest(arguments=arguments):
                result = self.run_bootstrap(*arguments)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn("Usage:", result.stdout + result.stderr)
                self.assertFalse((self.root / ".venv").exists())

    def test_spaces_quotes_and_bambu_extras_are_one_package_argument(self):
        result = self.run_bootstrap("--bambu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.package.read_text().endswith("[bambu]"))
        self.assertIn("checkout with spaces and ' quote", self.package.read_text())
        self.assertIn(".venv/bin/inkboard init --directory", result.stdout)
        before = self.package.read_bytes()
        repeat = self.run_bootstrap()
        self.assertNotEqual(repeat.returncode, 0)
        self.assertEqual(self.package.read_bytes(), before)

    def test_dependency_failure_preserves_environment_and_explains_retry(self):
        result = self.run_bootstrap(FIXTURE_PIP_OK="no")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Dependency installation failed", result.stderr)
        self.assertTrue((self.root / ".venv").is_dir())


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell not available")
class WindowsBootstrapTests(unittest.TestCase):
    def test_old_python_fails_before_environment_creation(self):
        with tempfile.TemporaryDirectory(prefix="inkboard-bootstrap-") as temporary:
            root = Path(temporary) / "checkout with spaces and ' quote"
            (root / "tools").mkdir(parents=True)
            shutil.copyfile(ROOT / "tools/bootstrap.ps1", root / "tools/bootstrap.ps1")
            command = root / "reject-old-python.ps1"
            command.write_text("""$ErrorActionPreference = 'Stop'
function Fixture-Python {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    if ($Arguments[0] -ne '-c') { throw 'Unexpected environment mutation' }
    $global:LASTEXITCODE = 1
}
try { & (Join-Path $PSScriptRoot 'tools/bootstrap.ps1') -Python Fixture-Python }
catch { Write-Output $_.Exception.Message; exit 2 }
exit 0
""", encoding="utf-8")
            result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(command)],
                                    cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Python 3.11", result.stdout)
            self.assertIn("-Python the full path", result.stdout)
            self.assertFalse((root / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
