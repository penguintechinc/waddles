"""Pass/fail/retry/zero-examined cases for scripts/ci/hub_api_probe.py against a stub HTTP server.

Real sockets on 127.0.0.1 (no mocked transport); each case asserts the printed counts and the exit code.
"""

import contextlib
import io
import pathlib
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

CI = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))
import hub_api_probe as probe  # noqa: E402


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
