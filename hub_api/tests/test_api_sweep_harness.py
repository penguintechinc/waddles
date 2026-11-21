"""Unit tests for the alpha API sweep harness (`tests/e2e/api-sweep/api_sweep.py`, `lib/access.sh`).

Covers the harness fixes from the alpha security triage: must-fail probes never follow redirects
(a 3xx to an `error=` URL is a correct rejection), the explicit empty-body-OK route list, a sweep
that examined 0 routes fails, and `access.sh` never leaves `BASE_URL` empty.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SWEEP = ROOT / "tests" / "e2e" / "api-sweep" / "api_sweep.py"
ACCESS_SH = ROOT / "tests" / "e2e" / "lib" / "access.sh"

_spec = importlib.util.spec_from_file_location("api_sweep_under_test", SWEEP)
assert _spec is not None and _spec.loader is not None
sweep = importlib.util.module_from_spec(_spec)
sys.modules["api_sweep_under_test"] = sweep
_spec.loader.exec_module(sweep)


class _Handler(BaseHTTPRequestHandler):
    """Tiny stub hub-api: login, a redirecting error route, a redirecting success route."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, D102
        return

    def _send(self, status: int, body: bytes = b"{}", location: str | None = None) -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802, D102
        if self.path == "/api/v1/auth/login":
            self._send(200, json.dumps({"token": "t"}).encode())
        elif self.path == "/redir-error":
            self._send(302, location="/dashboard/settings?error=link_failed")
        elif self.path == "/redir-ok":
            self._send(302, location="/dashboard")
        else:
            self._send(200)

    do_PUT = do_PATCH = do_DELETE = do_GET = do_POST  # noqa: N815


@pytest.fixture
def stub_server() -> Any:
    """Run the stub on a loopback port; yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class TestClassifyMustFail:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
    def test_validation_statuses_pass(self, status: int) -> None:
        assert sweep.classify_must_fail("POST", "/x", status)[0] == "PASS"

    @pytest.mark.parametrize("status", [0, 500, 502])
    def test_server_errors_fail(self, status: int) -> None:
        assert sweep.classify_must_fail("POST", "/x", status)[0] == "FAIL"

    def test_200_on_must_fail_probe_fails(self) -> None:
        cls, why = sweep.classify_must_fail("PUT", "/api/v1/platform/users/{user_id}/role", 200)
        assert cls == "FAIL" and "accepted" in why

    def test_redirect_to_error_url_is_correct_rejection(self) -> None:
        cls, _ = sweep.classify_must_fail("POST", "/x", 302, "/settings?error=link_failed")
        assert cls == "PASS"

    def test_redirect_to_non_error_url_fails(self) -> None:
        assert sweep.classify_must_fail("POST", "/x", 302, "/dashboard")[0] == "FAIL"
        assert sweep.classify_must_fail("POST", "/x", 302, "")[0] == "FAIL"

    def test_empty_body_ok_route_may_return_200(self) -> None:
        assert ("PATCH", "/api/v1/cookie/preferences") in sweep.EMPTY_BODY_OK_ROUTES
        cls, why = sweep.classify_must_fail("PATCH", "/api/v1/cookie/preferences", 200)
        assert cls == "PASS" and "legitimate" in why

    def test_empty_body_ok_is_per_route_not_global(self) -> None:
        assert sweep.classify_must_fail("PUT", "/api/v1/cookie/preferences", 200)[0] == "FAIL"
        assert sweep.classify_must_fail("PATCH", "/api/v1/other", 200)[0] == "FAIL"


class TestClientRedirects:
    def test_must_fail_probe_does_not_follow_redirect(self, stub_server: str) -> None:
        client = sweep.Client(stub_server, None, 1000.0, False)
        status, _, _ = client.call(
            "POST", "/redir-error", b"{}", "application/json", follow_redirects=False
        )
        assert status == 302
        assert "error=" in client.last_location

    def test_non_error_redirect_is_surfaced_not_followed(self, stub_server: str) -> None:
        client = sweep.Client(stub_server, None, 1000.0, False)
        status, _, _ = client.call(
            "POST", "/redir-ok", b"{}", "application/json", follow_redirects=False
        )
        assert (status, client.last_location) == (302, "/dashboard")
        assert (
            sweep.classify_must_fail("POST", "/redir-ok", status, client.last_location)[0] == "FAIL"
        )

    def test_default_calls_still_follow_redirects(self, stub_server: str) -> None:
        client = sweep.Client(stub_server, None, 1000.0, False)
        # GET /redir-error -> 302 -> GET /dashboard/settings?error=... (stub answers 200).
        status, _, _ = client.call("GET", "/redir-error")
        assert status == 200


def _run_sweep(spec: dict[str, Any], base: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(json.dumps(spec))  # JSON is valid YAML
    env = {
        **os.environ,
        "BASE_URL": base,
        "HUB_TEST_EMAIL": "a@example.com",
        "HUB_TEST_PASS": "pw",
    }
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SWEEP),
            "--spec",
            str(spec_file),
            "--report-dir",
            str(tmp_path / "r"),
            "--rate",
            "1000",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class TestZeroRoutes:
    def test_sweep_fails_when_every_operation_is_skipped(
        self, stub_server: str, tmp_path: Path
    ) -> None:
        """A parameterless DELETE is skipped, so nothing is examined -> hard failure."""
        spec = {"paths": {"/api/v1/thing": {"delete": {"responses": {"200": {}}}}}}
        result = _run_sweep(spec, stub_server, tmp_path)
        assert result.returncode == 2
        assert "examined 0 routes" in result.stderr

    def test_sweep_passes_when_a_route_was_examined(self, stub_server: str, tmp_path: Path) -> None:
        spec = {"paths": {"/api/v1/things": {"get": {"responses": {"200": {}}}}}}
        result = _run_sweep(spec, stub_server, tmp_path)
        assert result.returncode == 0, result.stderr
        assert '"examined": 1' in result.stdout


def _bash(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if k not in ("BASE_URL", "HUB_TEST_PASS")}
    full.update(env)
    return subprocess.run(  # noqa: S603
        ["bash", "-c", snippet],  # noqa: S607
        env=full,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class TestAccessSh:
    def test_sourced_alone_with_base_url_keeps_it(self) -> None:
        result = _bash(
            f'source "{ACCESS_SH}" && echo "BASE=$BASE_URL"',
            {"BASE_URL": "http://example.test", "HUB_TEST_PASS": "pw"},
        )
        assert result.returncode == 0, result.stderr
        assert "BASE=http://example.test" in result.stdout

    def test_sourced_alone_without_base_url_fails_loudly(self, tmp_path: Path) -> None:
        """No BASE_URL and no reachable cluster: source must fail, never leave BASE_URL empty."""
        fake = tmp_path / "bin"
        fake.mkdir()
        kubectl = fake / "kubectl"
        kubectl.write_text("#!/bin/sh\nexit 1\n")
        kubectl.chmod(0o755)
        result = _bash(
            f'source "{ACCESS_SH}" && echo "UNEXPECTED BASE=[$BASE_URL]"',
            {
                "PATH": f"{fake}:{os.environ['PATH']}",
                "HUB_TEST_PASS": "pw",
                "SWEEP_LOCAL_PORT": "18999",
            },
        )
        assert result.returncode != 0
        assert "UNEXPECTED" not in result.stdout
        assert "access.sh" in result.stderr

    def test_require_base_url_fails_when_empty(self) -> None:
        result = _bash(
            f'SWEEP_ACCESS_LAZY=1 source "{ACCESS_SH}" && sweep_require_base_url',
            {},
        )
        assert result.returncode == 1
        assert "BASE_URL is unset" in result.stderr

    def test_lazy_source_does_not_resolve_access(self) -> None:
        result = _bash(
            f'SWEEP_ACCESS_LAZY=1 source "{ACCESS_SH}" && echo "BASE=[${{BASE_URL:-}}]"', {}
        )
        assert result.returncode == 0
        assert "BASE=[]" in result.stdout
