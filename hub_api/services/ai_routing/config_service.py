"""Per-community AI config + BYOK key CRUD -- `ai_model_config`/`ai_byok_keys`.

BYOK keys are validated against the real provider API (`clients.
validate_byok_key()`) before they are ever encrypted/committed, encrypted
at rest with `byok_crypto.encrypt_key()` (AES-256-GCM), and only ever
decrypted inside `router.py` immediately before an outbound provider call
-- `get_active_byok_key_plaintext()` is the one function in this whole
package that returns a real key value; every other accessor here returns
`key_last4` only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.ai_routing.byok_crypto import decrypt_key, encrypt_key, mask_key
from services.ai_routing.clients import validate_byok_key
from services.ai_routing.models import ByokProvider, OnInsufficientBalance, Tier
from services.errors import bad_request, not_found
from services.schema import bind_ai_routing_tables

_VALID_TIERS = frozenset({"free", "premium", "byok"})
_VALID_PROVIDERS = frozenset({"openai", "anthropic"})
#: Media (Gemini) BYOK rows share the `ai_byok_keys` store + AES-GCM key but are managed ONLY by
#: `services.media_byok_service`; the AI router/config paths never see or select them.
MEDIA_PROVIDERS = frozenset({"gemini"})
_VALID_BALANCE_POLICIES = frozenset({"block", "fallback_free"})


@dataclass(slots=True, frozen=True)
class AIConfig:
    """A community's resolved AI-routing config -- defaults apply when no row exists yet."""

    community_id: int
    preferred_tier: Tier
    byok_provider: ByokProvider | None
    on_insufficient_balance: OnInsufficientBalance


@dataclass(slots=True, frozen=True)
class ByokKeyInfo:
    """Masked view of one `ai_byok_keys` row -- never carries the real key."""

    provider: str
    key_last4: str
    is_active: bool
    created_at: Any
    updated_at: Any
    rotated_at: Any


async def get_ai_config(async_dal: Any, dal: Any, *, community_id: int) -> AIConfig:
    """Return `community_id`'s AI config, or the free-tier default if never configured."""
    bind_ai_routing_tables(dal)
    rows = await async_dal.select_async(dal(dal.ai_model_config.community_id == community_id))
    if not rows:
        return AIConfig(
            community_id=community_id,
            preferred_tier="free",
            byok_provider=None,
            on_insufficient_balance="fallback_free",
        )
    row = rows.first()
    return AIConfig(
        community_id=community_id,
        preferred_tier=row.preferred_tier,
        byok_provider=row.byok_provider,
        on_insufficient_balance=row.on_insufficient_balance,
    )


async def set_ai_config(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    preferred_tier: str,
    byok_provider: str | None,
    on_insufficient_balance: str,
    updated_by_user_id: int,
) -> AIConfig:
    """Upsert `community_id`'s AI-routing config. Raises 400 on an invalid enum value."""
    if preferred_tier not in _VALID_TIERS:
        raise bad_request(f"invalid preferred_tier: {preferred_tier!r}")
    if byok_provider is not None and byok_provider not in _VALID_PROVIDERS:
        raise bad_request(f"invalid byok_provider: {byok_provider!r}")
    if on_insufficient_balance not in _VALID_BALANCE_POLICIES:
        raise bad_request(f"invalid on_insufficient_balance: {on_insufficient_balance!r}")

    bind_ai_routing_tables(dal)
    now = datetime.now(UTC)
    existing = await async_dal.select_async(dal(dal.ai_model_config.community_id == community_id))
    if existing:
        await async_dal.update_async(
            dal.ai_model_config.community_id == community_id,
            preferred_tier=preferred_tier,
            byok_provider=byok_provider,
            on_insufficient_balance=on_insufficient_balance,
            updated_at=now,
            updated_by_user_id=updated_by_user_id,
        )
    else:
        await async_dal.insert_async(
            dal.ai_model_config,
            community_id=community_id,
            preferred_tier=preferred_tier,
            byok_provider=byok_provider,
            on_insufficient_balance=on_insufficient_balance,
            created_at=now,
            updated_at=now,
            updated_by_user_id=updated_by_user_id,
        )
    return await get_ai_config(async_dal, dal, community_id=community_id)


def _key_info(row: Any) -> ByokKeyInfo:
    return ByokKeyInfo(
        provider=row.provider,
        key_last4=row.key_last4,
        is_active=bool(row.is_active),
        created_at=row.created_at,
        updated_at=row.updated_at,
        rotated_at=row.rotated_at,
    )


async def list_byok_keys(async_dal: Any, dal: Any, *, community_id: int) -> list[ByokKeyInfo]:
    """List every BYOK key on file for `community_id`, active or not -- masked."""
    bind_ai_routing_tables(dal)
    rows = await async_dal.select_async(
        dal(
            (dal.ai_byok_keys.community_id == community_id)
            & dal.ai_byok_keys.provider.belongs(sorted(_VALID_PROVIDERS))
        ),
        orderby=dal.ai_byok_keys.provider,
    )
    return [_key_info(r) for r in rows]


async def set_byok_key(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    provider: str,
    plaintext_key: str,
    created_by_user_id: int,
) -> ByokKeyInfo:
    """Validate, encrypt, and upsert (set-or-rotate) a BYOK key. Never logs `plaintext_key`.

    Validation happens BEFORE any DB write (spec §3: "validates the new
    key with a cheap /models call before committing") -- a rejected key
    never reaches the database, encrypted or not.
    """
    if provider not in _VALID_PROVIDERS:
        raise bad_request(f"invalid provider: {provider!r}")
    if not plaintext_key or not plaintext_key.strip():
        raise bad_request("api_key is required")

    await validate_byok_key(provider, plaintext_key)  # type: ignore[arg-type]

    return await store_byok_key(
        async_dal,
        dal,
        community_id=community_id,
        provider=provider,
        plaintext_key=plaintext_key,
        created_by_user_id=created_by_user_id,
    )


async def store_byok_key(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    provider: str,
    plaintext_key: str,
    created_by_user_id: int,
) -> ByokKeyInfo:
    """Encrypt and upsert (set-or-rotate) an ALREADY-VALIDATED key; never logs `plaintext_key`.

    Shared by the AI path above and `services.media_byok_service` (which validates against its own
    provider first). Provider validity is the caller's job -- this only persists.
    """
    bind_ai_routing_tables(dal)
    encrypted = encrypt_key(plaintext_key)
    last4 = mask_key(plaintext_key)
    now = datetime.now(UTC)

    existing = await async_dal.select_async(
        dal(
            (dal.ai_byok_keys.community_id == community_id)
            & (dal.ai_byok_keys.provider == provider)
        )
    )
    if existing:
        await async_dal.update_async(
            (dal.ai_byok_keys.community_id == community_id)
            & (dal.ai_byok_keys.provider == provider),
            encrypted_key=encrypted,
            key_last4=last4,
            is_active=True,
            updated_at=now,
            rotated_at=now,
        )
    else:
        await async_dal.insert_async(
            dal.ai_byok_keys,
            community_id=community_id,
            provider=provider,
            encrypted_key=encrypted,
            key_last4=last4,
            is_active=True,
            created_at=now,
            updated_at=now,
            rotated_at=None,
            created_by_user_id=created_by_user_id,
        )

    rows = await async_dal.select_async(
        dal(
            (dal.ai_byok_keys.community_id == community_id)
            & (dal.ai_byok_keys.provider == provider)
        )
    )
    return _key_info(rows.first())


async def delete_byok_key(async_dal: Any, dal: Any, *, community_id: int, provider: str) -> None:
    """Deactivate (never hard-delete, keeps the audit trail) `community_id`'s `provider` key."""
    if provider not in _VALID_PROVIDERS:
        raise bad_request(f"invalid provider: {provider!r}")
    await deactivate_byok_key(async_dal, dal, community_id=community_id, provider=provider)


async def deactivate_byok_key(
    async_dal: Any, dal: Any, *, community_id: int, provider: str
) -> None:
    """Deactivate one stored key (404 when none on file). Provider validity is the caller's job."""
    bind_ai_routing_tables(dal)
    existing = await async_dal.select_async(
        dal(
            (dal.ai_byok_keys.community_id == community_id)
            & (dal.ai_byok_keys.provider == provider)
        )
    )
    if not existing:
        raise not_found(f"No {provider} key configured for this community")
    await async_dal.update_async(
        (dal.ai_byok_keys.community_id == community_id) & (dal.ai_byok_keys.provider == provider),
        is_active=False,
        updated_at=datetime.now(UTC),
    )


async def get_active_byok_key_plaintext(
    async_dal: Any, dal: Any, *, community_id: int, provider: str
) -> str | None:
    """Decrypt and return `community_id`'s active key for `provider`, or `None` if unconfigured.

    The ONLY function in this package that returns a real, usable key
    value -- `router.py` calls it immediately before dispatching a BYOK
    request and never persists or logs the result.
    """
    bind_ai_routing_tables(dal)
    rows = await async_dal.select_async(
        dal(
            (dal.ai_byok_keys.community_id == community_id)
            & (dal.ai_byok_keys.provider == provider)
            & (dal.ai_byok_keys.is_active == True)  # noqa: E712
        )
    )
    if not rows:
        return None
    return decrypt_key(rows.first().encrypted_key)
