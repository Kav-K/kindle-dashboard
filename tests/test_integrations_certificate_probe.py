import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[1] / "examples" / "integrations" / "inspect_printer_certificate.py"
SPEC = importlib.util.spec_from_file_location("inkboard_certificate_probe_fixture", PATH)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


class Connection:
    def __init__(self): self.sent = []
    def getpeercert(self, binary_form=False): return b"synthetic certificate"
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def sendall(self, data): self.sent.append(data)


class ProbeTests(unittest.TestCase):
    def test_discovery_sends_no_mqtt_or_auth_and_does_not_write_configuration(self):
        stream = Connection()
        class Context:
            def wrap_socket(self, *args, **kwargs): return stream
        with patch.object(PROBE.socket, "create_connection", return_value=Connection()) as connect:
            with patch.object(PROBE.ssl, "SSLContext", return_value=Context()):
                result = PROBE.inspect("printer.example.invalid", 8883, 4)
        self.assertEqual(result, hashlib.sha256(b"synthetic certificate").hexdigest())
        self.assertEqual(stream.sent, [])
        connect.assert_called_once_with(("printer.example.invalid", 8883), timeout=4)

    def test_cli_bounds(self):
        for value in ("0", "65536", "not-a-number"):
            with self.assertRaises(Exception): PROBE.bounded_port(value)
        for value in ("0", "11", "nan", "inf", "not-a-number"):
            with self.assertRaises(Exception): PROBE.bounded_timeout(value)
        self.assertEqual(PROBE.bounded_port("8883"), 8883)
        self.assertEqual(PROBE.bounded_timeout("4"), 4)

    def test_hostname_is_not_a_credential_bearing_url(self):
        with patch.object(PROBE.socket, "create_connection") as connect:
            for value in ("http://user:pass@example.com", "file:///secret", "host name", "host\nname"):
                with self.assertRaises(ValueError): PROBE.inspect(value)
        connect.assert_not_called()


if __name__ == "__main__": unittest.main()
