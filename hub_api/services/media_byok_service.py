"""Media (Gemini) BYOK key management, effective-tier resolution, and the internal resolver core.

Harness media generation (`docs/superpowers/specs/2026-11-06-harness-media-generation.md`) is
Professional-or-higher AND BYOK-only: there is no platform-funded media key. A community's Gemini
key lives in the existing `ai_byok_keys` store (AES-256-GCM, `AI_BYOK_ENCRYPTION_KEY`, provider
`gemini`) -- the AI router/config paths never select it (`config_service._VALID_PROVIDERS`).

Invariants this module owns:

* the plaintext key leaves this module ONLY through `resolve_media_key` (the internal resolver);
  every other accessor returns `key_last4` metadata;
* tier is `max(tenant_tier, community_tier)`, resolved here from trusted server state (the license
  gate + `communities.license_tier`), never from a caller-supplied value, and fails closed to
  `free` on anything unresolvable;
* every lookup is tenant-bound: a community that is not in the stated tenant never resolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import httpx
from flask_core.feature_flags import get_tier
from flask_core.tier_catalog import TIER_FREE, normalize_tier, tier_level

from services.ai_routing import config_service
from services.ai_routing.config_service import ByokKeyInfo
from services.ai_routing.errors import invalid_byok_key, provider_error
from services.community_authz import community_belongs_to_tenant
from services.errors import ApiError, bad_request

#: The only media provider in v3.0 (single BYOK Gemini key; OpenAI/others deferred to v3.1.x).
MEDIA_PROVIDER_GEMINI: Final = "gemini"
SUPPORTED_MEDIA_PROVIDERS: Final = frozenset({MEDIA_PROVIDER_GEMINI})

#: Pinned provider host -- the key is only ever sent here (never a caller-supplied URL).
GEMINI_MODELS_URL: Final = "https://generativelanguage.googleapis.com/v1beta/models"

#: Minimum effective tier for any media generation (spec §1).
MIN_MEDIA_TIER: Final = "professional"
#: Upper bound on a pasted API key; real Gemini keys are ~40 chars.
MAX_KEY_CHARS: Final = 512

STATUS_OK: Final = "ok"
STATUS_NOT_CONFIGURED: Final = "not_configured"
STATUS_TIER_REQUIRED: Final = "tier_required"


@dataclass(slots=True, frozen=True)
class MediaResolution:
    """Outcome of one resolver call. `api_key` is set only when `status == "ok"`."""

    status: str
    tier: str
    api_key: str | None = None


class CommunityScopeError(Exception):
    """The community does not exist inside the stated tenant (or the tenant is unknown)."""


def tier_meets_media_floor(tier: str) -> bool:
    """True when `tier` is Professional or higher; an unknown tier is below the floor."""
    return tier_level(tier) >= tier_level(MIN_MEDIA_TIER)


async def _tenant_slug(async_dal: Any, dal: Any, *, tenant_id: int) -> str:
    """Slug of `tenant_id` (the license gate is keyed by slug); raises if the tenant is unknown."""
    rows = await async_dal.select_async(dal(dal.tenants.id == tenant_id))
    if not rows:
        raise CommunityScopeError("unknown tenant")
    return str(rows.first().slug)


async def effective_tier(async_dal: Any, dal: Any, *, tenant_id: int, community_id: int) -> str:
    """`max(tenant_tier, community_tier)` for a community INSIDE `tenant_id`; fails closed to free.

    The tenant tier comes from the license gate (`flask_core.feature_flags.get_tier`, which itself
    fails closed to `free`); the community tier is the tenant-admin allocation on
    `communities.license_tier`. An unknown tier string ranks below `free` and never lifts the
    result. Raises `CommunityScopeError` when the community is not in the tenant -- a tier is
    never reported for a pair that does not exist.
    """
    if not await community_belongs_to_tenant(
        async_dal, dal, community_id=community_id, tenant_id=tenant_id
    ):
        raise CommunityScopeError("community not in tenant")
    slug = await _tenant_slug(async_dal, dal, tenant_id=tenant_id)
    tenant_tier = normalize_tier(await get_tier(tenant=slug))
    rows = await async_dal.select_async(dal(dal.communities.id == community_id))
    community_tier = normalize_tier(rows.first().license_tier or "")
    best = max((tenant_tier, community_tier), key=tier_level)
    return best if tier_level(best) > 0 else TIER_FREE


async def _validate_gemini_key(api_key: str) -> None:
    """Cheap authenticated `models` call to the pinned Gemini host; never persist a dead key.

    The key travels in the `x-goog-api-key` header only (never the URL), and no part of it or of
    the provider's response body is put in an error message.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(
                GEMINI_MODELS_URL,
                params={"pageSize": "1"},
                headers={"x-goog-api-key": api_key},
            )
    except httpx.HTTPError as exc:
        raise provider_error(
            f"gemini key validation request failed ({type(exc).__name__})"
        ) from exc
    if response.status_code in (400, 401, 403):
        raise invalid_byok_key("gemini rejected this API key")
    if response.is_error:
        raise provider_error(f"gemini key validation returned HTTP {response.status_code}")


async def require_media_entitlement(
    async_dal: Any, dal: Any, *, tenant_id: int, community_id: int
) -> None:
    """Raise 402 `TIER_REQUIRED` unless the community's effective tier is Professional or higher."""
    try:
        tier = await effective_tier(async_dal, dal, tenant_id=tenant_id, community_id=community_id)
    except CommunityScopeError as exc:
        raise ApiError("Community not found", 404, "NOT_FOUND") from exc
    if not tier_meets_media_floor(tier):
        raise ApiError(
            "Media generation requires the Professional tier or higher", 402, "TIER_REQUIRED"
        )


def _require_provider(provider: str) -> None:
    if provider not in SUPPORTED_MEDIA_PROVIDERS:
        raise bad_request(f"invalid provider: {provider!r}")


async def set_media_key(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    community_id: int,
    provider: str,
    api_key: str,
    user_id: int,
) -> ByokKeyInfo:
    """Entitle, validate against Gemini, encrypt, and upsert (set-or-rotate) the community's key."""
    _require_provider(provider)
    if not api_key or not api_key.strip():
        raise bad_request("api_key is required")
    if len(api_key) > MAX_KEY_CHARS:
        raise bad_request("api_key is too long")
    await require_media_entitlement(async_dal, dal, tenant_id=tenant_id, community_id=community_id)
    await _validate_gemini_key(api_key.strip())
    return await config_service.store_byok_key(
        async_dal,
        dal,
        community_id=community_id,
        provider=provider,
        plaintext_key=api_key.strip(),
        created_by_user_id=user_id,
    )


async def media_key_status(
    async_dal: Any, dal: Any, *, tenant_id: int, community_id: int
) -> list[ByokKeyInfo]:
    """Masked metadata for the community's media keys. The community must be in `tenant_id`."""
    if not await community_belongs_to_tenant(
        async_dal, dal, community_id=community_id, tenant_id=tenant_id
    ):
        raise ApiError("Community not found", 404, "NOT_FOUND")
    config_service.bind_ai_routing_tables(dal)
    rows = await async_dal.select_async(
        dal(
            (dal.ai_byok_keys.community_id == community_id)
            & dal.ai_byok_keys.provider.belongs(sorted(SUPPORTED_MEDIA_PROVIDERS))
        ),
        orderby=dal.ai_byok_keys.provider,
    )
    return [config_service._key_info(r) for r in rows]  # noqa: SLF001 - same package family


async def delete_media_key(
    async_dal: Any, dal: Any, *, tenant_id: int, community_id: int, provider: str
) -> None:
    """Deactivate the community's key. Deliberately NOT tier-gated (a downgrade may revoke)."""
    _require_provider(provider)
    if not await community_belongs_to_tenant(
        async_dal, dal, community_id=community_id, tenant_id=tenant_id
    ):
        raise ApiError("Community not found", 404, "NOT_FOUND")
    await config_service.deactivate_byok_key(
        async_dal, dal, community_id=community_id, provider=provider
    )


async def resolve_media_key(
    async_dal: Any, dal: Any, *, tenant_id: int, community_id: int, provider: str
) -> MediaResolution:
    """Tier-gate then decrypt the community's active key for the Rust media host.

    Order is fixed and fail-closed: scope check (raises `CommunityScopeError`) -> effective tier
    (`tier_required` below Professional) -> key lookup (`not_configured` when absent/inactive).
    The community-level key is the only level stored today; a tenant-level fallback is deferred
    (the stored scope is the community). Returns the plaintext ONLY on `ok`; the caller must never
    log or persist it.
    """
    _require_provider(provider)
    tier = await effective_tier(async_dal, dal, tenant_id=tenant_id, community_id=community_id)
    if not tier_meets_media_floor(tier):
        return MediaResolution(STATUS_TIER_REQUIRED, tier)
    key = await config_service.get_active_byok_key_plaintext(
        async_dal, dal, community_id=community_id, provider=provider
    )
    if key is None:
        return MediaResolution(STATUS_NOT_CONFIGURED, tier)
    return MediaResolution(STATUS_OK, tier, key)
