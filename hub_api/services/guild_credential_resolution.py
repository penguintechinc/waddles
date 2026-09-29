"""Internal-only bot-credential resolution for the data plane (contract Sec2).

`resolve_credentials()` is the ONLY function that should ever hand a bot
token to a caller outside `hub_api`'s own credential-storage service --
mirrors the contract doc's `resolve_credentials(tenant_id, platform)`
pseudocode exactly:

    if tenants.is_global(tenant_id):
        return platform_credentials_from_cluster_secret()  # waddlebot-platform-credentials (#478)
    row = SELECT * FROM tenant_platform_credentials WHERE tenant_id=? AND platform=? AND is_active
    if row is None:
        fail_closed("tenant Discord app not configured")   # NEVER fall back to the platform bot
    return decrypt(row)

PR #442 (per-tenant DEK broker) and PR #455 (internal gRPC `KeyService`)
are both still open/unmerged as of this module's authoring -- the contract
doc calls out PR #442's `internal_keys.py` pattern as the shape to reuse
"once it merges", not something to implement speculatively today. This
module is therefore exposed as a plain internal REST endpoint
(`blueprints/v1/guild_pairing_oauth.py`'s `discord_credential_internal_bp`,
`X-Service-Key` auth -- the existing internal-endpoint convention this
port already uses everywhere else, e.g. `community_activity.py`'s
`activity_internal_bp`) over TLS, not a sealed-delivery/gRPC transport.
**Documented migration seam:** once PR #455 lands, add a `ResolveCredentials`
RPC to `libs/grpc_protos/hub_internal.proto`'s `HubInternalService` that
calls this exact function and delivers the result sealed to the caller's
ephemeral X25519 key (PR #442's sealed-delivery pattern) instead of plain
TLS+JWT; this function's signature does not need to change for that.
"""

from __future__ import annotations

from dataclasses import dataclass

from penguin_dal import AsyncDB

from services.errors import ApiError
from services.tenant_platform_credentials_service import PLATFORM_DISCORD, decrypt_bot_token


class CredentialResolutionError(ApiError):
    """Raised when a bot token cannot be resolved -- always a fail-closed 409, never a fallback."""


@dataclass(slots=True, frozen=True)
class ResolvedCredential:
    """A resolved bot credential -- either the platform-Secret reference or a tenant's own token."""

    tenant_id: int
    platform: str
    is_platform_bot: bool
    #: `None` when `is_platform_bot` is True -- the caller resolves the
    #: actual token from the `waddlebot-platform-credentials` cluster
    #: Secret itself (#478), never through this module, which has no
    #: access to cluster Secrets.
    bot_token: str | None


async def resolve_credentials(
    install_dal: AsyncDB, *, tenant_id: int, platform: str = PLATFORM_DISCORD
) -> ResolvedCredential:
    """Resolve the bot credential to use for `tenant_id`, per contract Sec2's fail-closed rule.

    The global tenant always resolves to the platform-Secret reference
    (`is_platform_bot=True`, `bot_token=None`) -- this module never reads
    or returns the cluster Secret's contents, only signals which path the
    caller (already privileged to read that Secret, e.g. mounted into its
    own pod) should use. A non-global tenant with no configured app raises
    `CredentialResolutionError` -- callers MUST propagate this as a hard
    failure, never substitute the platform bot.
    """
    row = (
        await install_dal(install_dal.tenants.id == tenant_id).select(install_dal.tenants.is_global)
    ).first()
    if row is None:
        raise CredentialResolutionError(f"tenant {tenant_id} not found", 404, "NOT_FOUND")

    if bool(getattr(row, "is_global", False)):
        return ResolvedCredential(
            tenant_id=tenant_id, platform=platform, is_platform_bot=True, bot_token=None
        )

    bot_token = await decrypt_bot_token(install_dal, tenant_id=tenant_id, platform=platform)
    if bot_token is None:
        raise CredentialResolutionError(
            f"tenant {platform} app not configured", 409, "TENANT_APP_NOT_CONFIGURED"
        )

    return ResolvedCredential(
        tenant_id=tenant_id, platform=platform, is_platform_bot=False, bot_token=bot_token
    )
