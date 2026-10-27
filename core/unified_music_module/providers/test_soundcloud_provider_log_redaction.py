"""PII / secret log-redaction tests for SoundCloudProvider.

# regression: SoundCloudProvider logged exception text on every failure path --
# ``logger.error(f"... {e.response.text}")`` (the provider's free-text error body can echo the
# request), ``f"{e}"`` where ``str(httpx.HTTPStatusError)`` is the full request URL, and it
# raised ``SoundCloudAPIError(f"... {e}")`` carrying the same text to callers. SoundCloud puts
# the OAuth access token (``oauth_token=``) AND the user's search text (``q=``) in the query
# string of every API call, and httpx logs the full URL of every request at INFO -- so the
# bearer token and search text reached the logs on every request, not only on errors.
# ``play``/``add_to_queue`` also logged the caller-supplied track id.

Each test binds unique sentinels (bearer token, user search text, auth code, refresh token,
client secret, caller-supplied ids), serves a hostile provider response that echoes them,
drives the REAL provider method through a real ``httpx.AsyncClient`` (MockTransport -- no
network), and asserts no sentinel appears in any captured log record (message, args, rendered
exception, httpx's own request line) nor in the raised error.
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

from providers.soundcloud_provider import (
    SoundCloudAPIError,
    SoundCloudAuthError,
    SoundCloudProvider,
)

ACCESS_TOKEN = "SENTINEL-oauth-token-6b2f90"
QUERY = "SENTINEL-search-text-0d7a35"
AUTH_CODE = "SENTINEL-auth-code-e41c88"
REFRESH_TOKEN = "SENTINEL-refresh-token-93ab70"
CLIENT_SECRET = "SENTINEL-client-secret-2c5d19"
TRACK_ID = "SENTINEL-track-id-b83e46"
USER_ID = "SENTINEL-user-id-71f0cc"
SENTINELS = (ACCESS_TOKEN, QUERY, AUTH_CODE, REFRESH_TOKEN, CLIENT_SECRET, TRACK_ID, USER_ID)

Handler = Callable[[httpx.Request], httpx.Response]

ECHO = (
    f"echo token={ACCESS_TOKEN} q={QUERY} code={AUTH_CODE} rt={REFRESH_TOKEN} "
    f"secret={CLIENT_SECRET} track={TRACK_ID} user={USER_ID}"
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the provider's required environment (client secret is a sentinel)."""
    monkeypatch.setenv("SOUNDCLOUD_CLIENT_ID", "client-id")
    monkeypatch.setenv("SOUNDCLOUD_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("SOUNDCLOUD_REDIRECT_URI", "http://localhost/callback")


def _provider(handler: Handler, *, authed: bool = True) -> Any:
    """Build a provider whose HTTP client is served by `handler` (no network)."""
    factory: Any = SoundCloudProvider  # the legacy provider's constructor is unannotated
    provider = factory()
    provider.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if authed:
        provider.access_token = ACCESS_TOKEN
        provider.refresh_token = REFRESH_TOKEN
        provider.user_id = USER_ID
    return provider


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """Render every captured record: message, raw args and formatted exception text."""
    formatter = logging.Formatter()
    return "\n".join(
        f"{formatter.format(r)}|{r.getMessage()}|{r.args!r}|{r.msg!r}" for r in caplog.records
    )


def _assert_clean(text: str) -> None:
    """No sentinel (token, user text, code, secret, caller-supplied id) may appear."""
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
    """Return a 403 API error whose free text echoes every sentinel (no reason enum)."""
    return httpx.Response(403, json={"errors": [{"error_message": ECHO}]})


def _oauth_error(request: httpx.Request) -> httpx.Response:
    """Return a 400 OAuth2 token-endpoint error whose description echoes every sentinel."""
    return httpx.Response(400, json={"error": "invalid_grant", "error_description": ECHO})


def _connect_error(request: httpx.Request) -> httpx.Response:
    """Raise a transport error whose message embeds the full URL and every sentinel."""
    raise httpx.ConnectError(f"cannot reach {request.url} {ECHO}", request=request)


def _runtime_error(request: httpx.Request) -> httpx.Response:
    """Raise a non-httpx failure whose message embeds every sentinel."""
    raise RuntimeError(f"boom {ECHO}")


def _html_error(request: httpx.Request) -> httpx.Response:
    """Return a non-JSON 502 whose body echoes every sentinel."""
    return httpx.Response(502, text=f"<html>bad gateway {ECHO}</html>")


def _track_json(track_id: int = 1) -> dict[str, Any]:
    """Return a minimal SoundCloud track object."""
    return {
        "id": track_id,
        "title": "Song",
        "user": {"id": 9, "username": "artist"},
        "stream_url": "https://api.soundcloud.com/tracks/1/stream",
        "duration": 1000,
    }


def _api_ok(request: httpx.Request) -> httpx.Response:
    """Serve plausible success bodies for the endpoints the provider uses."""
    path = request.url.path
    if path.endswith("/likes"):
        return httpx.Response(200, json=[{"track": _track_json()}, {"track": None}])
    if path.endswith("/playlists") and path.startswith("/users/"):
        return httpx.Response(200, json=[{"id": 5}])
    if path.startswith("/playlists/"):
        return httpx.Response(200, json={"tracks": [_track_json(), None]})
    if path == "/me":
        return httpx.Response(200, json={"id": 9})
    if path == "/tracks":
        return httpx.Response(200, json=[_track_json()])
    return httpx.Response(200, json=_track_json())


def _token_ok(request: httpx.Request) -> httpx.Response:
    """Serve a successful token response."""
    return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-rt"})


def test_original_bug_shape_is_present_in_raw_httpx_error() -> None:
    """Sanity: the token and search text really are in the raw error text this fix suppresses."""
    request = httpx.Request(
        "GET", f"https://api.soundcloud.com/tracks?q={QUERY}&oauth_token={ACCESS_TOKEN}"
    )
    try:
        httpx.Response(403, request=request).raise_for_status()
    except httpx.HTTPStatusError as exc:
        raw = str(exc)
    assert QUERY in raw
    assert ACCESS_TOKEN in raw


def test_httpx_request_lines_are_off_by_default(caplog: pytest.LogCaptureFixture) -> None:
    """Real requests through httpx emit no INFO request line: it would carry the OAuth token.

    ``oauth_token=`` and ``q=`` sit in every request's query; user ids sit in the path.
    """
    provider = _provider(_api_ok)
    with caplog.at_level(logging.DEBUG):
        assert len(asyncio.run(provider.search(QUERY))) == 1
        asyncio.run(provider.get_user_likes(USER_ID))

    assert logging.getLogger("httpx").level >= logging.WARNING
    assert [r for r in caplog.records if r.name == "httpx"] == []
    _assert_clean(_everything_logged(caplog))


def test_httpx_request_line_never_carries_the_token_even_if_info_is_reenabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defence in depth: with httpx INFO switched back on, the query is still stripped."""
    provider = _provider(_api_ok)
    with caplog.at_level(logging.INFO, logger="httpx"):
        assert len(asyncio.run(provider.search(QUERY))) == 1

    request_lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert request_lines, "httpx must have logged the request (else this test proves nothing)"
    assert any("api.soundcloud.com/tracks" in line for line in request_lines)
    _assert_clean(_everything_logged(caplog))


FAILURES: list[tuple[Handler, list[str]]] = [
    (_api_error, ["status=403"]),
    (_oauth_error, ["status=400", "reason=invalid_grant"]),
    (_connect_error, ["ConnectError"]),
    (_runtime_error, ["RuntimeError"]),
    (_html_error, ["status=502"]),
]


@pytest.mark.parametrize(("handler", "expected"), FAILURES)
def test_api_failure_logs_and_raises_only_safe_description(
    caplog: pytest.LogCaptureFixture, handler: Handler, expected: list[str]
) -> None:
    """Every _make_request failure shape: type/status/reason only, no chained httpx error."""
    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(SoundCloudAPIError) as excinfo:
        asyncio.run(provider._make_request("GET", "/tracks", params={"q": QUERY}))

    text = _everything_logged(caplog)
    _assert_clean(text)
    _assert_clean(str(excinfo.value))
    for fragment in expected:
        assert fragment in text, "the log line must stay useful"
        assert fragment in str(excinfo.value)
    # the chained httpx error's message is the full URL (token + query): must not be chained
    _assert_unchained(excinfo.value)


@pytest.mark.parametrize("handler", [_api_error, _connect_error, _runtime_error, _html_error])
@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.search(QUERY),
        lambda p: p.get_track(TRACK_ID),
        lambda p: p.get_stream_url(TRACK_ID),
        lambda p: p.get_track_stream_url(TRACK_ID),
        lambda p: p.get_user_profile(),
        lambda p: p.get_user_likes(USER_ID),
        lambda p: p.get_user_playlists(USER_ID),
        lambda p: p.get_playlist_tracks(TRACK_ID),
    ],
)
def test_swallowing_getters_hide_the_error_without_leaking(
    caplog: pytest.LogCaptureFixture, handler: Handler, call: Callable[[Any], Any]
) -> None:
    """Getters return an empty value on API errors by design; the underlying log is clean."""
    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(call(provider))

    assert result in (None, [], {})
    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "SoundCloud API error" in text or "Unexpected error during SoundCloud API" in text


@pytest.mark.parametrize("handler", [_api_error, _connect_error, _runtime_error])
def test_play_and_add_to_queue_never_log_the_caller_supplied_track_id(
    caplog: pytest.LogCaptureFixture, handler: Handler
) -> None:
    """play()/add_to_queue() used to log ``Track {track_id} not found`` -- raw user input."""
    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.play(TRACK_ID)) is False
        assert asyncio.run(provider.add_to_queue(TRACK_ID)) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Track not found" in text


def test_play_could_not_get_stream_url_never_logs_track_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the track exists but has no stream URL the error line omits the track id."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = _track_json()
        del body["stream_url"]
        return httpx.Response(200, json=body)

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.play(TRACK_ID)) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Could not get stream URL for track" in text


def test_play_unexpected_error_logs_type_only(caplog: pytest.LogCaptureFixture) -> None:
    """A malformed track body (KeyError in conversion) is logged by type, not content."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": ECHO})  # no "id" -> KeyError

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.play(TRACK_ID)) is False
        assert asyncio.run(provider.add_to_queue(TRACK_ID)) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Failed to start playback: KeyError" in text
    assert "Failed to add to queue: KeyError" in text


def test_play_and_queue_happy_path_logs_no_track_name_or_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful play/queue/skip log counts and indexes only (titles are uploader-controlled)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = _track_json()
        body["title"] = f"Song\nINJECTED {QUERY}"
        return httpx.Response(200, json=body)

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.play("1")) is True
        assert asyncio.run(provider.add_to_queue("2")) is True
        assert asyncio.run(provider.skip()) is True

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "INJECTED" not in text
    assert "Started playing track (queue_len=1)" in text
    assert "Added track to queue (queue_len=2)" in text
    assert "Skipped to track index 1" in text


def test_clear_queue_failure_logs_type_only(caplog: pytest.LogCaptureFixture) -> None:
    """clear_queue()'s catch-all logs the exception type, never its message."""

    class ExplodingQueue(list[Any]):
        def clear(self) -> None:
            raise RuntimeError(f"cannot clear {ECHO}")

    provider = _provider(_api_ok)
    provider.current_queue = ExplodingQueue()
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.clear_queue()) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Failed to clear queue: RuntimeError" in text


def test_stream_url_is_still_returned_but_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The stream URL legitimately carries the token for the caller; the logs must not."""
    provider = _provider(_api_ok)
    with caplog.at_level(logging.DEBUG):
        url = asyncio.run(provider.get_stream_url("1"))

    assert url is not None and f"oauth_token={ACCESS_TOKEN}" in url
    _assert_clean(_everything_logged(caplog))


def test_exchange_code_http_error_never_echoes_provider_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The OAuth error body echoes the auth code + client secret; only the enum is kept."""
    provider = _provider(_oauth_error, authed=False)
    with caplog.at_level(logging.DEBUG), pytest.raises(SoundCloudAuthError) as excinfo:
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
    with caplog.at_level(logging.DEBUG), pytest.raises(SoundCloudAuthError) as excinfo:
        asyncio.run(provider.exchange_code(AUTH_CODE))

    _assert_clean(_everything_logged(caplog))
    _assert_clean(str(excinfo.value))
    _assert_unchained(excinfo.value)


def test_exchange_code_success_stores_tokens_and_logs_clean(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Happy path still works (incl. expiry) and its request line is clean."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})

    provider = _provider(handler, authed=False)
    with caplog.at_level(logging.DEBUG):
        data = asyncio.run(provider.exchange_code(AUTH_CODE))

    assert data["access_token"] == "new-access"
    assert provider.token_expires_at is not None
    _assert_clean(_everything_logged(caplog))


def test_refresh_http_error_never_echoes_provider_body(caplog: pytest.LogCaptureFixture) -> None:
    """The refresh token and client secret are echoed by the provider; neither is logged."""
    provider = _provider(_oauth_error)
    with caplog.at_level(logging.DEBUG), pytest.raises(SoundCloudAuthError) as excinfo:
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
    with caplog.at_level(logging.DEBUG), pytest.raises(SoundCloudAuthError) as excinfo:
        asyncio.run(provider.refresh_access_token())

    _assert_clean(_everything_logged(caplog))
    _assert_clean(str(excinfo.value))
    _assert_unchained(excinfo.value)


def test_refresh_success_rotates_token_and_logs_clean(caplog: pytest.LogCaptureFixture) -> None:
    """Happy-path refresh rotates the token and logs nothing sensitive."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "new", "refresh_token": "rot", "expires_in": 60}
        )

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.refresh_access_token()) is True

    assert provider.access_token == "new"
    assert provider.refresh_token == "rot"
    _assert_clean(_everything_logged(caplog))


def test_401_retry_whose_refresh_fails_wraps_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 401 triggers a refresh; when that fails the nested error keeps status/reason only."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _oauth_error(request)
        return httpx.Response(401, json={"errors": [{"error_message": ECHO}]})

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(SoundCloudAPIError) as excinfo:
        asyncio.run(provider._make_request("GET", "/tracks", params={"q": QUERY}))

    text = _everything_logged(caplog)
    _assert_clean(text)
    _assert_clean(str(excinfo.value))
    assert "SoundCloudAuthError" in str(excinfo.value)
    assert "reason=invalid_grant" in str(excinfo.value), "nested failure keeps its status/reason"
    _assert_unchained(excinfo.value)


def test_401_retry_with_successful_refresh_recovers(caplog: pytest.LogCaptureFixture) -> None:
    """Happy retry path: 401 -> refresh -> second attempt succeeds, nothing sensitive logged."""
    calls = {"api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_ok(request)
        calls["api"] += 1
        if calls["api"] == 1:
            return httpx.Response(401, json={"errors": [{"error_message": ECHO}]})
        return httpx.Response(200, json=[_track_json()])

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        tracks = asyncio.run(provider.search(QUERY))

    assert len(tracks) == 1
    assert calls["api"] == 2
    _assert_clean(_everything_logged(caplog))


def test_authenticate_with_bad_code_logs_clean(caplog: pytest.LogCaptureFixture) -> None:
    """authenticate() failing the code exchange returns False and logs a safe description."""
    provider = _provider(_oauth_error, authed=False)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.authenticate({"code": AUTH_CODE})) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "SoundCloud authentication failed" in text
    assert "reason=invalid_grant" in text


def test_authenticate_code_then_profile_failure_still_succeeds_cleanly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The code exchange works but /me fails: authenticate() warns (statically) and succeeds."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_ok(request)
        return _api_error(request)

    provider = _provider(handler, authed=False)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.authenticate({"code": AUTH_CODE})) is True

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Could not fetch user info after authentication" in text


def test_authenticate_other_branches(caplog: pytest.LogCaptureFixture) -> None:
    """Direct token succeeds, junk credentials fail -- neither logs a sentinel."""
    provider = _provider(_api_ok, authed=False)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.authenticate({"access_token": ACCESS_TOKEN})) is True
        assert asyncio.run(provider.authenticate({"nonsense": QUERY})) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "Invalid credentials format" in text


@pytest.mark.parametrize("handler", [_api_error, _connect_error, _runtime_error])
def test_health_check_failure_logs_clean(
    caplog: pytest.LogCaptureFixture, handler: Handler
) -> None:
    """health_check() used to log f"{e}" -- the URL incl. ``oauth_token=``."""
    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.health_check()) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "SoundCloud health check failed" in text


def test_unsafe_reason_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """Only a plain identifier is accepted as the provider 'reason' -- free text is dropped."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": f"bad {QUERY} {ACCESS_TOKEN} reason"})

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(SoundCloudAPIError) as excinfo:
        asyncio.run(provider._make_request("GET", "/tracks"))

    text = _everything_logged(caplog)
    _assert_clean(text)
    _assert_clean(str(excinfo.value))
    assert "reason=" not in text


def test_non_error_paths_leave_no_sentinel(caplog: pytest.LogCaptureFixture) -> None:
    """Non-error calls (likes, playlists, profile, 204) log nothing sensitive."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        return _api_ok(request)

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG):
        assert len(asyncio.run(provider.get_user_likes(limit=5))) == 1
        assert asyncio.run(provider.get_user_playlists(USER_ID)) == [{"id": 5}]
        assert len(asyncio.run(provider.get_playlist_tracks("5"))) == 1
        assert asyncio.run(provider.get_user_profile()) == {"id": 9}
        assert asyncio.run(provider._make_request("DELETE", "/x")) is None
        assert asyncio.run(provider.health_check()) is True
        assert asyncio.run(provider.get_track("1")) is not None

    _assert_clean(_everything_logged(caplog))


def test_missing_user_id_is_logged_statically(caplog: pytest.LogCaptureFixture) -> None:
    """get_user_likes/playlists without a user id log a static line and return []."""
    provider = _provider(_api_ok)
    provider.user_id = None
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.get_user_likes()) == []
        assert asyncio.run(provider.get_user_playlists()) == []

    assert "No user ID available" in _everything_logged(caplog)


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
        source = Path(__file__).resolve().with_name("soundcloud_provider.py")
        spec = importlib.util.spec_from_file_location(
            "providers._soundcloud_provider_guard_probe", source
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
    provider = _provider(_api_ok, authed=False)

    async def boom(code: str) -> None:
        raise RuntimeError(f"boom {ECHO} {code}")

    monkeypatch.setattr(provider, "exchange_code", boom)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.authenticate({"code": AUTH_CODE})) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "SoundCloud authentication failed: RuntimeError" in text


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
    assert "SoundCloud health check failed: RuntimeError" in text


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
    report = audit.audit_paths([here / "soundcloud_provider.py", here / "_log_safety.py"])
    assert report.files_examined == 2
    assert report.log_calls_examined >= 25
    assert report.findings == ()
