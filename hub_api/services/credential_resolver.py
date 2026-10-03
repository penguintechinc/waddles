"""`CredentialResolver` -- the per-tenant platform-credential seam for Bar Citizen (migration 0034).

**Foundation module.** Units C/D (Discord/Twitch OAuth bot-install flows)
and Unit F (the role-sync worker) resolve platform app credentials through
`CredentialResolver.resolve()` rather than reading env vars or
`tenant_platform_credentials` directly -- one stable, typed seam, pinned
for those units to build against (mirrors `community_connections.py`'s own
"pinned, not renamed" contract precedent for other chunk-contract modules
in this port).

Two lanes, selected by `is_global_tenant` (the caller's own
`flask_core.tenancy.TenantContext.is_default`, never re-derived here):

- Tenant 0 (global/SaaS): `{PLATFORM}_CLIENT_ID`/`_CLIENT_SECRET`/
  `_BOT_TOKEN` env vars -- the same `{PLATFORM}_CLIENT_ID`/
  `_CLIENT_SECRET` naming `services/oauth_providers.py` already reads for
  this app's own SaaS-wide OAuth app, extended with an optional bot-token
  env var for platforms (Discord) that need one.
- Tenant N: decrypt `tenant_platform_credentials.credentials_ciphertext`
  (AES-256-GCM via `platform_integrations_crypto`, same primitive/wire
  format `community_connections.py` uses) and JSON-decode the platform-
  defined payload -- migration 0034's own documented shape
  (`{"client_id": ..., "client_secret": ..., "bot_token": ..., "extra": {...}}`).

Every failure mode (missing env vars, missing DB row, decrypt failure,
malformed JSON) raises the single `TransportUnavailable` error -- callers
get one exception type to handle ("this platform isn't configured for
this tenant"), never a raw `KeyError`/`PlatformCredentialCryptoError`/
`json.JSONDecodeError` leaking through the seam.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from services.errors import bad_request
from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
    decrypt_value,
    encrypt_token,
)
from services.schema import bind_bar_citizen_tables

logger = logging.getLogger(__name__)


class TransportUnavailable(Exception):  # noqa: N818 - contract-pinned name, see module docstring
    """Raised when no usable credentials exist for a `(tenant_id, platform)` pair.

    Covers both lanes `resolve()` can take: tenant 0's SaaS env vars unset,
    or tenant N's DB row missing/undecryptable/malformed. Callers surface
    this as a clear "platform not configured" response rather than a raw
    decrypt or env-lookup failure.
    """


@dataclass(slots=True, frozen=True)
class PlatformCredentials:
    """Resolved, decrypted app credentials for one `(tenant_id, platform)` pair.

    `payload` is the platform-defined JSON shape (migration 0034's own
    documented convention: `client_id`/`client_secret`/`bot_token`/`extra`)
    -- callers pull whichever keys their platform integration needs.
    `source` (`"saas"` or `"tenant"`) is informational/audit only, never an
    authorization input.
    """

    tenant_id: int
    platform: str
    payload: dict[str, Any]
    source: str


class CredentialResolver(Protocol):
    """The stable interface Units C/D/F import -- one method, one typed result, one error type."""

    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> PlatformCredentials:
        """Return `platform`'s resolved credentials for `tenant_id`.

        Raises `TransportUnavailable` if none exist -- never returns a
        partially-populated result.
        """
        ...


def _saas_env_payload(platform: str) -> dict[str, Any] | None:
    """Read `{PLATFORM}_CLIENT_ID`/`_CLIENT_SECRET`/`_BOT_TOKEN` env vars for tenant 0.

    Matches `services/oauth_providers.py`'s own `client_id_env`/
    `client_secret_env` naming (`TWITCH_CLIENT_ID`, `DISCORD_CLIENT_SECRET`,
    ...) -- one source of truth for "what are this platform's SaaS
    credentials called", not a second env var map drifting from it.
    """
    prefix = platform.upper()
    client_id = os.getenv(f"{prefix}_CLIENT_ID")
    client_secret = os.getenv(f"{prefix}_CLIENT_SECRET")
    bot_token = os.getenv(f"{prefix}_BOT_TOKEN")
    if not client_id or not client_secret:
        return None
    payload: dict[str, Any] = {"client_id": client_id, "client_secret": client_secret}
    if bot_token:
        payload["bot_token"] = bot_token
    return payload


def _select_tenant_credentials_row(dal: Any, tenant_id: int, platform: str) -> Any | None:
    bind_bar_citizen_tables(dal)
    t = dal.tenant_platform_credentials
    return dal((t.tenant_id == tenant_id) & (t.platform == platform)).select().first()


class DefaultCredentialResolver:
    """Default `CredentialResolver`: tenant 0 -> SaaS env vars, tenant N -> decrypted DB row."""

    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> PlatformCredentials:
        """See `CredentialResolver.resolve`."""
        if is_global_tenant:
            payload = _saas_env_payload(platform)
            if payload is None:
                raise TransportUnavailable(
                    f"no SaaS credentials configured for platform '{platform}'"
                )
            return PlatformCredentials(
                tenant_id=tenant_id, platform=platform, payload=payload, source="saas"
            )

        row = _select_tenant_credentials_row(dal, tenant_id, platform)
        if row is None:
            raise TransportUnavailable(
                f"tenant {tenant_id} has no stored credentials for platform '{platform}'"
            )

        try:
            plaintext = decrypt_value(row.credentials_ciphertext)
        except PlatformCredentialCryptoError as exc:
            logger.error(
                "credential_resolver.decrypt_failed tenant_id=%s platform=%s",
                tenant_id,
                platform,
            )
            raise TransportUnavailable(
                f"stored credentials for tenant {tenant_id} platform '{platform}' are unreadable"
            ) from exc

        try:
            payload = json.loads(plaintext)
        except json.JSONDecodeError as exc:
            raise TransportUnavailable(
                f"stored credentials for tenant {tenant_id} platform '{platform}' are malformed"
            ) from exc

        return PlatformCredentials(
            tenant_id=tenant_id, platform=platform, payload=payload, source="tenant"
        )


def store_tenant_credentials(
    dal: Any,
    *,
    tenant_id: int,
    is_global_tenant: bool,
    platform: str,
    payload: dict[str, Any],
    installed_by_user_id: int | None,
) -> None:
    """Encrypt + upsert tenant N's own `(tenant_id, platform)` credentials row.

    The write side of this seam -- Units C/D's OAuth bot-install flow calls
    this once a tenant admin supplies their own app credentials. Not part
    of the `CredentialResolver` read interface itself (resolvers only
    read); kept in this module since it writes the exact row `resolve()`
    reads back. Raises `bad_request` for the global tenant -- it never
    stores a row here (also enforced by migration 0034's own
    `trg_reject_global_tenant_credentials` DB trigger; this is a faster,
    clearer failure before hitting it).
    """
    if is_global_tenant:
        raise bad_request("the global tenant cannot store platform credentials")

    bind_bar_citizen_tables(dal)
    t = dal.tenant_platform_credentials
    ciphertext = encrypt_token(json.dumps(payload))
    now = datetime.now(UTC)
    try:
        existing = dal((t.tenant_id == tenant_id) & (t.platform == platform)).select().first()
        if existing is None:
            t.insert(
                tenant_id=tenant_id,
                platform=platform,
                credentials_ciphertext=ciphertext,
                installed_by_user_id=installed_by_user_id,
                created_at=now,
                updated_at=now,
            )
        else:
            dal(t.id == existing.id).update(
                credentials_ciphertext=ciphertext,
                installed_by_user_id=installed_by_user_id,
                updated_at=now,
            )
        dal.commit()
    except Exception:
        dal.rollback()
        raise
