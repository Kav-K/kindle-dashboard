"""Outbound-only remote snapshots with bounded backoff, no remote control queue."""
from __future__ import annotations

import json
import random
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .integrations import create_adapter


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def endpoint(base):
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Collector hub requires an HTTPS URL without credentials")
    if parsed.path.rstrip("/") != "/kindle-dashboard":
        raise ValueError("Collector URL must include /kindle-dashboard")
    return base.rstrip("/") + "/api/v1/ingest"


def push(url, token, module_id, snapshot, opener=None):
    body = json.dumps({"module_id": module_id, "snapshot": snapshot}, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 262144:
        raise ValueError("Snapshot is too large")
    request = Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    opener = opener or build_opener(NoRedirect())
    with opener.open(request, timeout=10) as response:
        if response.status != 200 or len(response.read(4097)) > 4096:
            raise ValueError("Hub rejected the snapshot")


def collect(config, credentials, base, once=False):
    url = endpoint(base)
    adapters = []
    for module in config["modules"]:
        if module.get("enabled", True) and module["type"] in {"bambu", "plex", "http_status"}:
            adapters.append((module["id"], create_adapter(module, credentials)))
    if not adapters:
        raise ValueError("No collectable modules configured")
    backoff = config["refresh_seconds"]
    try:
        while True:
            failures = 0
            source_problems = 0
            for ident, adapter in adapters:
                try:
                    ready = getattr(adapter, "wait_ready", None)
                    snapshot = ready(timeout=8) if once and ready is not None else adapter.read()
                    push(url, credentials["ingest_token"], ident, snapshot)
                    source_problem = not snapshot.get("available") or snapshot.get("details", {}).get("degraded")
                    source_problems += bool(source_problem)
                    print(ident + ": snapshot delivered" + (" (source needs attention)" if source_problem else ""), flush=True)
                except Exception:
                    failures += 1
                    print(ident + ": snapshot delivery unavailable; retrying with backoff (no commands retried)", flush=True)
            if once:
                return 2 if failures or source_problems else 0
            backoff = min(300, max(config["refresh_seconds"], backoff * 2)) if failures else config["refresh_seconds"]
            time.sleep(backoff + random.uniform(0, min(5, backoff / 10)))
    finally:
        for _, adapter in adapters:
            adapter.close()
