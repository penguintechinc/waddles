"""PII / secret log-redaction tests for YouTubeProvider.

# regression: YouTubeProvider.search() logged the user's raw search text
# (``logger.debug(f"Searching YouTube for: {query}")``, plus the "No results" and
# "Found N videos" info lines), and its error paths interpolated exception text:
# ``str(httpx.HTTPStatusError)`` is the full request URL -- search text AND the
# ``key=`` API key -- and Google's free-text error message may echo parameters.

Each test uses a unique `QUERY` sentinel as the user's search text and a unique
`CANARY_B`, then asserts neither appears in any log record (message, args or
rendered exception) nor in the raised `YouTubeAPIError`.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from providers.youtube_provider import (
    YouTubeAPIError,
    YouTubeProvider,
)

QUERY = "SENTINEL-search-text-4b1d9e"
CANARY_B = "SENTINEL-canary-b-9d2e"

Handler = Callable[[httpx.Request], httpx.Response]


def _provider(handler: Handler) -> Any:
    """Build a provider whose HTTP client is served by `handler` (no network)."""
    factory: Any = YouTubeProvider  # the legacy provider's constructor is unannotated
    provider = factory()
    provider.api_key = CANARY_B
    provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """Render every captured record: message, raw args and formatted exception text."""
    formatter = logging.Formatter()
    return "\n".join(
        f"{formatter.format(r)}|{r.getMessage()}|{r.args!r}|{r.msg!r}" for r in caplog.records
    )


def _status_error_handler(request: httpx.Request) -> httpx.Response:
    """Return a 403 whose free-text message echoes the query and the key."""
    return httpx.Response(
        403,
        json={
            "error": {
                "message": f"Invalid value for q: {QUERY} (key={CANARY_B})",
                "errors": [{"reason": "quotaExceeded", "message": QUERY}],
            }
        },
    )


def _ok_empty_handler(request: httpx.Request) -> httpx.Response:
    """Return a successful search with no items."""
    return httpx.Response(200, json={"items": []})


def _connect_error_handler(request: httpx.Request) -> httpx.Response:
    """Raise a transport error whose message embeds the query and the key."""
    raise httpx.ConnectError(f"cannot reach {request.url} {QUERY} {CANARY_B}", request=request)


def _search_ok_handler(request: httpx.Request) -> httpx.Response:
    """Serve a one-video search then video-details sequence."""
    if request.url.path.endswith("/search"):
        return httpx.Response(200, json={"items": [{"id": {"videoId": "dQw4w9WgXcQ"}}]})
    return httpx.Response(
        200,
        json={
            "items": [
                {
                    "id": "dQw4w9WgXcQ",
                    "snippet": {
                        "title": "Song",
                        "channelTitle": "Chan",
                        "thumbnails": {"default": {"url": "https://i.ytimg.com/x.jpg"}},
                    },
                    "contentDetails": {"duration": "PT3M"},
                }
            ]
        },
    )


def _assert_clean(text: str) -> None:
    """Neither the user's search text nor the API key may appear."""
    assert QUERY not in text
    assert CANARY_B not in text


def test_search_success_never_logs_query(caplog: pytest.LogCaptureFixture) -> None:
    """DEBUG/INFO lines around a successful search omit the raw query (length only)."""
    provider = _provider(_search_ok_handler)
    with caplog.at_level(logging.DEBUG):
        tracks = asyncio.run(provider.search(QUERY, limit=1))

    assert len(tracks) == 1
    text = _everything_logged(caplog)
    _assert_clean(text)
    assert f"query_len={len(QUERY)}" in text, "log line must stay useful (length only)"


def test_search_no_results_never_logs_query(caplog: pytest.LogCaptureFixture) -> None:
    """The 'no results' INFO line omits the raw query."""
    provider = _provider(_ok_empty_handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.search(QUERY)) == []

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "No YouTube results" in text


def test_search_http_error_logs_status_and_reason_only(caplog: pytest.LogCaptureFixture) -> None:
    """An API error logs status + machine reason; the raised error carries no request data."""
    provider = _provider(_status_error_handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(YouTubeAPIError) as excinfo:
        asyncio.run(provider.search(QUERY))

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "status=403" in text
    assert "reason=quotaExceeded" in text
    _assert_clean(str(excinfo.value))
    assert "403" in str(excinfo.value)
    assert "quotaExceeded" in str(excinfo.value)
    # the chained httpx error's message is the full URL (query + key): must not be chained
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_search_transport_error_logs_type_only(caplog: pytest.LogCaptureFixture) -> None:
    """A transport failure logs the exception type, never its message."""
    provider = _provider(_connect_error_handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(YouTubeAPIError) as excinfo:
        asyncio.run(provider.search(QUERY))

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "ConnectError" in text
    _assert_clean(str(excinfo.value))


@pytest.mark.parametrize("handler", [_status_error_handler, _connect_error_handler])
def test_get_track_errors_never_log_request_data(
    caplog: pytest.LogCaptureFixture, handler: Handler
) -> None:
    """get_track() error paths get the same treatment (video ID + key are in the URL)."""
    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(YouTubeAPIError) as excinfo:
        asyncio.run(provider.get_track("dQw4w9WgXcQ"))

    _assert_clean(_everything_logged(caplog))
    _assert_clean(str(excinfo.value))
    assert excinfo.value.__cause__ is None


def test_get_track_unparseable_url_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A YouTube URL we cannot parse is user input: the warning must not echo it."""
    provider = _provider(_ok_empty_handler)
    url = f"https://www.youtube.com/playlist?list={QUERY}"
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.get_track(url)) is None

    text = _everything_logged(caplog)
    assert QUERY not in text
    assert "Could not extract video ID" in text


def test_authenticate_failure_never_logs_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """authenticate() logs a sanitized description of the failed probe search."""
    provider = _provider(_status_error_handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.authenticate({"api_key": CANARY_B})) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "YouTube authentication failed" in text
    assert "status=403" in text


def test_health_check_failure_never_logs_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """health_check() used to log str(HTTPStatusError) -- the URL including ``key=``."""
    provider = _provider(_status_error_handler)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider.health_check()) is False

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "YouTube health check failed" in text
    assert "status=403" in text


def test_unsafe_reason_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """Only a plain identifier is accepted as the API 'reason' -- free text is dropped."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"errors": [{"reason": f"bad {QUERY} reason"}]}})

    provider = _provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(YouTubeAPIError) as excinfo:
        asyncio.run(provider.search("anything"))

    _assert_clean(_everything_logged(caplog))
    _assert_clean(str(excinfo.value))
    assert "reason=" not in _everything_logged(caplog)


def test_original_bug_shape_is_detected_by_helpers() -> None:
    """Sanity: the sentinel really is present in the raw httpx error this fix suppresses."""
    request = httpx.Request(
        "GET", f"https://www.googleapis.com/youtube/v3/search?q={QUERY}&key={CANARY_B}"
    )
    response = httpx.Response(403, request=request)
    raw: Any = None
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raw = str(exc)
    assert QUERY in raw
    assert CANARY_B in raw


def test_httpx_request_log_line_has_query_stripped(caplog: pytest.LogCaptureFixture) -> None:
    """The httpx INFO line carries the full URL; the provider's filter strips the query."""
    httpx_logger = logging.getLogger("httpx")
    with caplog.at_level(logging.INFO, logger="httpx"):
        httpx_logger.info(
            'HTTP Request: %s %s "%s %d %s"',
            "GET",
            httpx.URL(f"https://www.googleapis.com/youtube/v3/search?q={QUERY}&key={CANARY_B}"),
            "HTTP/1.1",
            200,
            "OK",
        )
        httpx_logger.info("HTTP Request: GET %s", f"https://x.test/s?q={QUERY}&key={CANARY_B}")

    text = _everything_logged(caplog)
    _assert_clean(text)
    assert "https://www.googleapis.com/youtube/v3/search" in text
    assert "HTTP/1.1" in text, "non-URL args must be left untouched"
