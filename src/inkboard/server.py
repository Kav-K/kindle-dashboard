"""Authenticated HTTP hub. Bind loopback; put HTTPS in front for remote readers."""
from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
from socketserver import TCPServer
import threading
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import read_json
from .render import render
from .store import encode, utc

PREFIX = "/kindle-dashboard"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, hub, directory: Path):
        self.hub, self.directory = hub, directory
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def server_bind(self):
        # HTTPServer.server_bind calls getfqdn before listen. Reverse DNS is
        # unnecessary for a numeric bind address and can stall offline startup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Request content, URLs and downstream exceptions can contain credentials.
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "Inkboard"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # No token, query, downstream address or private title logging.
        pass

    @property
    def hub(self):
        return self.server.hub

    def send(self, status, payload, content_type="application/json", extra_headers=None):
        if content_type == "application/json":
            payload = encode(payload).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def authorize(self, role):
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1 or not values[0].startswith("Bearer ") or len(values[0]) > 520:
            return False
        token = values[0][7:]
        allowed = [self.hub.secrets[role + "_token"]]
        if role == "reader":
            allowed.append(self.hub.secrets["controller_token"])
        return any(hmac.compare_digest(token, value) for value in allowed)

    def origin_valid(self):
        # Authorization is explicit bearer, never ambient cookies; also reject cross-origin UI writes.
        origin = self.headers.get("Origin")
        if not origin:
            return True
        public = urlsplit(self.hub.config.get("public_url", ""))
        allowed = set()
        if public.netloc:
            allowed.add(public.scheme + "://" + public.netloc)
        host, port = self.server.server_address[:2]
        allowed.add(f"http://{host}:{port}")
        if host == "127.0.0.1":
            allowed.add(f"http://localhost:{port}")
        return origin in allowed

    def route(self):
        if len(self.path) > 1024:
            raise ValueError("URL too long")
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == PREFIX:
            path = "/"
        elif path.startswith(PREFIX + "/"):
            path = path[len(PREFIX):]
        if "%" in path or "//" in path or ".." in path:
            raise ValueError("Invalid route")
        host_headers = self.headers.get_all("Host", [])
        host, port = self.server.server_address[:2]
        allowed = {f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"}
        if host == "::1":
            allowed.add(f"[::1]:{port}")
        public = urlsplit(self.hub.config.get("public_url", ""))
        if public.netloc:
            allowed.add(public.netloc)
            if public.port in {None, 443}:
                allowed.add(public.hostname)
        if len(host_headers) != 1 or host_headers[0].lower() not in allowed:
            raise ValueError("Unexpected Host header")
        return path, parse_qs(parsed.query, max_num_fields=8)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path, query = self.route()
            if path == "/healthz":
                return self.send(200, {"status": "ok", "version": __version__})
            if path in {"/", "/index.html"}:
                return self.send(200, Path(__file__).with_name("web.html").read_bytes(), "text/html")
            if not self.authorize("reader"):
                return self.send(401, {"error": "A reader or controller bearer token is required"})
            if path in {"/api/v1/modules", "/api/v1/health"}:
                value = self.hub.snapshot()
                value.update(version=__version__)
                return self.send(200, value)
            if path in {"/api/v1/status", "/api/v1/printers"}:
                return self.send(200, self.hub.printers())
            if path == "/dashboard.png" or re.fullmatch(r"/status/(llm|site|plex|traffic)\.png", path):
                tab = query.get("tab", ["printers"])[0] if path == "/dashboard.png" else path.split("/")[-1][:-4]
                if tab not in {"printers", "plex", "llm", "site", "traffic", "alerts", "status"}:
                    return self.send(400, {"error": "Unknown dashboard tab"})
                return self.send(200, render(self.hub, tab), "image/png")
            if path == "/api/v1/plex/status":
                return self.send(200, self.hub.plex_status())
            if path in {"/api/v1/status/llm", "/api/v1/status/site"}:
                return self.send(200, self.hub.status_details(path.rsplit("/", 1)[1]))
            if path == "/api/v1/status/traffic":
                return self.send(200, {"schema_version": 1, "key": "traffic", "source": "origin", "window": query.get("window", ["5m"])[0], "view": query.get("view", ["pages"])[0], "sites": [], "paths": [], "coverage": {}, "totals": {}, "pagination": {}})
            if path in {"/api/v1/alerts", "/api/v1/alerts/history"}:
                return self.send(200, self.hub.alerts(history=path.endswith("history")))
            alert = re.fullmatch(r"/api/v1/alerts/(" + UUID + ")", path)
            if alert:
                self.hub.alerts()
                value = self.hub.store.notices().get(alert[1])
                return self.send(200 if value else 404, {"schema_version": 1, "alert": value} if value else {"error": "Unknown alert"})
            alert_receipt = re.fullmatch(r"/api/v1/alerts/actions/(" + UUID + ")", path)
            if alert_receipt:
                value = self.hub.store.command(alert_receipt[1])
                receipt = value.get("receipt") if value and value.get("module_id") == "alerts" else None
                return self.send(200 if receipt else 404, receipt or {"error": "Unknown alert receipt"})
            match = re.fullmatch(r"/api/v1/commands/(" + UUID + ")", path)
            if match:
                value = self.hub.store.command(match[1])
                return self.send(200 if value else 404, value or {"error": "Unknown receipt"})
            camera = re.fullmatch(r"/api/v1/camera/([A-Za-z0-9_-]{1,64})", path)
            if camera:
                rows = [p for p in self.hub.printers()["printers"] if p["id"] == camera[1] and p["online"]]
                if len(rows) != 1:
                    return self.send(503, {"error": "Camera source is offline or unavailable"})
                adapter = self.hub.adapters.get(rows[0]["module_id"])
                method = getattr(adapter, "camera_bytes", None)
                if not method:
                    return self.send(503, {"error": "This integration has no camera"})
                body, observed = method(camera[1])
                if not isinstance(body, bytes) or not body.startswith(b"\xff\xd8") or len(body) > 1048576:
                    return self.send(503, {"error": "Camera did not return a bounded JPEG"})
                from .hub import age
                headers = {"X-Captured-At": observed} if age(observed) != float("inf") else {}
                return self.send(200, body, "image/jpeg", headers)
            if path.startswith("/api/client-update/"):
                return self.update(path)
            if path.startswith("/api/v1/"):
                return self.send(503, {"error": "This optional feature is not configured in this portable hub"})
            return self.send(404, {"error": "Not found"})
        except (ValueError, TypeError, KeyError):
            return self.send(400, {"error": "Invalid request"})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return
        except Exception:
            return self.send(503, {"error": "Source unavailable; cached data and Health remain accessible"})

    def update(self, path):
        directory = self.server.directory / "client_updates"
        manifest = read_json(directory / "manifest.json", 16384)
        if path == "/api/client-update/manifest":
            return self.send(200, manifest)
        match = re.fullmatch(r"/api/client-update/file/([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:-dev)?)/(main\.lua|_meta\.lua|VERSION)", path)
        if not match or match[1] != manifest.get("version"):
            return self.send(404, {"error": "Unknown update asset"})
        assets = [row for row in manifest.get("files", []) if row.get("name") == match[2]]
        if len(assets) != 1:
            raise ValueError()
        target = directory / "files" / match[1] / match[2]
        if target.is_symlink() or not target.is_file() or target.stat().st_size > 393216:
            raise ValueError()
        body = target.read_bytes()
        if len(body) != assets[0].get("size") or hashlib.sha256(body).hexdigest() != assets[0].get("sha256"):
            return self.send(503, {"error": "Update asset failed integrity verification"})
        return self.send(200, body, "application/octet-stream")

    def do_POST(self):
        try:
            path, query = self.route()
            role = "ingest" if path == "/api/v1/ingest" else "controller"
            if not self.authorize(role):
                return self.send(401, {"error": "An appropriate bearer token is required"})
            if not self.origin_valid():
                return self.send(403, {"error": "Cross-origin writes are not allowed"})
            lengths = self.headers.get_all("Content-Length", [])
            limit = 262144 if role == "ingest" else 8192
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit() or not 1 <= int(lengths[0]) <= limit:
                return self.send(413, {"error": "Expected a bounded Content-Length"})
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                return self.send(415, {"error": "Use application/json"})
            body = self.rfile.read(int(lengths[0]))
            if len(body) != int(lengths[0]):
                raise ValueError()
            payload = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if path in {"/api/v1/commands", "/api/v1/modules/commands"}:
                value, created = self.hub.submit(payload)
                return self.send(202 if created else 200, value)
            if path == "/api/v1/ingest":
                self.hub.ingest(payload)
                return self.send(200, {"status": "accepted", "received_at": utc()})
            if path == "/api/v1/alerts/actions":
                return self.send(200, self.hub.alert_action(payload))
            return self.send(503, {"error": "This optional action is not configured; nothing was sent"})
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            # Core validation messages contain no downstream credentials.
            return self.send(400, {"error": str(exc)[:200] if type(exc) is ValueError else "Invalid request"})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return
        except Exception:
            return self.send(503, {"error": "Action outcome unavailable. Check its request receipt before sending again."})


def create_server(hub, directory: Path, *, port=None):
    address = (hub.config["listen"]["host"], hub.config["listen"]["port"] if port is None else port)
    if address[0] == "::1":
        class IPv6Server(Server):
            address_family = socket.AF_INET6
        return IPv6Server(address, hub, directory)
    return Server(address, hub, directory)
