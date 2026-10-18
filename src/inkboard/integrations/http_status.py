"""A dependency-free HTTP availability integration for any chosen service."""
from __future__ import annotations

import time
from .common import Adapter, HTTPClient, IntegrationError, integer, text, utc_now


class HTTPStatusAdapter(Adapter):
    def __init__(self, config, secrets):
        super().__init__(config, secrets)
        services = config.get("services")
        if not isinstance(services, list) or not 1 <= len(services) <= 16:
            raise IntegrationError("http_status requires between 1 and 16 services.")
        self.services = []
        seen = set()
        for service in services:
            if not isinstance(service, dict):
                raise IntegrationError("Each HTTP service must be an object.")
            identifier = service.get("id")
            if not isinstance(identifier, str) or not identifier or len(identifier) > 64 or identifier in seen:
                raise IntegrationError("Each HTTP service needs a unique id.")
            seen.add(identifier)
            codes = service.get("expected_status", [200])
            if not isinstance(codes, list) or not 1 <= len(codes) <= 10:
                raise IntegrationError("expected_status must be a list of HTTP status codes.")
            for code in codes:
                integer(code, 100, 599)
            path = service.get("path", "/")
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or len(path) > 256:
                raise IntegrationError("Service path must be a short absolute path.")
            self.services.append((identifier, text(service.get("name", identifier), 80),
                                  HTTPClient(service["url"], timeout=config.get("timeout_seconds", 4)),
                                  path, codes))

    def read(self):
        rows = []
        for identifier, name, client, path, codes in self.services:
            started = time.monotonic()
            try:
                code, _ = client.request(path, json_response=False, max_bytes=262144,
                                         accept_http_errors=True, read_body=False)
                okay, message = code in codes, "Responding" if code in codes else "Unexpected HTTP status"
            except IntegrationError as exc:
                code, okay, message = None, False, str(exc)
            rows.append({"id": identifier, "name": name, "available": okay,
                         "status_code": code, "latency_ms": round((time.monotonic() - started) * 1000),
                         "message": message, "observed_at": utc_now()})
        healthy = sum(row["available"] for row in rows)
        return self.snapshot(available=healthy == len(rows), summary=f"{healthy}/{len(rows)} services responding",
                             metrics={"healthy": healthy, "total": len(rows)}, details={"services": rows})
