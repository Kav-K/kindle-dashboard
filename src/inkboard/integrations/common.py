"""Bounded provider I/O and the small public adapter contract.

Provider bodies, exception strings, credentials and network URLs never become
dashboard fields. Only deliberately selected values cross this boundary.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import ssl
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


class IntegrationError(ValueError):
    """A safe, non-secret diagnostic suitable for the operator."""


class TransportError(IntegrationError):
    """The provider may have received a mutation before this failure."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def text(value, maximum=160):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:maximum]


def number(value, minimum=0, maximum=1_000_000_000):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return max(minimum, min(value, maximum))


def integer(value, minimum=0, maximum=2_147_483_647):
    if type(value) is not int or not minimum <= value <= maximum:
        raise IntegrationError("An integer parameter is out of range.")
    return value


def secret(secrets, reference):
    if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", reference):
        raise IntegrationError("Configure a named secret reference.")
    value = secrets.get(reference)
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "\r\n\x00"):
        raise IntegrationError("A required secret is missing or invalid.")
    return value


def endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise IntegrationError("Configure a valid HTTP(S) base URL.")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise IntegrationError("Configure a valid HTTP(S) base URL.") from exc
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or "\\" in value
            or any(ord(c) < 33 for c in value)):
        raise IntegrationError("URLs must use HTTP(S), without credentials, queries or fragments.")
    return value.rstrip("/")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a configured API token to a redirect destination.
        raise IntegrationError("The service redirected. Configure its final URL.")


class HTTPClient:
    """Fixed-origin requests; no ambient proxy, redirects or response-body logs."""

    def __init__(self, base_url, headers=None, timeout=4):
        self.base_url = endpoint(base_url)
        if type(timeout) not in (int, float) or not 0.25 <= timeout <= 10:
            raise IntegrationError("timeout_seconds must be between 0.25 and 10.")
        self.timeout = timeout
        self.headers = dict(headers or {})
        self.opener = build_opener(ProxyHandler({}), NoRedirects(), HTTPSHandler(context=ssl.create_default_context()))

    def request(self, path, *, method="GET", body=None, json_response=True, max_bytes=1_048_576,
                accept_http_errors=False, read_body=True):
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise IntegrationError("Invalid provider route.")
        headers = {"Accept": "application/json", **self.headers}
        encoded = None
        if body is not None:
            encoded = json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > 8192:
                raise IntegrationError("Provider request is too large.")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=encoded, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = response.status
                payload = response.read(max_bytes + 1) if read_body else b""
        except HTTPError as exc:
            if accept_http_errors:
                try:
                    payload = exc.read(max_bytes + 1) if read_body else b""
                finally:
                    exc.close()
                if len(payload) > max_bytes:
                    raise TransportError("The service response exceeded the size limit.") from None
                return exc.code, payload
            code = exc.code
            exc.close()
            if code in (401, 403):
                raise IntegrationError("The service rejected its credential or permission.") from None
            error = TransportError if code >= 500 else IntegrationError
            raise error("The service returned an HTTP error (%s)." % code) from None
        except (URLError, TimeoutError, OSError):
            raise TransportError("The service could not be reached within its network timeout.") from None
        if len(payload) > max_bytes:
            raise TransportError("The service response exceeded the size limit.")
        if not json_response:
            return status, payload
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except (ValueError, UnicodeError):
            raise TransportError("The service returned invalid JSON.") from None


class Adapter:
    def __init__(self, config, secrets):
        self.config = config
        self.secrets = secrets
        self.id = config["id"]
        self.type = config["type"]
        self.name = config.get("name", self.id)
        self._receipt_lock = threading.Lock()
        self._receipts = OrderedDict()

    def snapshot(self, *, available, summary, metrics=None, details=None, capabilities=None, observed_at=None):
        return {"id": self.id, "type": self.type, "name": self.name,
                "observed_at": observed_at or utc_now(), "available": bool(available),
                "summary": text(summary, 240), "metrics": metrics or {},
                "capabilities": list(capabilities or []), "details": details or {}}

    def _once(self, action, parameters, request_id, operation):
        """Short-lived secondary deduplication; core supplies durable receipts.

        Mark uncertainty *before* handing a command to a provider. Never retry a
        timed-out command. IDs cannot be reused with a different payload.
        """
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            return {"status": "rejected", "message": "A unique request_id is required."}
        digest = hashlib.sha256(json.dumps([action, parameters], sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self._receipt_lock:
            previous = self._receipts.get(request_id)
            if previous:
                if previous[0] != digest:
                    return {"status": "rejected", "message": "This request ID was already used for another action."}
                return dict(previous[1])
            unknown = {"status": "uncertain", "message": "The action may have been delivered. Inspect the actual state before trying again."}
            self._receipts[request_id] = (digest, unknown)
            while len(self._receipts) > 256:
                self._receipts.popitem(last=False)
        try:
            result = operation()
        except TransportError:
            result = unknown
        except IntegrationError as exc:
            result = {"status": "rejected", "message": str(exc)}
        except Exception:
            result = unknown
        with self._receipt_lock:
            self._receipts[request_id] = (digest, dict(result))
        return result

    def command(self, action, parameters, request_id):
        return {"status": "rejected", "message": "This module is read-only."}

    def close(self):
        pass
