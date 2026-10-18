#!/usr/bin/env python3
"""Build a private, device-bound Inkboard client without contacting services.

SPDX-License-Identifier: GPL-3.0-or-later
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native"
MAX_FILE = 384 * 1024  # Also enforced by the root-owned API2 recovery helper.
MAX_PACKAGE = 512 * 1024
FILES = ("_meta.lua", "main.lua", "VERSION")
FEATURES = ("doctor", "media_requests", "media_operator", "subtitles", "telemetry")


class BuildError(ValueError):
    """A configuration or package failed validation; no install was attempted."""


def validate_profile(config: dict) -> dict:
    if not isinstance(config, dict):
        raise BuildError("Configuration must be a JSON object")
    base = config.get("public_url")
    if not isinstance(base, str) or len(base) > 300 or not base.isascii():
        raise BuildError("public_url must be an ASCII HTTPS URL")
    try:
        parsed = urlsplit(base)
        port = parsed.port
    except ValueError as error:
        raise BuildError("public_url is invalid") from error
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query
            or parsed.fragment or port not in (None, 443) or host != host.lower()
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
            or "." not in host or len(host) > 253 or ".." in host
            or any(len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in host.split("."))):
        raise BuildError("public_url needs a DNS name, HTTPS and the standard port")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise BuildError("Use a certificate-covered DNS name, not an IP address")
    path = parsed.path.rstrip("/")
    if path != "/kindle-dashboard":
        raise BuildError("public_url must end with the hub's /kindle-dashboard route")
    # Canonical spelling is essential: the native route and TLS guards compare it.
    normalized = "https://" + host + path
    if base.rstrip("/") not in (normalized, "https://" + host + ":443" + path):
        raise BuildError("public_url must use a canonical lowercase DNS name")
    kindle = config.get("kindle")
    if not isinstance(kindle, dict):
        raise BuildError("Add a kindle object with this device's serial")
    if set(kindle) - {"serial", "printer_ids", "timezone", "features"}:
        raise BuildError("Unknown kindle configuration field")
    serial = kindle.get("serial")
    if not isinstance(serial, str) or not re.fullmatch(r"[A-Z0-9]{16}", serial):
        raise BuildError("kindle.serial must be the 16 uppercase letters/digits from Device Info")
    ids = kindle.get("printer_ids", [])
    if (not isinstance(ids, list) or len(ids) > 2 or len(set(str(x) for x in ids)) != len(ids)
            or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", x) for x in ids)):
        raise BuildError("kindle.printer_ids must contain at most two distinct configured IDs")
    timezone = kindle.get("timezone", "UTC")
    if (not isinstance(timezone, str) or len(timezone) > 64
            or not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", timezone)):
        raise BuildError("kindle.timezone must be an IANA timezone name")
    features = kindle.get("features", {})
    if (not isinstance(features, dict) or set(features) - set(FEATURES)
            or any(type(value) is not bool for value in features.values())):
        raise BuildError("kindle.features must contain only documented boolean feature flags")
    return {"public_url": normalized, "host": host, "serial": serial,
            "printer_ids": ids, "timezone": timezone,
            "features": {name: features.get(name, False) for name in FEATURES}}


def _tokens(secrets: dict | None) -> dict[str, str]:
    if secrets is None:
        return {}
    # The hub's secrets file is the single source. Never put a credential in config.
    values = secrets.get("tokens", secrets) if isinstance(secrets, dict) else None
    if not isinstance(values, dict):
        raise BuildError("Secrets must be a JSON object")
    reader = values.get("reader_token", values.get("reader"))
    controller = values.get("controller_token", values.get("control_token", values.get("controller")))
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", value)
           for value in (reader, controller)) or reader == controller:
        raise BuildError("Secrets need separate reader and controller tokens (24–256 URL-safe characters)")
    return {"token": reader, "control-token": controller}


def render_client(config: dict, version: str = "0.1.0") -> tuple[dict[str, bytes], bytes, dict]:
    profile = validate_profile(config)
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:-dev)?", version):
        raise BuildError("Version must be x.y.z or x.y.z-dev")
    recovery = (NATIVE / "ota-file-recovery.lua.in").read_text(encoding="utf-8")
    recovery = recovery.replace("__INKBOARD_SERIAL__", profile["serial"]).encode("utf-8")
    if len(recovery) > 32768:
        raise BuildError("Recovery helper exceeds the native bound")
    ids = profile["printer_ids"] + ["unconfigured-printer-1", "unconfigured-printer-2"]
    features = "{" + ",".join(f"{key}={'true' if value else 'false'}" for key, value in profile["features"].items()) + "}"
    replacements = {"VERSION": version, "HOST": profile["host"], "BASE": profile["public_url"],
                    "SERIAL": profile["serial"], "PRINTER_ONE": ids[0], "PRINTER_TWO": ids[1],
                    "TIMEZONE": profile["timezone"], "FEATURES": features,
                    "RECOVERY_SHA256": hashlib.sha256(recovery).hexdigest()}
    main = (NATIVE / "main.lua.in").read_text(encoding="utf-8")
    for key, value in replacements.items():
        main = main.replace(f"__INKBOARD_{key}__", value)
    if "__INKBOARD_" in main:
        raise BuildError("Unresolved client template field")
    files = {"_meta.lua": (NATIVE / "_meta.lua").read_bytes(),
             "main.lua": main.encode("utf-8"), "VERSION": (version + "\n").encode("ascii")}
    if any(not 0 < len(value) <= MAX_FILE for value in files.values()) or sum(map(len, files.values())) > MAX_PACKAGE:
        raise BuildError("Client exceeds the API2 update bounds; do not publish this build")
    manifest = {"schema_version": 1, "device_serial": profile["serial"], "version": version,
                "files": [{"name": name, "size": len(files[name]), "sha256": hashlib.sha256(files[name]).hexdigest()}
                          for name in FILES]}
    return files, recovery, manifest


def build(config: dict, output: Path, version: str = "0.1.0", secrets: dict | None = None) -> dict:
    profile = validate_profile(config)
    token_files = _tokens(secrets)
    files, recovery, manifest = render_client(config, version)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise BuildError("Output already exists; select a fresh build directory")
    # Complete all validation before creating an output. mkdir is exclusive.
    output.mkdir(mode=0o700, parents=True)
    if os.name == "nt":
        principal = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
        secured = subprocess.run(["icacls", str(output), "/inheritance:r", "/grant:r",
                                  principal + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                                 capture_output=True, timeout=15)
        if secured.returncode:
            raise BuildError("Could not restrict the private build directory")

    def put(relative: str, data: bytes | str):
        target = output / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        raw = data.encode("utf-8") if isinstance(data, str) else data
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)

    install = "usb/inkboard-install/"
    for name, raw in files.items():
        put(install + "kindleprinter.koplugin/" + name, raw)
        put("client_updates/files/" + version + "/" + name, raw)
    put(install + "ota-file-recovery.lua", recovery)
    startup = (NATIVE / "startup-recovery.lua.in").read_text(encoding="utf-8")
    startup = startup.replace("__INKBOARD_RECOVERY_SHA256__", hashlib.sha256(recovery).hexdigest())
    put(install + "1-inkboard-file-ota-recovery.lua", startup)
    for name, value in token_files.items():
        put(install + name, value + "\n")
    sums = {"kindleprinter.koplugin/" + name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    sums["ota-file-recovery.lua"] = hashlib.sha256(recovery).hexdigest()
    sums["1-inkboard-file-ota-recovery.lua"] = hashlib.sha256(startup.encode("utf-8")).hexdigest()
    put(install + "checksums.sha256", "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())))
    script = (NATIVE / "install.sh.in").read_text(encoding="utf-8")
    script = script.replace("__INKBOARD_SERIAL__", profile["serial"])
    put(install + "install.sh", script)
    put("usb/documents/InstallInkboard.sh", "#!/bin/sh\n# Name: Install Inkboard\n# Author: Inkboard contributors\n/bin/sh /mnt/us/inkboard-install/install.sh\n")
    put("usb/extensions/inkboard/menu.json", json.dumps({"items": [{"name": "Install Inkboard (first install)",
          "action": "/bin/sh /mnt/us/inkboard-install/install.sh", "exitmenu": True}]}, indent=2) + "\n")
    put("usb/extensions/inkboard/config.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<extension><information><name>Inkboard</name><version>1</version></information><menus><menu type="json" dynamic="true">menu.json</menu></menus></extension>\n')
    put("client_updates/manifest.json", json.dumps(manifest, indent=2) + "\n")
    put("build-receipt.json", json.dumps({"schema_version": 1, "version": version,
          "public_url": profile["public_url"], "device_serial": profile["serial"],
          "printer_ids": profile["printer_ids"], "contains_usb_credentials": bool(token_files),
          "files": manifest["files"], "recovery_sha256": sums["ota-file-recovery.lua"]}, indent=2) + "\n")
    # A fixed timestamp and ordering make identical inputs byte-reproducible.
    with zipfile.ZipFile(output / "inkboard-usb.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((output / "usb").rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(output / "usb").as_posix(), (2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o100600 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    os.chmod(output / "inkboard-usb.zip", 0o600)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--secrets", type=Path, help="Private hub secrets JSON; values are never printed")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        secrets = json.loads(args.secrets.read_text(encoding="utf-8")) if args.secrets else None
        result = build(config, args.out, args.version, secrets)
    except (BuildError, OSError, json.JSONDecodeError):
        # JSON/OS errors can include secret fragments or private paths. Keep stdout clean.
        parser.exit(2, "Build failed: check the configuration, secrets format, and unused output directory.\n")
    print("Built Inkboard " + result["version"] + ". USB package and authenticated OTA assets are ready.")
    if args.secrets:
        print("The USB package contains private credentials. Delete it after installation; never upload it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
