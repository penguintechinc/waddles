"""Pass/fail/retry/zero-examined cases for scripts/ci/hub_api_probe.py against a stub HTTP server.

Real sockets on 127.0.0.1 (no mocked transport); each case asserts the printed counts and the exit code.
"""

import contextlib
import datetime
import io
import pathlib
import socket
import ssl
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

CI = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))
import hub_api_probe as probe  # noqa: E402

try:
    import cryptography  # noqa: F401

    _HAVE_CRYPTO = True
except ImportError:  # some CI jobs (e.g. bundle-hygiene) run without cryptography installed
    _HAVE_CRYPTO = False

# The real-TLS roundtrip tests need a throwaway cert, which only cryptography can mint here; skip
# where it is absent. The dependency-free tests below (build_ssl_context, http, transport-error)
# still cover the insecure-context wiring and status-fail behavior in every job.
requires_crypto = pytest.mark.skipif(not _HAVE_CRYPTO, reason="cryptography not installed in this CI job")


def _server(routes: dict[str, list[int]]) -> ThreadingHTTPServer:
    """Start a stub server; routes maps path -> queue of statuses (last one repeats)."""
    seen: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            queue = routes.get(self.path, [404])
            idx = seen.get(self.path, 0)
            seen[self.path] = idx + 1
            status = queue[min(idx, len(queue) - 1)]
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run(argv: list[str]) -> tuple[int, str]:
    """Run probe.main capturing stdout; return (exit code, stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = probe.main(argv)
    return rc, buf.getvalue()


def _run_with_stderr(argv: list[str]) -> tuple[int, str, str]:
    """Run probe.main capturing stdout and stderr; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = probe.main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def stub() -> object:
    """Yield a started server; shut it down after the test."""
    holder: dict[str, ThreadingHTTPServer] = {}

    def start(routes: dict[str, list[int]]) -> str:
        holder["srv"] = _server(routes)
        return f"http://127.0.0.1:{holder['srv'].server_address[1]}"

    yield start
    if "srv" in holder:
        holder["srv"].shutdown()
        holder["srv"].server_close()


def test_pass_case_all_200(stub: object) -> None:
    """Both endpoints return 200 on the first attempt: exit 0, examined=2 ok=2."""
    base = stub({"/health": [200], "/ready": [200]})  # type: ignore[operator]
    rc, out = _run(["--base-url", base, "--paths", "/health", "/ready", "--delay", "0"])
    assert rc == 0
    assert "endpoints examined=2 ok=2 failed=0" in out


def test_fail_case_ready_503(stub: object) -> None:
    """/ready never returns 200 within the attempt budget: exit 1, failed=1."""
    base = stub({"/health": [200], "/ready": [503]})  # type: ignore[operator]
    rc, out = _run(["--base-url", base, "--paths", "/health", "/ready", "--attempts", "3", "--delay", "0"])
    assert rc == 1
    assert "GET /ready status=503 attempts=3 FAIL" in out
    assert "endpoints examined=2 ok=1 failed=1" in out


def test_retry_then_pass(stub: object) -> None:
    """Early 503s then 200 (hub-api still booting): passes on attempt 3."""
    base = stub({"/ready": [503, 503, 200]})  # type: ignore[operator]
    rc, out = _run(["--base-url", base, "--paths", "/ready", "--attempts", "5", "--delay", "0"])
    assert rc == 0
    assert "GET /ready status=200 attempts=3 PASS" in out


def test_connection_refused_fails_loud() -> None:
    """Nothing listening (port-forward dead): status 0, exit 1, never a silent pass."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    rc, out = _run(["--base-url", f"http://127.0.0.1:{port}", "--paths", "/health", "--attempts", "2", "--delay", "0"])
    assert rc == 1
    assert "GET /health status=0 attempts=2 FAIL" in out


def test_zero_paths_is_usage_error() -> None:
    """No paths means zero endpoints examined: argparse rejects it (non-zero exit), never a pass."""
    with pytest.raises(SystemExit) as exc:
        _run(["--base-url", "http://127.0.0.1:1"])
    assert exc.value.code != 0


def test_relative_path_rejected() -> None:
    """A path without a leading slash is a usage error, not a probe of a malformed URL."""
    with pytest.raises(SystemExit) as exc:
        _run(["--base-url", "http://127.0.0.1:1", "--paths", "health"])
    assert exc.value.code != 0


def _self_signed_cert(dirpath: pathlib.Path) -> tuple[str, str]:
    """Write a throwaway self-signed ECDSA cert whose SAN is a service FQDN (never 127.0.0.1).

    Mirrors the real hub-api cert (SAN = in-cluster FQDN only), so verification against the
    port-forward loopback host can never match -- exactly the case --insecure exists for.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "waddlebot-hub-api-v3")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("waddlebot-hub-api-v3.waddlebot.svc")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = dirpath / "cert.pem"
    key_path = dirpath / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


@pytest.fixture
def tls_stub() -> object:
    """Yield a started HTTPS stub (self-signed, FQDN-only SAN); shut it down after the test."""
    holder: dict[str, ThreadingHTTPServer] = {}
    tmp = tempfile.TemporaryDirectory()

    def start(routes: dict[str, list[int]]) -> str:
        srv = _server(routes)
        cert, key = _self_signed_cert(pathlib.Path(tmp.name))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        holder["srv"] = srv
        return f"https://127.0.0.1:{srv.server_address[1]}"

    yield start
    if "srv" in holder:
        holder["srv"].shutdown()
        holder["srv"].server_close()
    tmp.cleanup()


def test_build_ssl_context_default_verifies() -> None:
    """Without --insecure, no override context is built (urllib's verifying default is used)."""
    assert probe.build_ssl_context(False) is None


def test_build_ssl_context_insecure_skips_verification() -> None:
    """--insecure yields a context that skips cert AND hostname checks (curl -k equivalent)."""
    ctx = probe.build_ssl_context(True)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


@requires_crypto
def test_https_insecure_passes_on_200(tls_stub: object) -> None:
    """https against the FQDN-only self-signed cert succeeds with --insecure: examined=2 ok=2."""
    base = tls_stub({"/health": [200], "/ready": [200]})  # type: ignore[operator]
    rc, out = _run(["--base-url", base, "--paths", "/health", "/ready", "--delay", "0", "--insecure"])
    assert rc == 0
    assert "endpoints examined=2 ok=2 failed=0" in out


@requires_crypto
def test_https_insecure_still_fails_on_non_200(tls_stub: object) -> None:
    """--insecure skips TLS verification only -- a real non-200 over https still fails the gate."""
    base = tls_stub({"/health": [200], "/ready": [503]})  # type: ignore[operator]
    rc, out = _run(
        ["--base-url", base, "--paths", "/health", "/ready", "--attempts", "3", "--delay", "0", "--insecure"]
    )
    assert rc == 1
    assert "GET /ready status=503 attempts=3 FAIL" in out
    assert "endpoints examined=2 ok=1 failed=1" in out


@requires_crypto
def test_https_without_insecure_fails_verification(tls_stub: object) -> None:
    """Default (verifying) context rejects the self-signed cert: status 0, exit 1, never a silent pass."""
    base = tls_stub({"/health": [200]})  # type: ignore[operator]
    rc, out = _run(["--base-url", base, "--paths", "/health", "--attempts", "2", "--delay", "0"])
    assert rc == 1
    assert "GET /health status=0 attempts=2 FAIL" in out


def test_transport_error_logs_exception_class_to_stderr() -> None:
    """A swallowed status-0 still prints the exception class+message to stderr (diagnosable)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    rc, out, err = _run_with_stderr(
        ["--base-url", f"http://127.0.0.1:{port}", "--paths", "/health", "--attempts", "1", "--delay", "0"]
    )
    assert rc == 1
    assert "GET /health status=0 attempts=1 FAIL" in out
    assert "transport error" in err
    assert "URLError" in err  # connection refused surfaces as a URLError, class named in the log
