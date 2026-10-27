"""Marketplace sources: enums, record type and persistence helpers (Phase 1, Task 1).

A source is a pinned git repo that supplies bundles (design 4.1). This module owns the int enum
codes (`BuildMode`, `SourceStatus` -- never strings in the DB), the immutable `SourceRecord`
read model and the CRUD helpers the API (Task 6), scanner (Task 3) and updater (Task 8) call.

Persistence is SQLAlchemy Core over the reflected `marketplace_sources` table of the shared
`AsyncDB` (same seam as `app_command_service`), always parameter-bound. Only `public_id` is
logged or returned to callers' API layers; the numeric `id` never leaves this module's rows.
The DB enforces the hard invariants (https-only URL, 40-hex SHAs, prebuilt-needs-key,
sandbox-needs-ack, exactly one immutable built-in row); the checks here fail early with a clean
`ApiError` instead of an `IntegrityError`.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

from penguin_dal import AsyncDB
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from services.errors import bad_request, conflict, not_found
from services.marketplace_publisher_attestation import validate_publisher_key

logger = logging.getLogger(__name__)

_TABLE = "marketplace_sources"
_GIT_URL_RE = re.compile(r"^https://[^/\s@]+(/\S*)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_MAX = 100


class BuildMode(IntEnum):
    """How a source's bundles become `.wasm` (stored as SMALLINT)."""

    PREBUILT = 1
    SANDBOX = 2
    BUILTIN_IMAGE = 3


class SourceStatus(IntEnum):
    """Lifecycle/approval state of a source (stored as SMALLINT)."""

    PENDING_SCAN = 1
    ACTIVE = 2
    NEEDS_APPROVAL = 3
    DISABLED = 4
    ERROR = 5


@dataclass(slots=True, frozen=True)
class SandboxAck:
    """Operator sandbox trust acknowledgement (design 4.8): who, when, text hash, commit SHA."""

    by: int
    at: datetime
    text_hash: str
    commit_sha: str


@dataclass(slots=True, frozen=True)
class SourceRecord:
    """Read model of one `marketplace_sources` row, keyed externally by `public_id` only."""

    public_id: uuid.UUID
    name: str
    is_builtin: bool
    git_url: str | None
    ref: str | None
    pinned_commit_sha: str | None
    pending_commit_sha: str | None
    build_mode: BuildMode
    publisher_key_fingerprint: str | None
    publisher_public_key: str | None
    sandbox_ack_by: int | None
    sandbox_ack_at: datetime | None
    sandbox_ack_text_hash: str | None
    sandbox_ack_commit_sha: str | None
    status: SourceStatus
    last_scanned_at: datetime | None
    last_scan_error: str | None
    created_by: int | None
    created_at: datetime
    updated_at: datetime


def _table(dal: AsyncDB) -> Any:
    """Reflected `marketplace_sources` table (fails loudly if the migration has not run)."""
    try:
        return dal.metadata.tables[_TABLE]
    except KeyError as exc:  # pragma: no cover - deployment error, not a runtime path
        raise RuntimeError("marketplace_sources table not reflected; run migration 0067") from exc


def _to_record(row: Any) -> SourceRecord:
    """Map a result row to a `SourceRecord`, decoding the int enum codes."""
    return SourceRecord(
        public_id=row.public_id
        if isinstance(row.public_id, uuid.UUID)
        else uuid.UUID(str(row.public_id)),
        name=row.name,
        is_builtin=bool(row.is_builtin),
        git_url=row.git_url,
        ref=row.ref,
        pinned_commit_sha=row.pinned_commit_sha,
        pending_commit_sha=row.pending_commit_sha,
        build_mode=BuildMode(int(row.build_mode)),
        publisher_key_fingerprint=row.publisher_key_fingerprint,
        publisher_public_key=row.publisher_public_key,
        sandbox_ack_by=row.sandbox_ack_by,
        sandbox_ack_at=row.sandbox_ack_at,
        sandbox_ack_text_hash=row.sandbox_ack_text_hash,
        sandbox_ack_commit_sha=row.sandbox_ack_commit_sha,
        status=SourceStatus(int(row.status)),
        last_scanned_at=row.last_scanned_at,
        last_scan_error=row.last_scan_error,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_create(
    *,
    name: str,
    git_url: str,
    ref: str,
    build_mode: BuildMode,
    publisher_key_fingerprint: str | None,
    publisher_public_key: str | None,
    sandbox_ack: SandboxAck | None,
) -> None:
    """Early, clean-error mirror of the DB CHECKs for externally created sources."""
    if build_mode == BuildMode.BUILTIN_IMAGE:
        raise bad_request("built-in image sources cannot be created")
    if not name.strip() or len(name) > _NAME_MAX:
        raise bad_request(f"name must be 1-{_NAME_MAX} characters")
    if not _GIT_URL_RE.fullmatch(git_url):
        raise bad_request("git_url must be an https:// URL without credentials")
    if not ref.strip() or len(ref) > 255:
        raise bad_request("ref is required (<=255 characters)")
    if build_mode == BuildMode.PREBUILT:
        if not (publisher_key_fingerprint and publisher_public_key):
            raise bad_request("prebuilt sources require a registered publisher key")
        if validate_publisher_key(publisher_public_key) != publisher_key_fingerprint:
            raise bad_request("publisher key fingerprint does not match the key")
    elif publisher_public_key or publisher_key_fingerprint:
        raise bad_request("only prebuilt sources take a publisher key")
    if build_mode == BuildMode.SANDBOX:
        if sandbox_ack is None:
            raise bad_request("sandbox sources require a trust acknowledgement")
        if not _SHA_RE.fullmatch(sandbox_ack.commit_sha):
            raise bad_request("sandbox acknowledgement commit_sha must be a 40-hex SHA")


_SHORT_INDEX_NAME = "uq_marketplace_sources_short"
_MAX_SHORT_RETRIES = 5


async def create_source(
    dal: AsyncDB,
    *,
    name: str,
    git_url: str,
    ref: str,
    build_mode: BuildMode,
    created_by: int | None = None,
    publisher_key_fingerprint: str | None = None,
    publisher_public_key: str | None = None,
    sandbox_ack: SandboxAck | None = None,
    pinned_commit_sha: str | None = None,
) -> SourceRecord:
    """Insert an external source in `PENDING_SCAN` and return it (public_id assigned by the DB).

    `pinned_commit_sha` is the operator-supplied commit the scanner will clone (the only thing
    ever cloned/built); `None` leaves the source un-scannable until a pin is set.
    """
    if pinned_commit_sha is not None and not _SHA_RE.fullmatch(pinned_commit_sha):
        raise bad_request("pinned_commit_sha must be a 40-hex SHA")
    _validate_create(
        name=name,
        git_url=git_url,
        ref=ref,
        build_mode=build_mode,
        publisher_key_fingerprint=publisher_key_fingerprint,
        publisher_public_key=publisher_public_key,
        sandbox_ack=sandbox_ack,
    )
    table = _table(dal)
    values: dict[str, Any] = {
        "name": name.strip(),
        "git_url": git_url,
        "ref": ref,
        "build_mode": int(build_mode),
        "status": int(SourceStatus.PENDING_SCAN),
        "created_by": created_by,
        "publisher_key_fingerprint": publisher_key_fingerprint,
        "publisher_public_key": publisher_public_key,
        "pinned_commit_sha": pinned_commit_sha,
    }
    if sandbox_ack is not None:
        values.update(
            sandbox_ack_by=sandbox_ack.by,
            sandbox_ack_at=sandbox_ack.at,
            sandbox_ack_text_hash=sandbox_ack.text_hash,
            sandbox_ack_commit_sha=sandbox_ack.commit_sha,
        )
    row = None
    for _ in range(_MAX_SHORT_RETRIES):
        # Server-generated public_id; the 12-hex namespace token must be unique (DB index
        # uq_marketplace_sources_short), so a collision just retries with a fresh uuid4.
        values["public_id"] = uuid.uuid4()
        try:
            async with dal.engine.begin() as conn:
                row = (await conn.execute(insert(table).values(**values).returning(*table.c))).one()
            break
        except IntegrityError as exc:
            if _SHORT_INDEX_NAME not in str(exc):
                raise conflict("marketplace source violates a constraint") from exc
    if row is None:
        raise conflict("could not allocate a unique source namespace token")
    record = _to_record(row)
    logger.info(
        "marketplace source: created",
        extra={"source_public_id": str(record.public_id), "build_mode": int(build_mode)},
    )
    return record


async def get_source(dal: AsyncDB, public_id: uuid.UUID) -> SourceRecord | None:
    """Return the source with `public_id`, or `None`."""
    table = _table(dal)
    async with dal.engine.connect() as conn:
        row = (await conn.execute(select(table).where(table.c.public_id == public_id))).first()
    return None if row is None else _to_record(row)


async def list_sources(dal: AsyncDB, *, include_disabled: bool = True) -> tuple[SourceRecord, ...]:
    """Return all sources, built-in first then by creation order."""
    table = _table(dal)
    query = select(table).order_by(table.c.is_builtin.desc(), table.c.id)
    if not include_disabled:
        query = query.where(table.c.status != int(SourceStatus.DISABLED))
    async with dal.engine.connect() as conn:
        rows = (await conn.execute(query)).all()
    return tuple(_to_record(r) for r in rows)


async def update_status(
    dal: AsyncDB,
    public_id: uuid.UUID,
    status: SourceStatus,
    *,
    last_scan_error: str | None = None,
    pending_commit_sha: str | None = None,
    scanned: bool = False,
) -> SourceRecord:
    """Set `status` (and scan bookkeeping); `scanned=True` stamps `last_scanned_at`.

    `last_scan_error` / `pending_commit_sha` are written as given (None clears them). This never
    touches `pinned_commit_sha` -- advancing the pin is a separate, approval-gated operation.
    """
    if pending_commit_sha is not None and not _SHA_RE.fullmatch(pending_commit_sha):
        raise bad_request("pending_commit_sha must be a 40-hex SHA")
    table = _table(dal)
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "status": int(status),
        "last_scan_error": last_scan_error,
        "pending_commit_sha": pending_commit_sha,
        "updated_at": now,
    }
    if scanned:
        values["last_scanned_at"] = now
    async with dal.engine.begin() as conn:
        row = (
            await conn.execute(
                update(table)
                .where(table.c.public_id == public_id)
                .values(**values)
                .returning(*table.c)
            )
        ).first()
    if row is None:
        raise not_found("marketplace source not found")
    logger.info(
        "marketplace source: status",
        extra={"source_public_id": str(public_id), "status": int(status)},
    )
    return _to_record(row)


async def disable_source(dal: AsyncDB, public_id: uuid.UUID) -> SourceRecord:
    """Mark a source `DISABLED` (versions are never deleted; deactivating its bundles is Task 6)."""
    return await update_status(dal, public_id, SourceStatus.DISABLED)


async def delete_source(dal: AsyncDB, public_id: uuid.UUID) -> None:
    """Hard-delete an external source. The built-in row is refused; referenced sources conflict."""
    existing = await get_source(dal, public_id)
    if existing is None:
        raise not_found("marketplace source not found")
    if existing.is_builtin:
        raise conflict("the built-in marketplace source cannot be deleted")
    table = _table(dal)
    try:
        async with dal.engine.begin() as conn:
            await conn.execute(delete(table).where(table.c.public_id == public_id))
    except IntegrityError as exc:
        raise conflict("marketplace source still has bundle versions; disable it instead") from exc
    logger.info("marketplace source: deleted", extra={"source_public_id": str(public_id)})


async def get_source_pk(dal: AsyncDB, public_id: uuid.UUID) -> int | None:
    """Return the internal numeric `marketplace_sources.id` (FK use only, never exposed)."""
    table = _table(dal)
    async with dal.engine.connect() as conn:
        row = (await conn.execute(select(table.c.id).where(table.c.public_id == public_id))).first()
    return None if row is None else int(row[0])


async def approve_update(dal: AsyncDB, public_id: uuid.UUID) -> SourceRecord:
    """Promote `pending_commit_sha` to `pinned_commit_sha` (human-approved update, design 4.5).

    One atomic UPDATE guarded on `pending_commit_sha IS NOT NULL`, so a concurrent approve or a
    missing pending SHA can never advance the pin to a stale/empty value. The built-in source is
    refused (its pin is the baked image commit). Status returns to `ACTIVE`.
    """
    existing = await get_source(dal, public_id)
    if existing is None:
        raise not_found("marketplace source not found")
    if existing.is_builtin:
        raise conflict("the built-in marketplace source has no approvable update")
    table = _table(dal)
    async with dal.engine.begin() as conn:
        row = (
            await conn.execute(
                update(table)
                .where(table.c.public_id == public_id, table.c.pending_commit_sha.is_not(None))
                .values(
                    pinned_commit_sha=table.c.pending_commit_sha,
                    pending_commit_sha=None,
                    status=int(SourceStatus.ACTIVE),
                    last_scan_error=None,
                    updated_at=datetime.now(UTC),
                )
                .returning(*table.c)
            )
        ).first()
    if row is None:
        raise conflict("marketplace source has no pending update to approve")
    logger.info("marketplace source: update approved", extra={"source_public_id": str(public_id)})
    return _to_record(row)
