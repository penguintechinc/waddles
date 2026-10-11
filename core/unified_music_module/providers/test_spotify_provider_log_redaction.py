"""PII / secret log-redaction tests for SpotifyProvider.

# regression: SpotifyProvider logged exception text on every failure path --
# ``logger.error(f"... {e.response.text}")`` (Spotify's free-text error body can echo the
# request: auth code, refresh token, search text), ``f"{e}"`` where ``str(httpx.HTTPStatusError)``
# is the full request URL (``q=`` search text, ``device_id=``), and it raised
# ``SpotifyAPIError(f"... {e}")`` carrying the same text to callers. httpx also logs the
# full URL of every request at INFO.

Each test binds unique sentinels (user search text, bearer token, auth code, refresh token,
client secret), serves a hostile provider response that echoes them, drives the REAL provider
method through a real ``httpx.AsyncClient`` (MockTransport -- no network), and asserts no
sentinel appears in any captured log record (message, args, rendered exception, httpx's own
request line) nor in the raised error.
"""

import asyncio
import importlib.util
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from providers.spotify_provider import SpotifyAPIError, SpotifyAuthError, SpotifyProvider

QUERY = "SENTINEL-search-text-7c41e2"
ACCESS_TOKEN = "SENTINEL-bearer-token-5a90b1"
AUTH_CODE = "SENTINEL-auth-code-31d8f7"
REFRESH_TOKEN = "SENTINEL-refresh-token-c27e64"
CLIENT_SECRET = "SENTINEL-client-secret-9e03aa"
DEVICE_ID = "SENTINEL-device-id-4f6b12"
SENTINELS = (QUERY, ACCESS_TOKEN, AUTH_CODE, REFRESH_TOKEN, CLIENT_SECRET, DEVICE_ID)

Handler = Callable[[httpx.Request], httpx.Response]

ECHO = f"echo q={QUERY} code={AUTH_CODE} rt={REFRESH_TOKEN} secret={CLIENT_SECRET} dev={DEVICE_ID}"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the provider's required environment (client secret is a sentinel)."""
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://localhost/callback")


def _provider(handler: Handler, *, authed: bool = True) -> Any:
    """Build a provider whose HTTP client is served by `handler` (no network)."""
    factory: Any = SpotifyProvider  # the legacy provider's constructor is unannotated
    provider = factory()
    provider.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if authed:
        provider.access_token = ACCESS_TOKEN
        provider.refresh_token = REFRESH_TOKEN
        provider.active_device_id = DEVICE_ID
    return provider


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """Render every captured record: message, raw args and formatted exception text."""
    formatter = logging.Formatter()
    return "\n".join(
        f"{formatter.format(r)}|{r.getMessage()}|{r.args!r}|{r.msg!r}" for r in caplog.records
    )


def _assert_clean(text: str) -> None:
    """No sentinel (user text, token, code, secret, device id) may appear."""
    for sentinel in SENTINELS:
        assert sentinel not in text, f"{sentinel} leaked"


def _assert_unchained(error: BaseException) -> None:
    """The raised error must not chain the httpx error: its text is the full request URL.

    ``raise ... from None`` clears ``__cause__`` *and* sets ``__suppress_context__`` so a
    caller's ``exc_info`` / ``logger.exception`` cannot re-print the URL via the chain.
    """
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def _api_error(request: httpx.Request) -> httpx.Response:
    """Return a 403 Web API error whose free text echoes every sentinel."""
    return httpx.Response(
        403,
        json={
            "error": {
                "status": 403,
                "message": ECHO,
                "reason": "PREMIUM_REQUIRED",
            }
        },
    )


def _oauth_error(request: httpx.Request) -> httpx.Response:
    """Return a 400 OAuth2 token-endpoint error whose description echoes every sentinel."""
    return httpx.Response(400, json={"error": "invalid_grant", "error_description": ECHO})


def _connect_error(request: httpx.Request) -> httpx.Response:
    """Raise a transport error whose message embeds the URL and every sentinel."""
    raise httpx.ConnectError(f"cannot reach {request.url} {ECHO}", request=request)


def _runtime_error(request: httpx.Request) -> httpx.Response:
    """Raise a non-httpx failure whose message embeds every sentinel."""
    raise RuntimeError(f"boom {ECHO}")


def _html_error(request: httpx.Request) -> httpx.Response:
    """Return a non-JSON 502 whose body echoes every sentinel."""
    return httpx.Response(502, text=f"<html>bad gateway {ECHO}</html>")


def _search_ok(request: httpx.Request) -> httpx.Response:
    """Serve a one-track search result."""
    return httpx.Response(
        200,
        json={
            "tracks": {
                "items": [
                    {
                        "id": "t1",
                        "name": "Song",
                        "uri": "spotify:track:t1",
                        "artists": [{"name": "Artist"}],
                        "album": {"name": "Album", "images": []},
                    }
                ]
            }
        },
    )


def _track_ok(request: httpx.Request) -> httpx.Response:
    """Serve a single track for GET /tracks/<id>."""
    return httpx.Response(
        200,
        json={
            "id": "t1",
            "name": "Song",
            "uri": "spotify:track:t1",
            "artists": [{"name": "Artist"}],
            "album": {"name": "Album", "images": []},
        },
    )


def _token_ok(request: httpx.Request) -> httpx.Response:
    """Serve a successful token response."""
    return httpx.Response(
        200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
    )


def test_httpx_request_lines_are_off_by_default(caplog: pytest.LogCaptureFixture) -> None:
    """Real requests through httpx emit no INFO request line: it would carry the URL.

    The URL's query holds ``q=`` text and the path holds a caller-supplied track id.
    """
    provider = _provider(_search_ok)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(provider.search(QUERY, limit=1))
        track = asyncio.run(_provider(_track_ok).get_track(QUERY))  # id lands in the URL path

    assert track is not None

    assert logging.getLogger("httpx").level >= logging.WARNING
    assert [r for r in caplog.records if r.name == "httpx"] == []
    _assert_clean(_everything_logged(caplog))


def test_httpx_request_line_never_carries_the_query_even_if_info_is_reenabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defence in depth: with httpx INFO switched back on, the query is still stripped."""
    provider = _provider(_search_ok)
    with caplog.at_level(logging.INFO, logger="httpx"):
        tracks = asyncio.run(provider.search(QUERY, limit=1))

    assert len(tracks) == 1
    request_lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert request_lines, "httpx must have logged the request (else this test proves nothing)"
    assert any("api.spotify.com/v1/search" in line for line in request_lines)
    _assert_clean(_everything_logged(caplog))


def test_original_bug_shape_is_present_in_raw_httpx_error() -> None:
    """Sanity: the sentinels really are in the raw error text this fix suppresses."""
    request = httpx.Request(
        "GET", f"https://api.spotify.com/v1/search?q={QUERY}&device_id={DEVICE_ID}"
    )
    try:
        httpx.Response(403, request=request).raise_for_status()
    except httpx.HTTPStatusError as exc:
        raw = str(exc)
    assert QUERY in raw
    assert DEVICE_ID in raw


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (_api_error, ["status=403", "reason=PREMIUM_REQUIRED"]),
        (_connect_error, ["ConnectError"]),
        (_runtime_error, ["RuntimeError"]),
        (_html_error, ["status=502"]),
    ],
)
def test_search_failure_logs_and_raises_only_safe_description(
    caplog: pytest.LogCaptureFixture, handler: Handler, expected: list[str]
) -> None:
    """Every _make_request failure shape: type/status/reason only, no chained httpx error."""
    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpotifyAPIError) as excinfo:
        asyncio.run(provider.search(QUERY))

    text = _everything_logged(caplog)
    _assert_clean(text)
    _assert_clean(str(excinfo.value))
    for fragment in expected:
        assert fragment in text, "the log line must stay useful"
        assert fragment in str(excinfo.value)
    # the chained httpx error's message is the full URL (query + ids): must not be chained
    _assert_unchained(excinfo.value)


@pytest.mark.parametrize("handler", [_api_error, _connect_error, _runtime_error, _html_error])
@pytest.mark.parametrize(
    ("call", "label"),
    [
        (lambda p: p.play("track-id"), "Failed to start playback"),
        (lambda p: p.pause(), "Failed to pause"),
        (lambda p: p.resume(), "Failed to resume"),
        (lambda p: p.skip(), "Failed to skip"),
        (lambda p: p.set_device(DEVICE_ID), "Failed to set device"),
        (lambda p: p.add_to_queue("track-id"), "Failed to add to queue"),
        (lambda p: p.set_volume(50), "Failed to set volume"),
        (lambda p: p.seek(1000), "Failed to seek"),
    ],
)
def test_control_failures_return_false_without_leaking(
    caplog: pytest.LogCaptureFixture,
    handler: Handler,
    call: Callable[[Any], Any],
    label: str,
) -> None:
    """Playback controls swallow SpotifyAPIError by design; their log line must be clean."""
    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(call(provider)) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    # contract: the described provider error (type + trusted detail), never raw exception text
    assert f"{label}: SpotifyAPIError detail=" in text


def test_swallowing_getters_return_none_without_leaking(caplog: pytest.LogCaptureFixture) -> None:
    """get_track / get_now_playing / get_playback_state hide API errors; nothing leaks."""
    provider = _provider(_api_error)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.get_track(QUERY)) is None
        assert asyncio.run(provider.get_now_playing()) is None
        assert asyncio.run(provider.get_playback_state()) is None

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "status=403" in text, "the underlying failure is still logged (not swallowed)"


def test_exchange_code_http_error_never_echoes_provider_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The OAuth error body echoes the auth code + client secret; only the enum is kept."""
    provider = _provider(_oauth_error, authed=False)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpotifyAuthError) as excinfo:
        asyncio.run(provider.exchange_code(AUTH_CODE))

    text = _everything_logged(caplog)
    _assert_clean(text)
    _assert_clean(str(excinfo.value))
    assert "status=400" in text
    assert "reason=invalid_grant" in text
    assert "reason=invalid_grant" in str(excinfo.value)
    _assert_unchained(excinfo.value)


@pytest.mark.parametrize("handler", [_connect_error, _runtime_error])
def test_exchange_code_unexpected_error_logs_type_only(
    caplog: pytest.LogCaptureFixture, handler: Handler
) -> None:
    """A non-HTTP failure during exchange logs the type, never its message."""
    provider = _provider(handler, authed=False)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpotifyAuthError) as excinfo:
        asyncio.run(provider.exchange_code(AUTH_CODE))

    _assert_clean(_everything_logged(caplog))
    _assert_clean(str(excinfo.value))
    _assert_unchained(excinfo.value)


def test_exchange_code_missing_token_field_is_reported_by_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 200 without access_token (KeyError) is reported by type; the body is never logged."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": ECHO})

    provider = _provider(handler, authed=False)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpotifyAuthError) as excinfo:
        asyncio.run(provider.exchange_code(AUTH_CODE))

    text = _everything_logged(caplog)
    _assert_clean(text)
    _assert_clean(str(excinfo.value))
    assert "KeyError" in text


def test_exchange_code_success_stores_tokens_and_logs_clean(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Happy path still works and its request line is clean."""
    provider = _provider(_token_ok, authed=False)
    with caplog.at_level(logging.DEBUG):
        data = asyncio.run(provider.exchange_code(AUTH_CODE))

    assert data["access_token"] == "new-access"
    assert provider.access_token == "new-access"
    _assert_clean(_everything_logged(caplog))


def test_refresh_http_error_never_echoes_provider_body(caplog: pytest.LogCaptureFixture) -> None:
    """The refresh token and client secret are echoed by the provider; neither is logged."""
    provider = _provider(_oauth_error)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpotifyAuthError) as excinfo:
        asyncio.run(provider.refresh_access_token())

    text = _everything_logged(caplog)
    _assert_clean(text)
    _assert_clean(str(excinfo.value))
    assert "reason=invalid_grant" in text
    _assert_unchained(excinfo.value)


@pytest.mark.parametrize("handler", [_connect_error, _runtime_error])
def test_refresh_unexpected_error_logs_type_only(
    caplog: pytest.LogCaptureFixture, handler: Handler
) -> None:
    """A transport failure while refreshing logs the type only."""
    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpotifyAuthError) as excinfo:
        asyncio.run(provider.refresh_access_token())

    _assert_clean(_everything_logged(caplog))
    _assert_clean(str(excinfo.value))
    _assert_unchained(excinfo.value)


def test_refresh_success_logs_clean(caplog: pytest.LogCaptureFixture) -> None:
    """Happy-path refresh rotates the token and logs nothing sensitive."""
    provider = _provider(_token_ok)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.refresh_access_token()) is True

    assert provider.access_token == "new-access"
    _assert_clean(_everything_logged(caplog))


def test_401_retry_whose_refresh_fails_wraps_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 401 triggers a refresh; when that fails the nested error keeps status/reason only."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            return _oauth_error(request)
        return httpx.Response(401, json={"error": {"status": 401, "message": ECHO}})

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpotifyAPIError) as excinfo:
        asyncio.run(provider.search(QUERY))

    text = _everything_logged(caplog)
    _assert_clean(text)
    _assert_clean(str(excinfo.value))
    assert "SpotifyAuthError" in str(excinfo.value)
    assert "reason=invalid_grant" in str(excinfo.value), "nested failure keeps its status/reason"
    _assert_unchained(excinfo.value)


def test_authenticate_with_bad_code_logs_clean(caplog: pytest.LogCaptureFixture) -> None:
    """authenticate() failing the code exchange returns False and logs a safe description."""
    provider = _provider(_oauth_error, authed=False)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.authenticate({"code": AUTH_CODE})) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Spotify authentication failed" in text
    assert "reason=invalid_grant" in text


def test_authenticate_invalid_credentials_and_direct_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other authenticate() branches: direct token succeeds, junk credentials fail."""
    provider = _provider(_search_ok, authed=False)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.authenticate({"access_token": ACCESS_TOKEN})) is True
        assert asyncio.run(provider.authenticate({"nonsense": QUERY})) is False

    _assert_clean(_everything_logged(caplog))
    assert "Invalid credentials format" in _everything_logged(caplog)


@pytest.mark.parametrize("handler", [_api_error, _connect_error, _runtime_error])
def test_health_check_failure_logs_clean(
    caplog: pytest.LogCaptureFixture, handler: Handler
) -> None:
    """health_check() used to log f"{e}" -- the URL; now type/status/reason only."""
    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.health_check()) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Spotify health check failed" in text


def test_health_check_success() -> None:
    """The healthy path returns True."""
    provider = _provider(lambda request: httpx.Response(200, json={"id": "me"}))
    assert asyncio.run(provider.health_check()) is True


def test_unsafe_reason_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """Only a plain identifier is accepted as the provider 'reason' -- free text is dropped."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": f"bad {QUERY} {ACCESS_TOKEN} reason"})

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(SpotifyAPIError) as excinfo:
        asyncio.run(provider.search("anything"))

    text = _everything_logged(caplog)
    _assert_clean(text)
    _assert_clean(str(excinfo.value))
    assert "reason=" not in text


def test_api_calls_without_failure_leave_no_sentinel(caplog: pytest.LogCaptureFixture) -> None:
    """Non-error calls (playlists, devices, playlist tracks, state) log nothing sensitive."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/devices"):
            return httpx.Response(200, json={"devices": [{"id": "d1"}]})
        if path.endswith("/me/playlists"):
            return httpx.Response(200, json={"items": [{"id": "p1"}]})
        if "/playlists/" in path:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"track": None},
                        {
                            "track": {
                                "id": "t1",
                                "name": "N",
                                "uri": "spotify:track:t1",
                                "artists": [{"name": "A"}],
                                "album": {"name": "B", "images": [{"url": "http://img"}]},
                            }
                        },
                    ]
                },
            )
        return httpx.Response(204)

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.get_devices()) == [{"id": "d1"}]
        assert asyncio.run(provider.get_playlists()) == [{"id": "p1"}]
        assert len(asyncio.run(provider.get_playlist_tracks(QUERY))) == 1
        assert asyncio.run(provider.get_playback_state()) is None
        assert asyncio.run(provider.get_now_playing()) is None
        assert asyncio.run(provider.play("t1")) is True
        assert asyncio.run(provider.is_authenticated()) is True

    _assert_clean(_everything_logged(caplog))


@pytest.mark.parametrize("start_level", [logging.NOTSET, logging.INFO])
def test_module_import_installs_the_httpx_guards_on_its_own(start_level: int) -> None:
    """Importing this provider module alone pins httpx to WARNING and installs the filter.

    The package ``__init__`` imports every provider, so a missing guard here would be masked
    by a sibling's. A private copy of the module is executed to prove this module's own call.
    """
    httpx_logger = logging.getLogger("httpx")
    saved_level, saved_filters = httpx_logger.level, list(httpx_logger.filters)
    try:
        httpx_logger.setLevel(start_level)
        httpx_logger.filters[:] = []
        source = Path(__file__).resolve().with_name("spotify_provider.py")
        spec = importlib.util.spec_from_file_location(
            "providers._spotify_provider_guard_probe", source
        )
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        assert httpx_logger.level == logging.WARNING
        assert any(type(f).__name__ == "StripHttpxQueryFilter" for f in httpx_logger.filters)
    finally:
        httpx_logger.setLevel(saved_level)
        httpx_logger.filters[:] = saved_filters


def test_authenticate_catch_all_logs_type_only_for_a_raw_exception(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """authenticate() catches anything; a raw (non-provider) exception is reported by type."""
    provider = _provider(_search_ok, authed=False)

    async def boom(code: str) -> None:
        raise RuntimeError(f"boom {ECHO} {code}")

    monkeypatch.setattr(provider, "exchange_code", boom)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.authenticate({"code": AUTH_CODE})) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Spotify authentication failed: RuntimeError" in text


def test_health_check_catch_all_logs_type_only_for_a_raw_exception(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """health_check() catches anything; a raw (non-provider) exception is reported by type."""
    provider = _provider(lambda request: httpx.Response(200, json={}))

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(f"boom {ECHO}")

    monkeypatch.setattr(provider, "_make_request", boom)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.health_check()) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Spotify health check failed: RuntimeError" in text


def test_provider_log_calls_pass_the_repo_exception_log_audit() -> None:
    """Static guard: the repo's exc_log_audit finds no unsafe exception log call.

    A finding means a log call interpolates a bound exception / ``exc_info`` again. The
    audit must also have examined real log calls -- zero examined is a failure, not a pass.
    """
    audit_path = (
        Path(__file__).resolve().parents[3]
        / "libs"
        / "flask_core"
        / "flask_core"
        / "exc_log_audit.py"
    )
    spec = importlib.util.spec_from_file_location("_exc_log_audit_under_test", audit_path)
    assert spec is not None and spec.loader is not None
    audit = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = audit  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(audit)

    here = Path(__file__).resolve().parent
    report = audit.audit_paths([here / "spotify_provider.py", here / "_log_safety.py"])
    assert report.files_examined == 2
    assert report.log_calls_examined >= 20
    assert report.findings == ()
