"""Per-tenant, per-platform application/bot credential storage (#500/#501, owner rules).

**Owner clarification (2026-09-29):** every tenant other than tenant 0
(global/default) ALWAYS requires its own app integration for EVERY
platform this product connects to (Discord, Twitch, YouTube, Kick, Slack,
Mattermost, Teams, etc.), not only Discord and not only when a guild is
shared. This module is therefore platform-generic: every function takes
`platform` as a first-class argument (`SUPPORTED_PLATFORMS` below), and
`tenant_platform_credentials` is keyed `(tenant_id, platform)` (migration
0038's own `UNIQUE` constraint, unchanged) with `bot_token`/`extra_secret`
nullable (migration 0039) -- not every platform's app credential includes
a separate bot token or an extra platform-specific secret.

Owner rules (task brief, `client.md` Authentication & Tokens, `security.md`
Secrets & Credentials), all platform-agnostic:

- Integrations are never shared across tenants -- each non-global tenant
  gets its own row per `(tenant_id, platform)`.
- The global tenant (`tenants.is_global`) never gets a row here for any
  platform -- it uses the cluster `waddlebot-platform-credentials` Secret
  (#478) for every platform. Migration 0038's
  `trg_reject_global_tenant_credentials` DB trigger is the actual
  enforcement; `set_credentials()` below adds a service-layer check purely
  so a caller gets a clean 409 instead of a raw `IntegrityError`.
- Secrets are write-only via the API: `set_credentials()` accepts
  plaintext once and never returns it; `get_masked_credentials()` returns
  only `...1234`-style hints plus timestamps (never ciphertext, never
  plaintext).
- Every set/rotate is audited via `services.bundle_audit.record()` --
  action name + platform only, never a secret value.

**Connect-flow status per platform (this module is storage/resolution
only -- see `services/platform_oauth_connectors.py` for the pluggable
per-platform OAuth connector registry):**

| Platform | Storage/rotation/resolution | OAuth connect flow |
|---|---|---|
| `discord` | yes | yes -- `services/guild_oauth_install_service.py` |
| `twitch`, `youtube`, `kick`, `slack`, `teams`, `mattermost` | yes | not yet -- storage-only |

Queried through `install_dal: penguin_dal.AsyncDB` (R52 -- this table is
one of migration 0038/0039's tables, same DAL pattern
`services/ingest_source_service.py` already uses), never a new pydal
binder.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services import bundle_audit
from services.errors import ApiError, conflict, not_found
from services.tenant_platform_credentials_crypto import (
    STATIC_ENV_KEY_REF,
    decrypt,
    encrypt,
    mask_secret,
)

#: Historical default/back-compat alias -- Discord was this table's first
#: caller (#500). New code should pass `platform` explicitly.
PLATFORM_DISCORD = "discord"

#: Every platform this product may hold a per-tenant app integration for.
#: A caller-supplied platform outside this set is rejected (400) rather
#: than silently creating an unresolvable, typo'd row -- see
#: `services/platform_oauth_connectors.py`'s own registry, which this set
#: must stay in sync with (checked by `test_platform_credentials_generic.
#: py::test_supported_platforms_match_connector_registry`).
SUPPORTED_PLATFORMS: frozenset[str] = frozenset(
    {"discord", "twitch", "youtube", "kick", "slack", "teams", "mattermost"}
)


class UnsupportedPlatformError(ApiError):
    """Raised when `platform` isn't in `SUPPORTED_PLATFORMS` -- always a 400."""


@dataclass(slots=True, frozen=True)
class MaskedCredentials:
    """GET response shape -- masked hints and timestamps only, never a decryptable value."""

    platform: str
    application_id: str
    client_secret_hint: str
    bot_token_hint: str | None
    extra_secret_hint: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


def _require_supported_platform(platform: str) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise UnsupportedPlatformError(
            f"unsupported platform {platform!r}; expected one of {sorted(SUPPORTED_PLATFORMS)}",
            400,
            "UNSUPPORTED_PLATFORM",
        )


async def _is_global_tenant(install_dal: AsyncDB, tenant_id: int) -> bool:
    row = (
        await install_dal(install_dal.tenants.id == tenant_id).select(install_dal.tenants.is_global)
    ).first()
    if row is None:
        raise not_found(f"tenant {tenant_id} not found")
    return bool(getattr(row, "is_global", False))


async def set_credentials(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    actor_id: int,
    platform: str = PLATFORM_DISCORD,
    application_id: str,
    client_secret: str,
    bot_token: str | None = None,
    extra_secret: str | None = None,
) -> MaskedCredentials:
    """Create or rotate a tenant's own app credentials for `platform`. Never returns a secret value.

    Fails closed with 409 for the global tenant, for EVERY platform -- see
    module docstring; the DB trigger is the real backstop, this is just a
    friendlier error. `bot_token`/`extra_secret` are optional -- not every
    platform's app credential has a separate bot token or extra secret.
    """
    _require_supported_platform(platform)
    if await _is_global_tenant(install_dal, tenant_id):
        raise conflict(
            f"the global tenant uses the cluster platform credentials Secret for {platform}; "
            "it cannot have its own tenant_platform_credentials row"
        )
    if not application_id or not client_secret:
        raise ApiError("application_id and client_secret are both required", 400, "BAD_REQUEST")

    secret_ciphertext, secret_iv = encrypt(client_secret)
    token_ciphertext, token_iv = encrypt(bot_token) if bot_token else (None, None)
    extra_ciphertext, extra_iv = encrypt(extra_secret) if extra_secret else (None, None)
    now = datetime.now(UTC)

    existing = (
        await install_dal(
            (install_dal.tenant_platform_credentials.tenant_id == tenant_id)
            & (install_dal.tenant_platform_credentials.platform == platform)
        ).select()
    ).first()

    values = {
        "application_id": application_id,
        "client_secret_ciphertext": secret_ciphertext,
        "client_secret_iv": secret_iv,
        "bot_token_ciphertext": token_ciphertext,
        "bot_token_iv": token_iv,
        "extra_secret_ciphertext": extra_ciphertext,
        "extra_secret_iv": extra_iv,
        "key_ref": STATIC_ENV_KEY_REF,
        "is_active": True,
        "updated_at": now,
    }

    if existing is None:
        action = "tenant_platform_credentials.create"
        await install_dal.tenant_platform_credentials.async_insert(
            tenant_id=tenant_id, platform=platform, created_at=now, **values
        )
    else:
        action = "tenant_platform_credentials.rotate"
        await install_dal(
            (install_dal.tenant_platform_credentials.tenant_id == tenant_id)
            & (install_dal.tenant_platform_credentials.platform == platform)
        ).update(**values)

    await bundle_audit.record(
        install_dal,
        actor_id=actor_id,
        action=action,
        target_type="tenant_platform_credentials",
        target_id=f"{tenant_id}:{platform}",
        details={"platform": platform, "application_id": application_id},
    )

    return MaskedCredentials(
        platform=platform,
        application_id=application_id,
        client_secret_hint=mask_secret(client_secret),
        bot_token_hint=mask_secret(bot_token) if bot_token else None,
        extra_secret_hint=mask_secret(extra_secret) if extra_secret else None,
        is_active=True,
        created_at=now,
        updated_at=now,
    )


async def get_masked_credentials(
    install_dal: AsyncDB, *, tenant_id: int, platform: str = PLATFORM_DISCORD
) -> MaskedCredentials:
    """GET response -- masked hints + timestamps only. Raises 404 if unconfigured for `platform`."""
    _require_supported_platform(platform)
    row = (
        await install_dal(
            (install_dal.tenant_platform_credentials.tenant_id == tenant_id)
            & (install_dal.tenant_platform_credentials.platform == platform)
        ).select()
    ).first()
    if row is None:
        raise not_found(f"tenant {platform} app not configured")

    client_secret = decrypt(row.client_secret_ciphertext, row.client_secret_iv, key_ref=row.key_ref)
    bot_token = (
        decrypt(row.bot_token_ciphertext, row.bot_token_iv, key_ref=row.key_ref)
        if row.bot_token_ciphertext is not None
        else None
    )
    extra_secret = (
        decrypt(row.extra_secret_ciphertext, row.extra_secret_iv, key_ref=row.key_ref)
        if getattr(row, "extra_secret_ciphertext", None) is not None
        else None
    )
    return MaskedCredentials(
        platform=platform,
        application_id=row.application_id,
        client_secret_hint=mask_secret(client_secret),
        bot_token_hint=mask_secret(bot_token) if bot_token else None,
        extra_secret_hint=mask_secret(extra_secret) if extra_secret else None,
        is_active=bool(row.is_active),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _resolve_row(install_dal: AsyncDB, *, tenant_id: int, platform: str) -> Any:
    """Internal-only: the one place that ever reads a decryptable row for a non-global tenant.

    Callers outside this module must go through `get_masked_credentials()`
    (API surface) or `services.guild_credential_resolution.resolve_credentials()`
    (data-plane resolution) -- never this function directly, to keep the
    "never returned for another tenant" invariant enforceable at a single
    seam (`resolve_credentials()` is itself tenant-id-scoped by its own
    caller-supplied argument, never a client-controlled value).
    """
    row = (
        await install_dal(
            (install_dal.tenant_platform_credentials.tenant_id == tenant_id)
            & (install_dal.tenant_platform_credentials.platform == platform)
            & (install_dal.tenant_platform_credentials.is_active == True)  # noqa: E712
        ).select()
    ).first()
    return row


async def decrypt_bot_token(install_dal: AsyncDB, *, tenant_id: int, platform: str) -> str | None:
    """Decrypt and return a tenant's own bot token for `platform`, or `None` if absent/inactive.

    Never crosses tenants -- `tenant_id` is always the caller's own
    resolved value, never taken from a request body/param at this layer.
    Also `None` for a configured row whose platform simply has no
    bot-token concept (e.g. a pure client-id/secret OAuth app) -- callers
    needing to distinguish "not configured at all" from "configured, no
    bot token" should call `get_masked_credentials()`/`_resolve_row`-based
    checks instead.
    """
    row = await _resolve_row(install_dal, tenant_id=tenant_id, platform=platform)
    if row is None or row.bot_token_ciphertext is None:
        return None
    return decrypt(row.bot_token_ciphertext, row.bot_token_iv, key_ref=row.key_ref)


async def decrypt_client_credentials(
    install_dal: AsyncDB, *, tenant_id: int, platform: str
) -> tuple[str, str] | None:
    """Decrypt and return `(application_id, client_secret)` for `platform`'s token exchange."""
    row = await _resolve_row(install_dal, tenant_id=tenant_id, platform=platform)
    if row is None:
        return None
    client_secret = decrypt(row.client_secret_ciphertext, row.client_secret_iv, key_ref=row.key_ref)
    return row.application_id, client_secret
