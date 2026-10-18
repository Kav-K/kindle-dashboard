"""Repeatable provisioning, diagnostics, serving and outbound collectors."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import threading

from . import __version__
from .config import ConfigError, initialize, load
from .hub import Hub
from .server import create_server
from .store import Store


def parser():
    root = argparse.ArgumentParser(description="Inkboard private modular dashboard")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a new private setup without overwriting existing files")
    init.add_argument("--directory", "--dir", type=Path, required=True)
    init.add_argument("--demo", action="store_true", help="Use simulated printers with no device connections")
    init.add_argument("--name", default="My dashboard")
    init.add_argument("--wizard", action="store_true", help="Ask for the dashboard name and demo choice")
    check = sub.add_parser("check", help="Validate configuration and secret references (offline by default)")
    check.add_argument("--directory", "--dir", type=Path, required=True)
    check.add_argument("--connect", action="store_true", help="Explicitly make read-only connections to configured sources")
    for command in ("serve", "demo"):
        cmd = sub.add_parser(command, help="Serve a configured hub" if command == "serve" else "Create a new demo and serve it")
        cmd.add_argument("--directory", "--dir", type=Path, required=True)
        cmd.add_argument("--port", type=int, help="Override the local port for this run")
    token = sub.add_parser("show-token", help="Explicitly display one token in this terminal; never share it in issues")
    token.add_argument("--directory", "--dir", type=Path, required=True)
    token.add_argument("--role", choices=("reader", "controller", "ingest"), required=True)
    collect = sub.add_parser("collect", help="Poll local sources and send snapshots to a remote HTTPS hub")
    collect.add_argument("--directory", "--dir", type=Path, required=True)
    collect.add_argument("--hub", required=True, help="HTTPS base URL, including /kindle-dashboard")
    collect.add_argument("--once", action="store_true")
    secret = sub.add_parser("set-secret", help="Set an integration secret through a hidden terminal prompt")
    secret.add_argument("--directory", "--dir", type=Path, required=True)
    secret.add_argument("--name", required=True)
    return root


def check_setup(config, credentials, connect=False):
    from .integrations import create_adapter
    failed = False
    for module in config["modules"]:
        if not module.get("enabled", True):
            print(module["id"] + ": disabled")
            continue
        if module["type"] in {"demo", "alerts"} or module.get("remote", False):
            print(module["id"] + ": valid (" + ("remote ingestion" if module.get("remote") else module["type"]) + ")")
            continue
        adapter = None
        try:
            adapter = create_adapter(module, credentials)
            if connect:
                ready = getattr(adapter, "wait_ready", None)
                observation = ready(timeout=8) if ready is not None else adapter.read()
                okay = observation.get("available") is True and not observation.get("details", {}).get("degraded")
                print(module["id"] + (": responding" if okay else ": unavailable; check its network, credentials and service health"))
                failed |= not okay
            else:
                print(module["id"] + ": valid (offline check; connection not tested)")
        except Exception:
            print(module["id"] + ": invalid integration configuration or missing optional dependency; see the integration guide")
            failed = True
        finally:
            if adapter is not None:
                adapter.close()
    return not failed


def serve(directory, port=None):
    config, credentials = load(directory)
    if not check_setup(config, credentials):
        return 2
    store = Store(directory / "state")
    hub = Hub(config, credentials, store)
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("Port must be from 1 to 65535")
    server = create_server(hub, directory, port=port)
    hub.start()
    try:
        host, port = server.server_address[:2]
        print(f"Inkboard {__version__} listening at http://{host}:{port}/kindle-dashboard/", flush=True)
        print("Use a reader token in the browser. Remote access requires an HTTPS reverse proxy.", flush=True)
        if host == "0.0.0.0":
            print("Explicit network binding enabled. Keep the port private behind your HTTPS proxy/firewall.", flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if hub.close():
            store.close()
        # A bounded downstream HTTP call may outlive shutdown. Keep its DB
        # connection alive until process exit rather than close beneath it.
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    directory = args.directory.expanduser().absolute()
    try:
        if args.command == "init":
            name, demo = args.name, args.demo
            if args.wizard:
                name = input("Dashboard name [My dashboard]: ").strip() or name
                demo = input("Start with simulated printers? [Y/n]: ").strip().lower() not in {"n", "no"}
            initialize(directory, demo=demo, name=name)
            print("Created private config.json, secrets.json and state/ in " + str(directory))
            print("Next: inkboard check --directory <that directory>, then inkboard serve --directory <that directory>.")
            print("Add only the integrations you need; keep credentials in secrets.json.")
            return 0
        if args.command == "demo":
            initialize(directory, demo=True)
            return serve(directory, args.port)
        config, credentials = load(directory)
        if args.command == "check":
            okay = check_setup(config, credentials, args.connect)
            print("Configuration ready." if okay else "Configuration needs attention. No services were started.")
            return 0 if okay else 2
        if args.command == "show-token":
            print(credentials[args.role + "_token"])
            return 0
        if args.command == "set-secret":
            import getpass
            import os
            from .config import ID, write_private, check_private_access
            if not ID.fullmatch(args.name):
                raise ConfigError("Use a short lowercase secret name")
            if args.name in {"reader_token", "controller_token", "ingest_token"}:
                print("Changing a role token disconnects clients using the previous token. Coordinate the same private token on each paired device or collector.")
                if input("Type REPLACE to continue: ").strip() != "REPLACE":
                    raise ConfigError("Role token was not changed")
            value = getpass.getpass("Secret value (hidden): ")
            if not 1 <= len(value) <= 8192 or any(ord(c) < 32 for c in value):
                raise ConfigError("Secret must be a nonempty single-line value")
            if args.name in {"reader_token", "controller_token", "ingest_token"} and (not 32 <= len(value) <= 512 or value in {v for k, v in credentials.items() if k != args.name}):
                raise ConfigError("Role tokens require 32 to 512 characters and must be distinct")
            credentials[args.name] = value
            temporary = directory / (".secrets-" + __import__("secrets").token_hex(8) + ".tmp")
            try:
                write_private(temporary, credentials)
                check_private_access(temporary)
                os.replace(temporary, directory / "secrets.json")
            finally:
                if temporary.exists():
                    temporary.unlink()
            print("Integration secret saved privately; no value was printed.")
            return 0
        if args.command == "serve":
            return serve(directory, args.port)
        if args.command == "collect":
            from .collector import collect
            return collect(config, credentials, args.hub, once=args.once)
    except (ConfigError, OSError, ValueError) as exc:
        print("Setup failed: " + (str(exc) if isinstance(exc, ConfigError) else "Check the private setup path, network and permissions."), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
