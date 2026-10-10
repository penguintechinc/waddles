"""Log-redaction regression tests for the credential/OAuth/crypto paths.

SECURITY (HIGH, PII/secret in logs; consumer side of #766): these paths used to
log `f"...{e}"` / `"...%s", exc` / `str(e)` of the raw exception. An `httpx`
error (or any wrapper around one) can carry the request URL/body -- which for an
OAuth refresh holds `client_secret` and `refresh_token` -- and a crypto error can
echo key/ciphertext fragments. Every test plants a sentinel where a real failure
would put data and asserts it never appears in captured log output (message,
`extra`, or a rendered traceback), nor in the raised `OAuthRefreshError`.

Fail-first proof (executed, not narrated): with the pre-fix `oauth_handlers.py` /
`token_crypto.py` / `refresh_service.py` restored, every test in this file that
plants a sentinel fails (the sentinel appears in the captured log text); with the
fix it passes. See the PR description for the exact before/after run.
"""

from __future__ import annotations

import logging
import os
import traceback
from collections.abc import Callable

import httpx
import pytest

os.environ.setdefault(
    "CREDENTIAL_ENCRYPTION_KEY", "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"
)

from . import refresh_service, token_crypto  # noqa: E402 - env var set first
from .oauth_handlers import OAuthRefreshError, get_handler  # noqa: E402
from .refresh_service import RefreshService  # noqa: E402
from .token_crypto import (  # noqa: E402
    TokenCryptoError,
    decrypt_if_needed,
    encrypt_value,
)

CLIENT_SECRET = "SENTINEL-client-secret-7f31"
REFRESH_TOKEN = "SENTINEL-refresh-token-c20a"
SENTINELS = (CLIENT_SECRET, REFRESH_TOKEN)

PLATFORMS = ("twitch", "discord", "slack", "youtube", "spotify", "kick")


def _all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Everything this service's loggers emitted: message, rendered traceback, `extra`.

    The `httpx`/`httpcore` library loggers are excluded: httpx itself logs
    `HTTP Request: POST <url>` at INFO. That is independent of the code under test,
    and the handlers' token URLs are fixed constants with no query-string secrets in
    production (the secret-bearing URL in one test below is a deliberate probe).
    """
    formatter = logging.Formatter()
    return "\n".join(
        formatter.format(r) + repr(r.__dict__)
        for r in caplog.records
        if not r.name.startswith(("httpx", "httpcore"))
    )


def _assert_no_sentinel(text: str) -> None:
    for sentinel in SENTINELS:
        assert sentinel not in text, f"{sentinel} leaked into: {text}"


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, responder: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Route the handlers' `httpx.AsyncClient` through an in-process `MockTransport`."""
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(responder)

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def _refresh(platform: str) -> OAuthRefreshError:
    """Run one refresh expecting failure; return the raised error."""
    handler = get_handler(platform)
    with pytest.raises(OAuthRefreshError) as excinfo:
        await handler.refresh_token(
            refresh_token=REFRESH_TOKEN, client_id="cid", client_secret=CLIENT_SECRET
        )
    return excinfo.value


def _assert_error_is_clean(error: OAuthRefreshError) -> None:
    """The error message, its repr and its rendered traceback chain must be sentinel-free."""
    _assert_no_sentinel(str(error))
    _assert_no_sentinel(repr(error))
    _assert_no_sentinel("".join(traceback.format_exception(error)))


@pytest.mark.parametrize("platform", PLATFORMS)
class TestOAuthRefreshFailureIsRedacted:
    """Each platform handler, on each failure shape a real token endpoint produces."""

    async def test_transport_error_echoing_the_request_body(
        self, platform: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An error whose message echoes the form body (secret + refresh token)."""
        seen_bodies: list[str] = []

        def responder(request: httpx.Request) -> httpx.Response:
            body = request.content.decode()
            seen_bodies.append(body)
            raise httpx.ConnectError(f"connect failed, body={body}", request=request)

        _install_transport(monkeypatch, responder)
        caplog.set_level(logging.DEBUG)

        error = await _refresh(platform)

        # Precondition (a test that cannot fail is not a test): the leak vector is real --
        # the form body (or Spotify's Basic header) does carry the secret into the exception.
        assert seen_bodies and REFRESH_TOKEN in seen_bodies[0]
        _assert_error_is_clean(error)
        _assert_no_sentinel(_all_log_text(caplog))
        assert "category=network_connect" in str(error)

    async def test_http_status_error_with_secret_in_the_url(
        self, platform: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`raise_for_status()` messages embed the full request URL."""
        handler_cls = type(get_handler(platform))
        monkeypatch.setattr(
            handler_cls, "TOKEN_URL", f"https://idp.example/token?client_secret={CLIENT_SECRET}"
        )
        _install_transport(monkeypatch, lambda request: httpx.Response(401, request=request))
        caplog.set_level(logging.DEBUG)

        error = await _refresh(platform)

        _assert_error_is_clean(error)
        _assert_no_sentinel(_all_log_text(caplog))
        assert "code=401" in str(error) and "category=http_status" in str(error)

    async def test_unexpected_handler_exception_carrying_the_secret(
        self, platform: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The per-handler `except Exception` branch (the old `f"...{str(e)}"` + `logger.error`)."""
        handler = get_handler(platform)

        async def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError(f"unexpected: {CLIENT_SECRET} {REFRESH_TOKEN}")

        monkeypatch.setattr(handler, "_post_form", boom)
        caplog.set_level(logging.DEBUG)

        with pytest.raises(OAuthRefreshError) as excinfo:
            await handler.refresh_token(
                refresh_token=REFRESH_TOKEN, client_id="cid", client_secret=CLIENT_SECRET
            )

        _assert_error_is_clean(excinfo.value)
        text = _all_log_text(caplog)
        _assert_no_sentinel(text)
        assert "token refresh failed: type=builtins.RuntimeError" in text

    async def test_non_json_success_body(
        self, platform: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(
                200, request=request, content=f"<html>{CLIENT_SECRET}</html>".encode()
            ),
        )
        caplog.set_level(logging.DEBUG)

        error = await _refresh(platform)

        _assert_error_is_clean(error)
        _assert_no_sentinel(_all_log_text(caplog))
        assert "category=decode_error" in str(error)


class TestSlackErrorCode:
    """Slack's `error` field is remote data that flows into `OAuthRefreshError` (and logs)."""

    async def test_known_code_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(
                200, request=request, json={"ok": False, "error": "invalid_refresh_token"}
            ),
        )
        assert str(await _refresh("slack")) == "invalid_refresh_token"

    @pytest.mark.parametrize(
        "value", [f"bad token {CLIENT_SECRET}", CLIENT_SECRET.upper(), "", None, 7, ["x"]]
    )
    async def test_non_snake_case_code_is_replaced(
        self, value: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(
                200, request=request, json={"ok": False, "error": value}
            ),
        )
        error = await _refresh("slack")
        assert str(error) == "Unknown error"
        _assert_error_is_clean(error)

    async def test_missing_code_keeps_the_original_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, request=request, json={"ok": False})
        )
        assert str(await _refresh("slack")) == "Unknown error"


class TestDecryptFailureIsRedacted:
    """`token_crypto.decrypt_if_needed` logs a failed decrypt without key/ciphertext text."""

    def test_real_tampered_ciphertext(self, caplog: pytest.LogCaptureFixture) -> None:
        good = encrypt_value("the-live-oauth-token")
        tampered = good[:-4] + ("AAAA" if good[-4:] != "AAAA" else "BBBB")
        caplog.set_level(logging.DEBUG)

        assert decrypt_if_needed(tampered, is_encrypted=True) == tampered  # raw fallthrough kept

        text = _all_log_text(caplog)
        assert tampered not in text and good not in text
        assert os.environ["CREDENTIAL_ENCRYPTION_KEY"] not in text
        assert "Failed to decrypt credential value type=" in text
        assert "category=crypto_auth_failed" in text  # classified via the InvalidTag cause

    def test_exception_message_with_secret_is_never_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def explode(_value: str) -> str:
            raise TokenCryptoError(f"key={CLIENT_SECRET} ciphertext={REFRESH_TOKEN}")

        monkeypatch.setattr(token_crypto, "decrypt_value", explode)
        caplog.set_level(logging.DEBUG)

        assert decrypt_if_needed("blob", is_encrypted=True) == "blob"

        _assert_no_sentinel(_all_log_text(caplog))
        assert "type=" in _all_log_text(caplog)


class TestRefreshServiceErrorPath:
    """`RefreshService` call sites around the handler/refresh cycle."""

    @pytest.fixture
    def service(self) -> RefreshService:
        return RefreshService(
            database_url="postgresql://u:p@localhost/db", redis_url="redis://localhost"
        )

    @pytest.mark.parametrize("exc_type", [RuntimeError, ValueError, KeyError])
    async def test_call_refresh_endpoint_failure_is_redacted(
        self,
        service: RefreshService,
        exc_type: type[Exception],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class ExplodingHandler:
            async def refresh_token(self, **_kwargs: object) -> dict[str, object]:
                raise exc_type(f"leak {CLIENT_SECRET} {REFRESH_TOKEN}")

        monkeypatch.setattr(refresh_service, "get_handler", lambda _platform: ExplodingHandler())
        caplog.set_level(logging.DEBUG)

        result = await service._call_refresh_endpoint(
            "twitch",
            "https://id.twitch.tv/oauth2/token",
            {"id": 1, "refresh_token": REFRESH_TOKEN, "client_secret": CLIENT_SECRET},
        )

        assert result is None
        text = _all_log_text(caplog)
        _assert_no_sentinel(text)
        assert exc_type.__name__ in text  # the type is still diagnosable

    async def test_poll_loop_failure_is_redacted(
        self,
        service: RefreshService,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def failing_cycle(self: RefreshService) -> int:
            self._running = False  # run exactly one iteration
            raise RuntimeError(f"db said {CLIENT_SECRET} {REFRESH_TOKEN}")

        monkeypatch.setattr(RefreshService, "run_refresh_cycle", failing_cycle)
        service._running = True
        service._poll_interval = 0
        caplog.set_level(logging.DEBUG)

        await service._poll_loop()

        text = _all_log_text(caplog)
        _assert_no_sentinel(text)
        assert "Error in refresh cycle type=builtins.RuntimeError" in text
        assert service._total_errors == 1
