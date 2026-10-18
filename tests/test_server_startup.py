"""Startup must not wait for reverse DNS, including on disconnected hosts."""
import http.client
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from inkboard.server import create_server


class ServerStartupTests(unittest.TestCase):
    def hub(self):
        return SimpleNamespace(config={"listen": {"host": "127.0.0.1", "port": 8787}, "public_url": ""})

    def test_bind_uses_numeric_address_without_reverse_dns(self):
        with tempfile.TemporaryDirectory() as directory, patch("socket.getfqdn", side_effect=AssertionError("Reverse DNS must not run")) as lookup:
            server = create_server(self.hub(), Path(directory), port=0)
            try:
                lookup.assert_not_called()
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertGreater(server.server_port, 0)
                self.assertEqual(server.server_port, server.socket.getsockname()[1])
            finally:
                server.server_close()

    def test_real_health_request_works_when_reverse_dns_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory, patch("socket.getfqdn", side_effect=OSError("Fixture resolver unavailable")) as lookup:
            server = create_server(self.hub(), Path(directory), port=0)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("GET", "/kindle-dashboard/healthz")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(b'"status":"ok"', response.read())
                lookup.assert_not_called()
            finally:
                connection.close()
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
