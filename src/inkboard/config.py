"""Validated, non-secret configuration and private local provisioning."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
from urllib.parse import urlsplit

MAX_CONFIG_BYTES = 131072
ID = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
MODULE_TYPES = {"demo", "bambu", "plex", "http_status", "alerts"}
TOP_KEYS = {"schema_version", "name", "listen", "public_url", "refresh_seconds", "stale_after_seconds", "modules", "kindle"}


class ConfigError(ValueError):
    pass


def valid_role_token(value) -> bool:
    """Role credentials must survive HTTP headers and constant-time comparison.

    Visible ASCII excludes spaces, every control character, DEL and non-ASCII.
    Keep integration credentials separate: their provider may use another format.
    """
    return (isinstance(value, str) and 32 <= len(value) <= 512
            and all(33 <= ord(char) <= 126 for char in value))


def read_json(path: Path, max_bytes: int = MAX_CONFIG_BYTES) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ConfigError("Expected a bounded, regular JSON file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as exc:
        raise ConfigError("Invalid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration must be a JSON object")
    return data


def integer(value, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{label} must be an integer from {low} to {high}")
    return value


def validate(config: dict) -> dict:
    if config.get("schema_version") != 1 or set(config) - TOP_KEYS:
        raise ConfigError("Unknown configuration schema or keys")
    if not isinstance(config.get("name"), str) or not 1 <= len(config["name"]) <= 80:
        raise ConfigError("name must contain 1 to 80 characters")
    listen = config.get("listen", {})
    if not isinstance(listen, dict) or set(listen) != {"host", "port"}:
        raise ConfigError("listen requires host and port")
    if listen["host"] not in {"127.0.0.1", "::1", "0.0.0.0"}:
        raise ConfigError("listen.host must be a loopback address or explicit 0.0.0.0")
    integer(listen["port"], 1, 65535, "listen.port")
    integer(config.get("refresh_seconds"), 5, 3600, "refresh_seconds")
    integer(config.get("stale_after_seconds"), config["refresh_seconds"] * 2, 86400, "stale_after_seconds")
    public = config.get("public_url", "")
    if not isinstance(public, str) or len(public) > 512:
        raise ConfigError("public_url must be a URL")
    if public:
        u = urlsplit(public)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ConfigError("public_url must be HTTPS without credentials, query or fragment")
        if u.path.rstrip("/") not in {"", "/kindle-dashboard"}:
            raise ConfigError("public_url path must be empty or /kindle-dashboard")
    modules = config.get("modules")
    if not isinstance(modules, list) or not 1 <= len(modules) <= 16:
        raise ConfigError("Configure 1 to 16 modules")
    ids = set()
    for module in modules:
        if not isinstance(module, dict) or not isinstance(module.get("id"), str) or not ID.fullmatch(module["id"]):
            raise ConfigError("Every module needs a short lowercase id")
        if module["id"] in ids:
            raise ConfigError("Module ids must be unique")
        ids.add(module["id"])
        if module.get("type") not in MODULE_TYPES:
            raise ConfigError("Unsupported module type")
        if type(module.get("enabled", True)) is not bool:
            raise ConfigError("enabled must be true or false")
        if not isinstance(module.get("name", module["id"]), str) or len(module.get("name", "")) > 80:
            raise ConfigError("Module names must be short strings")
        # Configurations contain secret *references*. Values belong in secrets.json.
        def inspect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if re.search(r"(^|_)(password|token|api_key|access_code|secret)$", key) and not key.endswith("_secret"):
                        raise ConfigError("Use a *_secret reference instead of an inline credential")
                    if key.endswith("_secret") and (not isinstance(item, str) or not ID.fullmatch(item)):
                        raise ConfigError("Secret references must be lowercase names")
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)
        inspect(module)
    kindle = config.get("kindle", {})
    if not isinstance(kindle, dict) or set(kindle) - {"serial", "printer_ids", "timezone", "features"}:
        raise ConfigError("Invalid Kindle profile")
    if "serial" in kindle and not re.fullmatch(r"[A-Z0-9]{16}", str(kindle["serial"])):
        raise ConfigError("Invalid Kindle serial")
    if "printer_ids" in kindle and (not isinstance(kindle["printer_ids"], list) or len(kindle["printer_ids"]) > 2 or any(not isinstance(x, str) for x in kindle["printer_ids"]) or len(set(kindle["printer_ids"])) != len(kindle["printer_ids"]) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", x) for x in kindle["printer_ids"])):
        raise ConfigError("Kindle profile supports at most two distinct printer ids")
    if "timezone" in kindle:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(kindle["timezone"])
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ConfigError("Unknown Kindle timezone") from None
    if "features" in kindle and (not isinstance(kindle["features"], dict) or any(type(v) is not bool for v in kindle["features"].values())):
        raise ConfigError("Kindle features must be booleans")
    return config


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ConfigError("Private directory must not be a symbolic link")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        # Do not depend on chmod's incomplete Windows ACL semantics.
        principal = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", principal + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True, timeout=15)
        if result.returncode:
            raise ConfigError("Could not restrict private directory permissions")
        check_private_access(path)
    else:
        os.chmod(path, 0o700)


def check_private_access(path: Path) -> None:
    if os.name != "nt":
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ConfigError("Private setup files must not grant group or other access")
        return
    # Test actual allow ACE SIDs, including explicit ACEs which inheritance:r retains.
    script = "$ErrorActionPreference='Stop';try{Import-Module ($PSHOME+'\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop;$p=[Console]::In.ReadToEnd();$a=Get-Acl -LiteralPath $p -ErrorAction Stop;$u=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;$ok=@($u,'S-1-5-18','S-1-5-32-544','S-1-3-4');foreach($r in $a.Access){if($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -notin $ok){exit 21}};if(-not $a.Owner){exit 22};if(([System.Security.Principal.NTAccount]$a.Owner).Translate([System.Security.Principal.SecurityIdentifier]).Value -notin $ok){exit 22};exit 0}catch{exit 23}"
    child_environment = os.environ.copy()
    # A parent PowerShell 7 session can inject incompatible modules into Windows
    # PowerShell 5.1. Load the OS security module explicitly, with its own path.
    windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell_home = windows_root / "System32" / "WindowsPowerShell" / "v1.0"
    child_environment["PSModulePath"] = str(powershell_home / "Modules")
    result = subprocess.run([str(powershell_home / "powershell.exe"), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], input=str(path), text=True, capture_output=True, timeout=20, env=child_environment)
    if result.returncode:
        raise ConfigError("Private setup permissions allow another principal; use a new private setup directory")


def write_private(path: Path, document: dict) -> None:
    data = (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def initialize(directory: Path, *, demo: bool = False, name: str = "My dashboard") -> dict:
    directory = directory.expanduser().absolute()
    if directory.exists():
        raise ConfigError("Setup directory must be new; existing setup is never overwritten")
    private_directory(directory)
    config = {"schema_version": 1, "name": name, "listen": {"host": "127.0.0.1", "port": 8787},
              "public_url": "", "refresh_seconds": 15, "stale_after_seconds": 60,
              "modules": [{"id": "demo", "type": "demo", "name": "Demo printers", "enabled": True}] if demo else
                         [{"id": "health", "type": "alerts", "name": "Dashboard health", "enabled": True}]}
    validate(config)
    credentials = {role: secrets.token_urlsafe(32) for role in ("reader_token", "controller_token", "ingest_token")}
    write_private(directory / "config.json", config)
    write_private(directory / "secrets.json", credentials)
    private_directory(directory / "state")
    return config


def load(directory: Path) -> tuple[dict, dict]:
    directory = directory.expanduser().absolute()
    if directory.is_symlink():
        raise ConfigError("Setup directory must not be a symbolic link")
    check_private_access(directory)
    config = validate(read_json(directory / "config.json"))
    path = directory / "secrets.json"
    credentials = read_json(path, 65536)
    check_private_access(path)
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ConfigError("secrets.json must have mode 0600 (chmod 600)")
    tokens = [credentials.get(role) for role in ("reader_token", "controller_token", "ingest_token")]
    if any(not valid_role_token(value) for value in tokens) or len(set(tokens)) != 3:
        raise ConfigError("Three distinct role tokens of 32 to 512 visible ASCII characters, without spaces or control characters, are required")
    if any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 8192 for k, v in credentials.items()):
        raise ConfigError("Secrets must be bounded string values")
    return config, credentials
