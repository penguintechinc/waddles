"""Per-tenant ingest source registry (spec Sec5.2, Sec10.3).

Secret shown once, AES-256-GCM at rest.

R52: `ingest_sources`/`workstreams` are this slice's own new tables
(migrations 0020-0021), queried through the penguin-dal
`install_dal: AsyncDB` (`services/bundle_install_dal.py`), never the
pre-existing pydal `async_dal`/`dal` pair.

**Scope note.** The full M2b plan's `ingest_source_service.py` (Task 26,
extended by Task 39) also carries a per-source `auth` config (IP
allowlist/bearer/basic second factor) for the generic webhook intake
route. That is out of this slice's scope -- see
`alembic/versions/0020_ingest_sources_and_rbac_roles.py`'s own scope
note. This version carries the HMAC secret + JSON-pointer `mapping`
only.

`create_source()` inserts `ingest_sources` and its 1:1 `workstreams`
row inside one `engine.begin()` transaction (Decision #18(a) in the
full plan): a crash between the two inserts would otherwise leave an
`ingest_sources` row with no workstream. Written directly against the
connection, not through a generic multi-statement helper, because the
workstream insert needs the ingest source's generated id.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.bundle_secret_crypto import decrypt, encrypt
from services.errors import ApiError, not_found
from services.workstream_service import disable_workstream_for_source


async def create_source(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int | None,
    platform: str,
    source_id: str,
    label: str,
    mapping: dict[str, Any] | None,
) -> tuple[Any, str]:
    """Register a new ingest source.

    Returns `(row, plaintext_secret)` -- the secret is shown exactly once.

    Atomically creates the source's 1:1 `workstreams` row alongside it
    (spec Sec5.11, D30: "created ... in lockstep with it").
    """
    existing = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.platform == platform)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    if existing:
        raise ApiError(
            f"ingest source {source_id!r} already exists for platform {platform!r}",
            409,
            "CONFLICT",
        )

    plaintext_secret = secrets.token_urlsafe(32)
    ciphertext, iv = encrypt(plaintext_secret)
    now = datetime.now(UTC)

    ingest_sources_table = install_dal.ingest_sources.table
    workstreams_table = install_dal.workstreams.table
    async with install_dal.engine.begin() as conn:
        result = await conn.execute(
            ingest_sources_table.insert().values(
                tenant_id=tenant_id,
                community_id=community_id,
                platform=platform,
                source_id=source_id,
                label=label,
                secret_ciphertext=ciphertext,
                secret_iv=iv,
                mapping=mapping,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        new_id = result.inserted_primary_key[0]
        await conn.execute(
            workstreams_table.insert().values(
                tenant_id=tenant_id,
                community_id=community_id,
                ingest_source_id=new_id,
                platform=platform,
                source_id=source_id,
                created_at=now,
            )
        )

    row = (await install_dal(install_dal.ingest_sources.id == new_id).select()).first()
    return row, plaintext_secret


async def ensure_ingest_source(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int | None,
    platform: str,
    source_id: str,
    label: str,
    mapping: dict[str, Any] | None = None,
) -> Any:
    """Idempotently register a platform-managed ingest source, storing NO webhook secret.

    Core-bundle-seeder milestone: a "platform connection" (e.g. svc-ingest's Discord guild,
    `core/svc_ingest`'s own bot token already authenticates it independently, via a Helm
    secret) needs an `ingest_sources` row to exist before `app_source_binding_service.
    sync_bindings()`'s auto-bind can match a bundle's `consumes` rule against it -- but unlike
    `create_source()` above (the human-facing generic-webhook-intake flow, which always
    mints+encrypts an HMAC secret an external caller must sign requests with),
    a platform connection's own client authenticates through a completely different
    mechanism this table plays no part in. `secret_ciphertext`/`secret_iv` stay NULL here,
    always -- a secret-REFERENCE design for this class of connection (pointing at the same
    credential svc-ingest itself already holds, never a copy) is a documented follow-on, not
    yet built; this function must never be the second place a real credential is copied to.

    Same `(tenant_id, platform, source_id)` uniqueness key as `create_source()` (migration
    0020's own `UNIQUE (tenant_id, platform, source_id)`) -- re-running with identical
    arguments is always a safe no-op; a previously-disabled row is re-enabled (never
    duplicated), matching this function's own idempotency contract. Unlike `create_source()`,
    this never creates the paired `workstreams` row -- that table is scoped to generic-webhook
    usage metering (spec Sec5.11), orthogonal to whether a bundle can bind and consume this
    source's stream, which needs only this table (see `app_source_binding_service.py`'s own
    module docstring).
    """
    existing = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.platform == platform)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    row = existing.first()
    now = datetime.now(UTC)
    if row is not None:
        if not row.enabled:
            await install_dal(install_dal.ingest_sources.id == row.id).update(
                enabled=True, updated_at=now
            )
            row = (await install_dal(install_dal.ingest_sources.id == row.id).select()).first()
        return row

    new_id = await install_dal.ingest_sources.async_insert(
        tenant_id=tenant_id,
        community_id=community_id,
        platform=platform,
        source_id=source_id,
        label=label,
        secret_ciphertext=None,
        secret_iv=None,
        mapping=mapping,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    return (await install_dal(install_dal.ingest_sources.id == new_id).select()).first()


async def list_sources(install_dal: AsyncDB, *, tenant_id: int) -> list[Any]:
    """Every ingest source for a tenant. Never returns the plaintext secret."""
    rows = await install_dal(install_dal.ingest_sources.tenant_id == tenant_id).select()
    return list(rows)


async def delete_source(install_dal: AsyncDB, *, tenant_id: int, source_id: str) -> None:
    """Remove an ingest source by its `source_id`. Raises 404 if absent.

    Disables the source's workstream (never deletes it) before the
    source row itself is removed -- `workstream_usage_hourly` keeps its
    FK target for the life of the tenant's usage history.
    """
    existing = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    row = existing.first()
    if row is None:
        raise not_found(f"ingest source {source_id!r} not found")
    await disable_workstream_for_source(install_dal, ingest_source_id=row.id)
    await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).delete()


async def resolve_secret(
    install_dal: AsyncDB, *, tenant_id: int, platform: str, source_id: str
) -> str | None:
    """Decrypt and return an ingest source's webhook secret, or `None` if it has none."""
    rows = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.platform == platform)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    row = rows.first()
    if row is None or row.secret_ciphertext is None:
        return None
    return decrypt(row.secret_ciphertext, row.secret_iv)
