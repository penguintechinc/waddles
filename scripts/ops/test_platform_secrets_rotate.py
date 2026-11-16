"""Tests for `platform_secrets_rotate.py`: it calls ONLY the admin API, never leaks the token."""

from __future__ import annotations

import io
import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import platform_secrets_rotate as cli
import pytest

TOKEN = "tok-FAKE-never-print-0123456789abcdef"  # nosec B105 -- test fixture


class _Stub(BaseHTTPRequestHandler):
    """Records the request and answers with the configured status/body."""

    seen: ClassVar[list[dict[str, Any]]] = []
    reply: ClassVar[tuple[int, dict[str, Any]]] = (200, {})

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).seen.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        status, payload = type(self).reply
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _handle

    def log_message(self, *_args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def server() -> Iterator[str]:
    _Stub.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    thread.join(timeout=5)


def _run(
    argv: list[str], url: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = cli.main(
        ["--api-url", url, *argv],
        environ={cli.TOKEN_ENV: TOKEN},
        stdin=io.StringIO(""),
    )
    out = capsys.readouterr()
    return code, out.out, out.err


def test_rotate_calls_the_admin_api_with_idempotency_key_and_never_prints_the_token(
    server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _Stub.reply = (
        200,
        {
            "success": True,
            "type": "jwt_signing",
            "newVersion": 2,
            "previousVersion": 1,
            "previousVerifyUntil": "2026-11-14T12:00:00+00:00",
        },
    )
    code, out, err = _run(
        ["rotate", "jwt_signing", "--reason", "compromise", "--yes"], server, capsys
    )
    assert code == cli.EXIT_OK and "v1 -> v2" in out
    (req,) = _Stub.seen
    assert (req["method"], req["path"]) == (
        "POST",
        "/api/v1/admin/platform-secrets/jwt_signing/rotate",
    )
    assert json.loads(req["body"]) == {"reasonCode": 2}
    assert req["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert req["headers"]["x-requested-with"] == "waddles-admin"
    assert uuid.UUID(req["headers"]["idempotency-key"])
    assert TOKEN not in out and TOKEN not in err


def test_retire_sends_version_and_a_caller_chosen_key(
    server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _Stub.reply = (
        200,
        {
            "success": True,
            "type": "jwt_signing",
            "version": 1,
            "retiredAt": "2026-11-14T13:00:00+00:00",
        },
    )
    key = str(uuid.uuid4())
    code, out, _ = _run(
        ["retire", "jwt_signing", "--version", "1", "--reason", "scheduled", "--yes",
         "--idempotency-key", key],
        server,
        capsys,
    )  # fmt: skip
    assert code == cli.EXIT_OK and "retired jwt_signing v1" in out
    (req,) = _Stub.seen
    assert (
        req["path"].endswith("/jwt_signing/retire")
        and req["headers"]["idempotency-key"] == key
    )
    assert json.loads(req["body"]) == {"reasonCode": 1, "version": 1}


def test_status_is_a_get_and_summarises_versions(
    server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _Stub.reply = (
        200,
        {"success": True, "types": [{"type": "jwt_signing", "typeCode": 3,
                                      "versions": [{"version": 1, "state": 3}, {"version": 2, "state": 2}]}]},
    )  # fmt: skip
    code, out, _ = _run(["status"], server, capsys)
    assert code == cli.EXIT_OK and "jwt_signing: v1:state3, v2:state2" in out
    assert (
        _Stub.seen[0]["method"] == "GET"
        and "idempotency-key" not in _Stub.seen[0]["headers"]
    )


def test_an_api_refusal_exits_1_with_the_stable_code(
    server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _Stub.reply = (
        403,
        {"success": False, "error": {"code": "MFA_REQUIRED", "message": "no"}},
    )
    code, out, _ = _run(
        ["rotate", "jwt_signing", "--reason", "scheduled", "--yes"], server, capsys
    )
    assert code == cli.EXIT_REFUSED and "MFA_REQUIRED" in out and TOKEN not in out


def test_usage_errors_exit_2_and_never_touch_the_network(
    server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    no_yes, _, err = _run(
        ["rotate", "jwt_signing", "--reason", "scheduled"], server, capsys
    )
    assert no_yes == cli.EXIT_USAGE and "--yes" in err
    bad_key, _, _ = _run(
        [
            "rotate",
            "jwt_signing",
            "--reason",
            "scheduled",
            "--yes",
            "--idempotency-key",
            "nope",
        ],
        server,
        capsys,
    )
    assert bad_key == cli.EXIT_USAGE
    missing = cli.main(
        ["--api-url", server, "status"], environ={}, stdin=io.StringIO("")
    )
    assert missing == cli.EXIT_USAGE
    plain_http = cli.main(
        ["--api-url", "http://example.invalid", "status"],
        environ={cli.TOKEN_ENV: TOKEN},
        stdin=io.StringIO(""),
    )
    assert plain_http == cli.EXIT_USAGE
    assert _Stub.seen == []  # none of the above reached the server
    capsys.readouterr()


def test_there_is_no_token_command_line_option() -> None:
    help_text = cli.build_parser().format_help()
    assert "--token " not in help_text and "--token-stdin" in help_text
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--token", "x", "status"])


def test_token_can_come_from_stdin(
    server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _Stub.reply = (200, {"success": True, "types": []})
    code = cli.main(
        ["--api-url", server, "--token-stdin", "status"],
        environ={},
        stdin=io.StringIO(TOKEN + "\n"),
    )
    capsys.readouterr()
    assert (
        code == cli.EXIT_OK
        and _Stub.seen[0]["headers"]["authorization"] == f"Bearer {TOKEN}"
    )


def test_unreachable_api_exits_3(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        ["--api-url", "http://127.0.0.1:9", "status"],
        environ={cli.TOKEN_ENV: TOKEN},
        stdin=io.StringIO(""),
    )
    assert code == cli.EXIT_NETWORK and TOKEN not in capsys.readouterr().err


def test_the_cli_has_no_database_access() -> None:
    source = (Path(cli.__file__)).read_text()
    for forbidden in (
        "sqlalchemy",
        "psycopg",
        "platform_vault",
        "DATABASE_URL",
        "TENANT_KEK",
    ):
        assert forbidden not in source, forbidden
