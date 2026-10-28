"""One-time secret messages -- create and single-use pull (feature #684).

Backs `!secret <user> <msg>`. The creator (a bundle/service token) stores a
message for a target `hub_users.uuid`; the target later pulls it once
through the webui. Guarantees, all enforced here (never in the caller):

- **Encrypted at rest**: only AES-256-GCM ciphertext is stored
  (`services/one_time_secret_crypto.py`); the link token is stored as a
  SHA-256 hash, so a DB read yields neither message nor a usable link.
- **Single use, atomic**: pull claims the row with one conditional
  `UPDATE ... WHERE pulled_at IS NULL ... RETURNING` inside a transaction
  (row-locked on Postgres), reads the ciphertext, hard-deletes the row,
  and only then decrypts -- two concurrent pulls cannot both win, and a
  decrypt failure still burns the secret (fail-closed).
- **Target-only**: the caller's own `hub_users.uuid` must equal the row's
  `target_user_uuid` and tenants must match; a mismatch is a 403 that does
  NOT consume the secret.
- **Expiring**: expired rows are gone (410) and purged.
- **PII-free logs**: only community_id, op, a masked secret id and the
  exception type are logged -- never the body, token, or any username.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from penguin_dal import AsyncDB
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    Row,
    String,
    Table,
    Uuid,
    delete,
    select,
    update,
)

from services import one_time_secret_crypto as crypto
from services.errors import ApiError, bad_request, forbidden, not_found

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4000
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_TTL_SECONDS = 24 * 3600

#: Core mirror of migration 0045 (portable Uuid/aware-datetime binding on
#: both Postgres and the sqlite unit-test fixture).
METADATA = MetaData()
one_time_secrets = Table(
    "one_time_secrets",
    METADATA,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("tenant_id", Integer, nullable=False),
    Column("community_id", Integer, nullable=False),
    Column("target_user_uuid", Uuid(as_uuid=True), nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("iv", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("pulled_at", DateTime(timezone=True)),
)
#: Read-only mirrors of the existing tables/columns this module reads (never created here).
_hub_users = Table(
    "hub_users",
    MetaData(),
    Column("id", Integer, primary_key=True),
    Column("uuid", Uuid(as_uuid=True)),
)
_communities = Table(
    "communities",
    MetaData(),
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer),
)


class SecretGoneError(ApiError):
    """The secret is unknown, already pulled, or expired (HTTP 410)."""

    def __init__(self) -> None:
        """Build the fixed 410 -- never reveals which of the three it was."""
        super().__init__("Secret is gone", 410, "SECRET_GONE")


@dataclass(slots=True, frozen=True)
class CreatedSecret:
    """Result of `create_secret` -- `token` is returned to the creator exactly once."""

    secret_id: str
    token: str
    expires_at: datetime


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a link token (the only form that is stored)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_id(secret_id: uuid.UUID | str) -> str:
    """Log-safe short form of a secret id."""
    return f"{str(secret_id)[:8]}****"


def _now() -> datetime:
    return datetime.now(UTC)


async def create_secret(
    dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    target_user_uuid: uuid.UUID,
    message: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> CreatedSecret:
    """Encrypt and store `message` for `target_user_uuid`; return the one-time token."""
    if not message or len(message) > MAX_MESSAGE_CHARS:
        raise bad_request(f"message must be 1-{MAX_MESSAGE_CHARS} characters")
    if not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS:
        raise bad_request(f"ttlSeconds must be {MIN_TTL_SECONDS}-{MAX_TTL_SECONDS}")

    async with dal.engine.begin() as conn:
        community = (
            await conn.execute(
                select(_communities.c.id).where(
                    _communities.c.id == community_id, _communities.c.tenant_id == tenant_id
                )
            )
        ).first()
        if community is None:
            raise not_found("Community not found")
        target = (
            await conn.execute(select(_hub_users.c.id).where(_hub_users.c.uuid == target_user_uuid))
        ).first()
        if target is None:
            raise not_found("Target user not found")

        now = _now()
        await conn.execute(delete(one_time_secrets).where(one_time_secrets.c.expires_at <= now))
        secret_id = uuid.uuid4()
        token = secrets.token_urlsafe(32)
        ciphertext, iv = crypto.encrypt(message, row_id=str(secret_id))
        expires_at = now + timedelta(seconds=ttl_seconds)
        await conn.execute(
            one_time_secrets.insert().values(
                id=secret_id,
                token_hash=hash_token(token),
                tenant_id=tenant_id,
                community_id=community_id,
                target_user_uuid=target_user_uuid,
                ciphertext=ciphertext,
                iv=iv,
                created_at=now,
                expires_at=expires_at,
            )
        )
    logger.info(
        "one_time_secret.created",
        extra={"op": "create", "community_id": community_id, "secret": mask_id(secret_id)},
    )
    return CreatedSecret(secret_id=str(secret_id), token=token, expires_at=expires_at)


async def pull_secret(dal: AsyncDB, *, tenant_id: int, caller_user_id: int, token: str) -> str:
    """Return the secret body exactly once for the linked target user, then delete it.

    Raises `SecretGoneError` (410) for unknown/expired/already-pulled, 403 when the
    caller is not the linked target (secret is NOT consumed in that case).
    """
    token_hash = hash_token(token)
    t = one_time_secrets
    blob: Row[Any] | None = None
    async with dal.engine.begin() as conn:
        caller_uuid = (
            await conn.execute(select(_hub_users.c.uuid).where(_hub_users.c.id == caller_user_id))
        ).scalar()
        row = (
            await conn.execute(
                select(
                    t.c.id, t.c.tenant_id, t.c.community_id, t.c.target_user_uuid, t.c.expires_at
                ).where(t.c.token_hash == token_hash)
            )
        ).first()
        if row is None:
            raise SecretGoneError()
        now = _now()
        expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
        expired = expires_at <= now
        if expired:
            # Purge, then raise AFTER the transaction commits (a raise inside
            # `begin()` would roll the delete back).
            await conn.execute(delete(t).where(t.c.id == row.id))
        elif (
            caller_uuid is None or row.target_user_uuid != caller_uuid or row.tenant_id != tenant_id
        ):
            logger.warning(
                "one_time_secret.pull_denied",
                extra={"op": "pull", "community_id": row.community_id, "secret": mask_id(row.id)},
            )
            raise forbidden("Not the recipient of this secret")

        claimed = (
            None
            if expired
            else (
                await conn.execute(
                    update(t)
                    .where(t.c.id == row.id, t.c.pulled_at.is_(None), t.c.expires_at > now)
                    .values(pulled_at=now)
                    .returning(t.c.id)
                )
            ).first()
        )
        if claimed is not None:
            blob = (
                await conn.execute(select(t.c.ciphertext, t.c.iv).where(t.c.id == row.id))
            ).one()
            await conn.execute(delete(t).where(t.c.id == row.id))
    if blob is None:
        raise SecretGoneError()

    try:
        message = crypto.decrypt(blob.ciphertext, blob.iv, row_id=str(row.id))
    except Exception as exc:
        logger.error(
            "one_time_secret.decrypt_failed",
            extra={
                "op": "pull",
                "community_id": row.community_id,
                "secret": mask_id(row.id),
                "exc_type": type(exc).__name__,
            },
        )
        raise ApiError("Secret could not be decrypted", 500, "SECRET_DECRYPT_FAILED") from exc
    logger.info(
        "one_time_secret.pulled",
        extra={"op": "pull", "community_id": row.community_id, "secret": mask_id(row.id)},
    )
    return message
