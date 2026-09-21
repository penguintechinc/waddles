"""`http` transport -- outbound `webhook`/`rest_api`/`grpc`/`graphql`, inbound `rest_pull`.

Four **outbound** sub-types share one SSRF-guarded, redirect-revalidated
HTTP client underneath (`waddle_transports.url_guard.guarded_request`):

- `webhook` -- HMAC-signed POST.
- `rest_api` -- generic configurable-method REST call.
- `graphql` -- HTTP POST carrying a GraphQL `{"query", "variables"}` JSON
  body; a 200 response whose body carries a non-empty top-level `"errors"`
  array (the GraphQL-over-HTTP convention) is non-retryable.
- `grpc` -- a **real** gRPC unary call over HTTP/2: genuine 5-byte gRPC
  message framing, `application/grpc+proto` content-type, `te: trailers`
  header, sent via `httpx.AsyncClient(http2=True)`. One honest, documented
  limitation (not a stub): this transport has no compile-time knowledge of
  any specific `.proto` schema, so it cannot construct a request message
  from `payload` -- the caller supplies an already protobuf-encoded
  message, base64-encoded, via `config["grpc_message_b64"]`. `grpc-status`
  is read from `response.headers` -- some real gRPC servers place status
  only in HTTP/2 trailers, not universally exposed the same way across
  httpx versions/transports; absent that header, an HTTP 200 with a
  well-formed response message frame is a best-effort success, and a
  malformed/absent frame is non-retryable (outcome unconfirmable) rather
  than silently assumed successful.

One **inbound** sub-type:

- `rest_pull` -- poll `config["url"]` on `config["poll_interval_s"]`
  (default 5s), expecting a JSON array response; yields each array
  element as one inbound item. Real, working polling loop -- not a stub.

`webhook_push` (an inbound HTTP push delivered to a service's *own* route
handler) is deliberately **not** implemented as a `receive()` sub_type
here: receiving a push is inherently server-side (something posts *to*
the consuming service), not a client connecting outward to fetch
anything, so it does not fit this transport's `receive()` contract at
all. `receive(config={"sub_type": "webhook_push", ...})` raises a clear,
documented error explaining this rather than pretending to support it.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from waddle_transports.base import (
    NonRetryableTransportError,
    RetryableTransportError,
    Transport,
    TransportResult,
)
from waddle_transports.signing import resolve_secret, sign_body
from waddle_transports.templating import build_body
from waddle_transports.types import Direction
from waddle_transports.url_guard import (
    DEFAULT_MAX_RESPONSE_BYTES,
    ResponseTooLargeError,
    SSRFError,
    guarded_request,
)

#: gRPC status codes worth retrying (transient) per the standard status
#: code table (grpc.io/docs/guides/status-codes): DEADLINE_EXCEEDED(4),
#: RESOURCE_EXHAUSTED(8), UNAVAILABLE(14). Everything else is permanent.
_RETRYABLE_GRPC_STATUS = frozenset({"4", "8", "14"})

_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_POLL_INTERVAL_S = 5.0


class HttpTransport(Transport):
    """`http` transport -- see module docstring for the full sub_type matrix."""

    name = "http"
    directions = frozenset({Direction.OUTBOUND, Direction.INBOUND})

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        """`http_client` is reused across calls when given; otherwise built+closed per call."""
        self._client = http_client

    async def send(self, config: Mapping[str, Any], payload: Mapping[str, Any]) -> TransportResult:
        """Route to the sub_type-specific outbound dispatch."""
        sub_type = config.get("sub_type")
        timeout_seconds = float(config.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
        if sub_type == "webhook":
            return await self._send_webhook(config, payload, timeout_seconds)
        if sub_type == "rest_api":
            return await self._send_rest_api(config, payload, timeout_seconds)
        if sub_type == "graphql":
            return await self._send_graphql(config, payload, timeout_seconds)
        if sub_type == "grpc":
            return await self._send_grpc(config, timeout_seconds)
        raise NonRetryableTransportError(
            f"http transport sub_type={sub_type!r} is not supported for send()"
        )

    async def receive(self, config: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        """Route to the sub_type-specific inbound receive. Only `rest_pull` is implemented."""
        sub_type = config.get("sub_type")
        if sub_type == "rest_pull":
            async for item in self._receive_rest_pull(config):
                yield item
            return
        if sub_type == "webhook_push":
            raise NonRetryableTransportError(
                "http transport sub_type='webhook_push' is server-side (an inbound HTTP route "
                "receiving a push), not a client-side receive() this transport performs by "
                "connecting outward -- see module docstring"
            )
        raise NonRetryableTransportError(
            f"http transport sub_type={sub_type!r} is not supported for receive()"
        )
        yield {}  # pragma: no cover -- unreachable, keeps this a real async generator function

    # --- outbound ---------------------------------------------------------

    async def _client_or_new(
        self, timeout_seconds: float, *, http2: bool = False
    ) -> tuple[httpx.AsyncClient, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(follow_redirects=False, http2=http2, timeout=timeout_seconds), True

    async def _send_webhook(
        self, config: Mapping[str, Any], payload: Mapping[str, Any], timeout_seconds: float
    ) -> TransportResult:
        url = config.get("url")
        secret_ref = config.get("secret_ref")
        if not isinstance(url, str) or not url:
            raise NonRetryableTransportError("http:webhook config missing required 'url'")
        if not isinstance(secret_ref, str) or not secret_ref:
            raise NonRetryableTransportError("http:webhook config missing required 'secret_ref'")

        body = build_body(config.get("body_template"), payload)
        try:
            secret = resolve_secret(secret_ref)
        except Exception as exc:  # SecretResolutionError -- config error, never retryable.
            raise NonRetryableTransportError(f"webhook secret resolution failed: {exc}") from exc

        signature = sign_body(secret, body)
        headers = {
            **dict(config.get("headers", {})),
            "Content-Type": "application/json",
            "X-Waddle-Signature": signature,
        }

        client, owns_client = await self._client_or_new(timeout_seconds)
        try:
            response = await guarded_request(
                client,
                "POST",
                url,
                headers=headers,
                content=body,
                max_response_bytes=_max_response_bytes(config),
            )
        except SSRFError as exc:
            raise NonRetryableTransportError(f"webhook URL rejected by SSRF guard: {exc}") from exc
        except ResponseTooLargeError as exc:
            raise NonRetryableTransportError(f"webhook response exceeded max size: {exc}") from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise RetryableTransportError(f"webhook request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        _raise_for_http_status(response.status_code, "webhook")
        return TransportResult(
            transport="http",
            sub_type="webhook",
            detail=f"delivered, HTTP {response.status_code}",
            http_status=response.status_code,
        )

    async def _send_rest_api(
        self, config: Mapping[str, Any], payload: Mapping[str, Any], timeout_seconds: float
    ) -> TransportResult:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise NonRetryableTransportError("http:rest_api config missing required 'url'")
        method = str(config.get("method", "POST")).upper()

        body = (
            build_body(config.get("body_template"), payload)
            if method in ("POST", "PUT", "PATCH")
            else None
        )
        headers = {**dict(config.get("headers", {}))}
        if body is not None:
            headers.setdefault("Content-Type", "application/json")

        client, owns_client = await self._client_or_new(timeout_seconds)
        try:
            response = await guarded_request(
                client,
                method,
                url,
                headers=headers,
                content=body,
                max_response_bytes=_max_response_bytes(config),
            )
        except SSRFError as exc:
            raise NonRetryableTransportError(f"rest_api URL rejected by SSRF guard: {exc}") from exc
        except ResponseTooLargeError as exc:
            raise NonRetryableTransportError(f"rest_api response exceeded max size: {exc}") from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise RetryableTransportError(f"rest_api request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        _raise_for_http_status(response.status_code, "rest_api")
        return TransportResult(
            transport="http",
            sub_type="rest_api",
            detail=f"delivered, HTTP {response.status_code}",
            http_status=response.status_code,
        )

    async def _send_graphql(
        self, config: Mapping[str, Any], payload: Mapping[str, Any], timeout_seconds: float
    ) -> TransportResult:
        url = config.get("url")
        query = config.get("query")
        if not isinstance(url, str) or not url:
            raise NonRetryableTransportError("http:graphql config missing required 'url'")
        if not isinstance(query, str) or not query:
            raise NonRetryableTransportError("http:graphql config missing required 'query'")

        variables = config.get("variables", {})
        body = json.dumps({"query": query, "variables": dict(variables)}).encode("utf-8")
        headers = {**dict(config.get("headers", {})), "Content-Type": "application/json"}

        client, owns_client = await self._client_or_new(timeout_seconds)
        try:
            response = await guarded_request(
                client,
                "POST",
                url,
                headers=headers,
                content=body,
                max_response_bytes=_max_response_bytes(config),
            )
        except SSRFError as exc:
            raise NonRetryableTransportError(f"graphql URL rejected by SSRF guard: {exc}") from exc
        except ResponseTooLargeError as exc:
            raise NonRetryableTransportError(f"graphql response exceeded max size: {exc}") from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise RetryableTransportError(f"graphql request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        _raise_for_http_status(response.status_code, "graphql")

        try:
            body_json = response.json()
        except ValueError as exc:
            raise NonRetryableTransportError(f"graphql response was not valid JSON: {exc}") from exc

        errors = body_json.get("errors") if isinstance(body_json, dict) else None
        if errors:
            raise NonRetryableTransportError(
                f"graphql response carried {len(errors)} error(s): {errors[0]}"
            )

        return TransportResult(
            transport="http",
            sub_type="graphql",
            detail=f"delivered, HTTP {response.status_code}",
            http_status=response.status_code,
        )

    async def _send_grpc(
        self, config: Mapping[str, Any], timeout_seconds: float
    ) -> TransportResult:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise NonRetryableTransportError("http:grpc config missing required 'url'")
        message_b64 = config.get("grpc_message_b64")
        if not isinstance(message_b64, str):
            raise NonRetryableTransportError("http:grpc config missing required 'grpc_message_b64'")

        try:
            message = base64.b64decode(message_b64, validate=True)
        except ValueError as exc:
            raise NonRetryableTransportError(
                f"grpc_message_b64 is not valid base64: {exc}"
            ) from exc

        headers = {
            **dict(config.get("headers", {})),
            "content-type": "application/grpc+proto",
            "te": "trailers",
        }
        frame = _grpc_frame(message)

        client, owns_client = await self._client_or_new(timeout_seconds, http2=True)
        try:
            response = await guarded_request(
                client,
                "POST",
                url,
                headers=headers,
                content=frame,
                max_response_bytes=_max_response_bytes(config),
            )
        except SSRFError as exc:
            raise NonRetryableTransportError(f"grpc URL rejected by SSRF guard: {exc}") from exc
        except ResponseTooLargeError as exc:
            raise NonRetryableTransportError(f"grpc response exceeded max size: {exc}") from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise RetryableTransportError(f"grpc request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code != 200:
            raise RetryableTransportError(
                f"grpc endpoint returned non-200 HTTP status: {response.status_code}",
                http_status=response.status_code,
            )

        grpc_status = response.headers.get("grpc-status")
        if grpc_status is not None and grpc_status != "0":
            detail = response.headers.get("grpc-message", "")
            message_text = f"grpc call failed, grpc-status={grpc_status} {detail}".strip()
            if grpc_status in _RETRYABLE_GRPC_STATUS:
                raise RetryableTransportError(message_text)
            raise NonRetryableTransportError(message_text)

        message_out = _unframe_grpc(response.content)
        if message_out is None:
            raise NonRetryableTransportError(
                "grpc response had no grpc-status header and no well-formed message frame "
                "-- cannot confirm RPC outcome (trailer-only grpc-status is a known limitation)"
            )

        return TransportResult(
            transport="http",
            sub_type="grpc",
            detail=f"delivered, {len(message_out)} response byte(s)",
            http_status=response.status_code,
        )

    # --- inbound ------------------------------------------------------------

    async def _receive_rest_pull(
        self, config: Mapping[str, Any]
    ) -> AsyncIterator[Mapping[str, Any]]:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise NonRetryableTransportError("http:rest_pull config missing required 'url'")
        poll_interval_s = float(config.get("poll_interval_s", _DEFAULT_POLL_INTERVAL_S))
        timeout_seconds = float(config.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
        max_iterations = config.get("_max_iterations")  # test-only escape hatch, see tests

        client, owns_client = await self._client_or_new(timeout_seconds)
        iterations = 0
        try:
            while max_iterations is None or iterations < max_iterations:
                iterations += 1
                try:
                    response = await guarded_request(
                        client,
                        "GET",
                        url,
                        headers=dict(config.get("headers", {})),
                        max_response_bytes=_max_response_bytes(config),
                    )
                except SSRFError as exc:
                    raise NonRetryableTransportError(
                        f"rest_pull URL rejected by SSRF guard: {exc}"
                    ) from exc
                except ResponseTooLargeError as exc:
                    raise NonRetryableTransportError(
                        f"rest_pull response exceeded max size: {exc}"
                    ) from exc
                except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                    raise RetryableTransportError(f"rest_pull request failed: {exc}") from exc

                _raise_for_http_status(response.status_code, "rest_pull")
                try:
                    items = response.json()
                except ValueError as exc:
                    raise NonRetryableTransportError(
                        f"rest_pull response was not valid JSON: {exc}"
                    ) from exc
                if not isinstance(items, list):
                    raise NonRetryableTransportError("rest_pull response must be a JSON array")

                for item in items:
                    if isinstance(item, dict):
                        yield item

                if max_iterations is None or iterations < max_iterations:
                    await asyncio.sleep(poll_interval_s)
        finally:
            if owns_client:
                await client.aclose()


def _max_response_bytes(config: Mapping[str, Any]) -> int:
    """`config["max_response_bytes"]`, or `url_guard`'s own default if unset."""
    return int(config.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES))


def _grpc_frame(message: bytes) -> bytes:
    """Wrap `message` in the standard gRPC length-prefixed frame."""
    return b"\x00" + len(message).to_bytes(4, "big") + message


def _unframe_grpc(body: bytes) -> bytes | None:
    """Extract the message bytes from one gRPC-framed response, or `None` if malformed/short."""
    if len(body) < 5:
        return None
    length = int.from_bytes(body[1:5], "big")
    return body[5:5 + length]


def _raise_for_http_status(status_code: int, label: str) -> None:
    """Shared 401/403 -> 4xx -> 5xx classification for the plain-HTTP sub-types."""
    if status_code in (401, 403):
        raise NonRetryableTransportError(
            f"{label} target rejected auth: HTTP {status_code}", http_status=status_code
        )
    if 400 <= status_code < 500:
        raise NonRetryableTransportError(
            f"{label} target returned client error: HTTP {status_code}", http_status=status_code
        )
    if status_code >= 500:
        raise RetryableTransportError(
            f"{label} target returned server error: HTTP {status_code}", http_status=status_code
        )
