"""WaddleAI completion ACTION handler -- routes text to the hub-api AI service.

Ported from `hub_api/services/ai_routing/` (v2). This handler wraps the
existing hub-api `/api/v1/community/<community_id>/ai/completions` endpoint
via HTTP, sending a prompt from the triggering event's text and returning
the AI-generated response text as a reply in the channel (reply-in-place).

The AI service itself enforces feature flags (PostHog `waddles.ai.routing`),
Enterprise tier gating, and the deploy-time `WADDLES_AI_ENABLED` kill-switch
-- this handler is the thin client interface, never the policy enforcement
layer. An unreachable hub-api, a disabled AI service, or an unauthenticated
request is an external failure (`RetryableTransportError` for network/5xx or
`NonRetryableTransportError` for auth/4xx), not a handler-logic error.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from flask_core import StageEnvelope, get_bundle_context
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.url_guard import SSRFError, guarded_request


async def waddleai_completion(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Send event.payload["text"] as a prompt to the hub-api AI service.

    The incoming `text` is treated as the AI prompt; the community ID comes
    from the bundle context (via get_bundle_context()). Calls the hub-api
    `/api/v1/community/<community_id>/ai/completions` endpoint with the prompt
    and returns the AI-generated text as a reply-in-place to the originating channel.

    Raises `NonRetryableTransportError` for a config/auth failure (no
    community_id, unresolvable hub-api URL, 401/403, any other 4xx) and
    `RetryableTransportError` for a 5xx/network error. The AI service itself
    enforces feature flags, Enterprise tier gating, and the deploy-time
    `WADDLES_AI_ENABLED` kill-switch -- a 503 response from the AI endpoint
    is retryable (the kill-switch is temporary).
    """
    # Extract prompt from event payload
    text = envelope.event.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise NonRetryableTransportError(
            "waddleai bundle: event.payload missing required 'text' string"
        )

    # Get tenant/community from the frozen API (per APP_BUNDLE_AUTHORING.md §5).
    # Never read from payload -- context comes from the envelope's isolation boundary.
    ctx = get_bundle_context()
    if not ctx.community:
        raise NonRetryableTransportError(
            "waddleai bundle: context.community is None (tenant-wide activation unsupported)"
        )

    # Parse community as an integer ID
    try:
        community_id = int(ctx.community)
    except (ValueError, TypeError) as exc:
        raise NonRetryableTransportError(
            f"waddleai bundle: community identifier {ctx.community!r} is not a valid integer"
        ) from exc

    # Build the hub-api endpoint URL
    # The config may carry `hub_api_base` (default from svc-action's HUB_API_URL),
    # but since the runner provides this URL indirectly through distribution
    # polling and we need to call hub-api, we resolve it from a required
    # config key or a fallback.
    hub_api_base = config.get("hub_api_base")
    if not isinstance(hub_api_base, str) or not hub_api_base:
        raise NonRetryableTransportError(
            "waddleai bundle config missing required 'hub_api_base'"
        )

    url = f"{hub_api_base}/api/v1/community/{community_id}/ai/completions"
    headers = {"Content-Type": "application/json"}
    body = {
        "prompt": text.strip(),
        "max_tokens": config.get("max_tokens", 512),
        "temperature": config.get("temperature", 0.7),
        "requested_tier": config.get("requested_tier"),
        "model_hint": config.get("model_hint"),
        "byok_provider": config.get("byok_provider"),
        "invocation": config.get("invocation", "interactive"),
    }

    # Filter out None values to match the CompletionRequestDTO
    body = {k: v for k, v in body.items() if v is not None}

    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(
            f"waddleai API URL rejected by SSRF guard: {exc}"
        ) from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"waddleai API request failed: {exc}") from exc

    # Handle 503 (AI disabled or flag off) as retryable -- may become available
    if response.status_code == 503:
        raise RetryableTransportError(
            "waddleai API returned 503 (AI service may be disabled or unavailable)",
            http_status=503,
        )
    # Handle auth failures
    if response.status_code in (401, 403):
        raise NonRetryableTransportError(
            f"waddleai API rejected auth: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    # Handle other 4xx
    if 400 <= response.status_code < 500:
        try:
            error_detail = response.json()
        except Exception:  # noqa: BLE001, S110 -- best-effort detail only
            error_detail = response.text[:200]
        raise NonRetryableTransportError(
            f"waddleai API returned client error: HTTP {response.status_code} {error_detail}",
            http_status=response.status_code,
        )
    # Handle 5xx
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"waddleai API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    # Parse the response
    try:
        data = response.json()
    except Exception as exc:
        raise NonRetryableTransportError(
            f"waddleai API response was not valid JSON: {exc}"
        ) from exc

    # Extract the AI-generated text
    ai_text = data.get("text")
    if not isinstance(ai_text, str):
        raise NonRetryableTransportError(
            "waddleai API response missing 'text' field or not a string"
        )

    return TransportResult(
        transport="bundle",
        detail=(
            f"waddleai completion sent, community={community_id} "
            f"tokens={data.get('output_tokens', 0)}"
        ),
        http_status=response.status_code,
    )
