"""Show a printer's certificate fingerprint without sending any credentials.

This is discovery, not verification: compare the target with the printer's
physical network screen/router and run on a trusted LAN. Nothing is trusted or
written automatically. A hostile first connection can show a hostile certificate.
"""
import argparse
import hashlib
import re
import socket
import ssl
import sys


def bounded_port(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def bounded_timeout(value):
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a number") from None
    if not 0.25 <= timeout <= 10:
        raise argparse.ArgumentTypeError("timeout must be between 0.25 and 10 seconds")
    return timeout


def inspect(host, port=8883, timeout=4):
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", host):
        raise ValueError("Use a printer IP address or hostname, not a URL")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    # The probe deliberately sends no MQTT CONNECT, username or access code.
    with socket.create_connection((host, port), timeout=timeout) as plain:
        with context.wrap_socket(plain, server_hostname=host) as stream:
            return hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Printer IP shown on its physical network screen")
    parser.add_argument("--port", type=bounded_port, default=8883)
    parser.add_argument("--timeout", type=bounded_timeout, default=4)
    args = parser.parse_args()
    try:
        fingerprint = inspect(args.host, args.port, args.timeout)
    except (OSError, ValueError, ssl.SSLError):
        print("Could not read a TLS certificate from that endpoint. No credential was sent or configuration changed.", file=sys.stderr)
        return 1
    print("Observed SHA256 certificate fingerprint (not automatically verified or trusted):")
    print(fingerprint)
    print("Verify the physical printer IP and your trusted LAN before adding this pin to private configuration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
