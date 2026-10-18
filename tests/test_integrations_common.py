"""Offline integration boundaries. No real credentials or external requests."""
import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from inkboard.integrations import create_adapter
from inkboard.integrations.common import Adapter, HTTPClient, IntegrationError, NoRedirects, TransportError, endpoint, number, secret


class Response:
    status = 200
    def __init__(self, data=b"{}"):
        self.data = data
        self.reads = []
    def read(self, n):
        self.reads.append(n)
        return self.data[:n]
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response or Response(), error, []
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class CommonTests(unittest.TestCase):
    def test_endpoint_rejects_inline_credentials_and_other_schemes(self):
        for value in ("file:///etc/passwd", "http://user:pass@example.com", "https://example.invalid/?token=x",
                      "https://example.invalid/#x", "http://a\\b", "http://a\n.invalid", "http://a:99999"):
            with self.subTest(value=value), self.assertRaises(IntegrationError): endpoint(value)

    def test_endpoint_accepts_local_network_and_base_path(self):
        self.assertEqual(endpoint("http://127.0.0.1:32400/"), "http://127.0.0.1:32400")
        self.assertEqual(endpoint("https://service.example.invalid/base"), "https://service.example.invalid/base")

    def test_secret_lookup_is_private_and_validated(self):
        self.assertEqual(secret({"plex_token": "fixture-only"}, "plex_token"), "fixture-only")
        for reference, values in (("missing", {}), ("x", {"x": "a\r\nb"}), ("../secret", {})):
            with self.assertRaises(IntegrationError): secret(values, reference)

    def test_numeric_fields_do_not_accept_bools_nan_or_infinity(self):
        for value in (True, "10", float("nan"), float("inf")):
            self.assertIsNone(number(value))

    def test_redirect_never_forwards_secret(self):
        with self.assertRaisesRegex(IntegrationError, "redirected"):
            NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.invalid")

    def test_http_uses_header_and_size_bound(self):
        client = HTTPClient("https://example.invalid", {"X-Plex-Token": "fixture-only"})
        response = Response(b'{"ok":true}')
        client.opener = Opener(response)
        self.assertEqual(client.request("/status"), {"ok": True})
        request, timeout = client.opener.calls[0]
        self.assertNotIn("fixture-only", request.full_url)
        self.assertEqual(request.get_header("X-plex-token"), "fixture-only")
        self.assertEqual(response.reads, [1_048_577])
        self.assertLessEqual(timeout, 10)

    def test_http_failures_are_safe_without_url_or_token(self):
        client = HTTPClient("http://example.invalid", {"Authorization": "fixture-only"})
        client.opener = Opener(error=URLError("fixture-only http://private.invalid/password"))
        with self.assertRaises(TransportError) as caught: client.request("/")
        self.assertNotIn("fixture-only", str(caught.exception))
        self.assertNotIn("private.invalid", str(caught.exception))

    def test_response_bounds_and_malformed_json(self):
        client = HTTPClient("http://example.invalid")
        for data in (b"x" * 12, b"not json"):
            client.opener = Opener(Response(data))
            with self.assertRaises(TransportError): client.request("/", max_bytes=10)

    def test_http_status_can_observe_503_without_reading_body(self):
        client = HTTPClient("http://example.invalid")
        client.opener = Opener(error=HTTPError("http://example.invalid", 503, "", {}, io.BytesIO(b"private-body")))
        self.assertEqual(client.request("/", json_response=False, accept_http_errors=True, read_body=False), (503, b""))

    def test_mutation_receipts_are_not_replayed(self):
        adapter = Adapter({"id": "test", "type": "test"}, {})
        calls = []
        operation = lambda: (calls.append(1) or {"status": "sent", "message": "Sent"})
        first = adapter._once("pause", {}, "request-123", operation)
        self.assertEqual(adapter._once("pause", {}, "request-123", operation), first)
        self.assertEqual(len(calls), 1)
        self.assertEqual(adapter._once("stop", {}, "request-123", operation)["status"], "rejected")

    def test_transport_loss_stays_uncertain(self):
        adapter = Adapter({"id": "test", "type": "test"}, {})
        calls = []
        def operation():
            calls.append(1)
            raise TransportError("safe timeout")
        self.assertEqual(adapter._once("request", {}, "request-123", operation)["status"], "uncertain")
        self.assertEqual(adapter._once("request", {}, "request-123", operation)["status"], "uncertain")
        self.assertEqual(len(calls), 1)

    def test_unknown_modules_fail_and_status_does_not_import_mqtt(self):
        with self.assertRaises(IntegrationError): create_adapter({"type": "unknown"}, {})
        with patch.dict("sys.modules", {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            adapter = create_adapter({"id": "site", "type": "http_status", "services": [
                {"id": "home", "url": "https://example.invalid"}]}, {})
        self.assertEqual(adapter.type, "http_status")

    def test_status_is_bounded_and_reports_recovery(self):
        adapter = create_adapter({"id": "site", "type": "http_status", "services": [
            {"id": "one", "url": "https://one.invalid"}, {"id": "two", "url": "https://two.invalid"}]}, {})
        first, second = adapter.services[0][2], adapter.services[1][2]
        first.opener, second.opener = Opener(), Opener(error=URLError("private"))
        failed = adapter.read()
        self.assertFalse(failed["available"])
        self.assertEqual(failed["metrics"], {"healthy": 1, "total": 2})
        second.opener = Opener()
        self.assertTrue(adapter.read()["available"])
        self.assertEqual(adapter.command("restart", {}, "request-123")["status"], "rejected")

    def test_status_default_only_checks_response_headers(self):
        adapter = create_adapter({"id": "site", "type": "http_status", "services": [
            {"id": "home", "url": "https://example.invalid"}]}, {})
        response = Response(b"x" * 1_000_000)
        adapter.services[0][2].opener = Opener(response)
        self.assertTrue(adapter.read()["available"])
        self.assertEqual(response.reads, [])


if __name__ == "__main__": unittest.main()
