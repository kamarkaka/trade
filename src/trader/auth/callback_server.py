"""Local HTTPS loopback server to capture the OAuth redirect callback (§8.1/§16.4).

Schwab requires HTTPS even for loopback redirects, so we serve over TLS with a
self-signed certificate. The operator completes the interactive browser login;
the redirect lands here with the single-use ``?code=``, which is handed to
``exchange_code`` (M1.4). Binds loopback only; never logs the request (the path
contains the code).
"""

from __future__ import annotations

import http.server
import ipaddress
import ssl
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from trader.schwab.errors import SchwabAuthError

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class CallbackResult:
    code: str
    state: str | None


def parse_callback_query(path: str) -> CallbackResult:
    """Parse a callback request path: ``?code=...&state=...`` or ``?error=...``."""
    query = parse_qs(urlparse(path).query)
    if "error" in query:
        raise SchwabAuthError(f"oauth callback error: {query['error'][0]}")
    codes = query.get("code")
    if not codes or not codes[0]:
        raise SchwabAuthError("oauth callback missing code")
    state = query.get("state", [None])[0]
    return CallbackResult(code=codes[0], state=state)


def generate_self_signed_cert(
    cert_path: str | Path, key_path: str | Path, *, host: str = "127.0.0.1"
) -> None:
    """Write a self-signed cert + 0600 private key for the loopback callback."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    try:
        san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        san = x509.DNSName(host)
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .sign(key, hashes.SHA256())
    )
    Path(key_path).write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    Path(key_path).chmod(0o600)
    Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))


class _QuietHTTPServer(http.server.HTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        # Suppress tracebacks on abrupt client disconnects; also avoids any chance
        # of request data reaching stderr.
        pass


class CallbackServer:
    """Serves the loopback HTTPS callback once and yields the captured code."""

    def __init__(
        self,
        certfile: str | Path,
        keyfile: str | Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8182,
        expected_state: str | None = None,
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(f"callback server must bind to loopback only, got {host!r}")
        self._certfile = str(certfile)
        self._keyfile = str(keyfile)
        self._host = host
        self._port = port
        self._expected_state = expected_state
        self._result: CallbackResult | None = None
        self._error: Exception | None = None
        self._event = threading.Event()
        self._httpd: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_host(self) -> str:
        return self._host

    @property
    def bound_port(self) -> int:
        return self._port

    def start(self) -> None:
        httpd = _QuietHTTPServer((self._host, self._port), self._make_handler())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self._certfile, self._keyfile)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self._port = httpd.server_address[1]  # resolve actual port if 0 was requested
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

    def wait_for_code(self, timeout: float = 300.0) -> CallbackResult:
        try:
            if not self._event.wait(timeout):
                raise SchwabAuthError("oauth callback timed out")
            if self._error is not None:
                raise self._error
            assert self._result is not None
            return self._result
        finally:
            self.stop()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def serve_until_code(self, timeout: float = 300.0) -> CallbackResult:
        self.start()
        return self.wait_for_code(timeout)

    def _make_handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        server = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    result = parse_callback_query(self.path)
                    if (
                        server._expected_state is not None
                        and result.state != server._expected_state
                    ):
                        raise SchwabAuthError("oauth callback state mismatch")
                    server._result = result
                except Exception as exc:  # capture; surfaced via wait_for_code
                    server._error = exc
                    self._respond(400, "Authorization failed. You may close this window.")
                else:
                    self._respond(200, "Authorization received. You may close this window.")
                finally:
                    server._event.set()

            def _respond(self, status: int, message: str) -> None:
                body = f"<html><body><p>{message}</p></body></html>".encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                # Never log: the request path contains the single-use auth code.
                pass

        return _Handler
