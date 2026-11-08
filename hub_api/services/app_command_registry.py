"""Slash-command desired state + per-user active context (hub-api side of the slash design).

Spec: ``docs/superpowers/specs/2026-10-23-slash-command-architecture-design.md`` sections 2, 4.

* :func:`desired_commands` -- what a platform source (a Discord guild) should have registered: the
  union of the ``webhook``-type, platform-matching, non-wildcard ``app_commands`` rows of every
  community paired to the source (plus the tenant-wide sentinel), plus the built-in ``context``
  command. Carries a deterministic ``version`` (sha256 over exactly the registered payload, i.e.
  name + description) so a connector's bulk overwrite is idempotent: same version => nothing to do.
  ``app_commands`` is maintained on activate/deactivate, so only installed + enabled bundles
  are in it.
* :func:`list_sources` -- paginated list of sources that have a paired community (the reconcile
  work-list).
* :func:`get_active_context` / :func:`set_active_context` / :func:`clear_active_context` -- the
  per-``(user token, platform, source)`` community choice; default = first-registered community.

Pairing mirrors ``services.dataplane_scope`` (approved ``community_servers`` links beat
self-asserted ``communities.platform_server_id``) but returns *all* communities, not one. A source
paired across tenants fails closed (:class:`AmbiguousSourceError`). Only public_id UUIDs and
tokenized user UUIDs cross the boundary (numeric PKs stay inside hub-api) -- no PII, no raw
handle logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB
from sqlalchemy import and_, delete, func, insert, select, union, update
from sqlalchemy.exc import IntegrityError

from services.dataplane_scope import (
    AmbiguousScopeError,
    InvalidSourceError,
    ScopeNotFoundError,
    normalize,
)

logger = logging.getLogger(__name__)

#: ``command_types`` bitmask bit for slash (``webhook``) commands (migration 0062).
WEBHOOK_BIT = 2
#: Discord caps guild commands at 100; excess is dropped (oldest-registered kept) and flagged.
MAX_COMMANDS = 100
#: Page-size bounds for :func:`list_sources`.
MAX_PAGE = 500
#: The reserved built-in command (spec section 2); an app declaring the same name is ignored.
BUILTIN_CONTEXT = "context"
BUILTIN_CONTEXT_DESCRIPTION = "Show or switch your active community"
_TENANT_WIDE = 0
_DEFAULT_DESCRIPTION = "Run the {name} command"


class AmbiguousSourceError(AmbiguousScopeError):
    """The source is paired across tenants (409) -- never adjudicated, always refused."""


class ContextNotFoundError(Exception):
    """The requested community is not paired to the source (404)."""


@dataclass(slots=True, frozen=True)
class PairedCommunity:
    """One community paired to a source, in registration order (``position`` 0 = first)."""

    community_id: int  # internal numeric PK -- never leaves hub-api
    public_id: str  # the only cross-boundary reference
    name: str  # display alias only, never a key
    position: int


@dataclass(slots=True, frozen=True)
class PairedSource:
    """All communities paired to a source; all share ``tenant_id``."""

    tenant_id: int  # internal numeric PK -- never leaves hub-api
    tenant_public_id: str
    platform: str
    source_id: str
    communities: tuple[PairedCommunity, ...]


@dataclass(slots=True, frozen=True)
class DesiredCommand:
    """One command to register: ``name``/``description`` are the registered payload."""

    name: str
    description: str
    builtin: bool
    community_public_ids: tuple[str, ...]  # empty = tenant-wide (applies to every community)
    app_public_ids: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DesiredState:
    """Desired registration for one source, with the idempotency ``version``."""

    version: str
    tenant_public_id: str
    platform: str
    source_id: str
    commands: tuple[DesiredCommand, ...]
    truncated: bool


@dataclass(slots=True, frozen=True)
class SourcePage:
    """One page of reconcile-eligible source ids."""

    items: tuple[str, ...]
    total: int
    limit: int
    offset: int


@dataclass(slots=True, frozen=True)
class ActiveContext:
    """A user's active community for a source; ``explicit`` False = the first-registered default."""

    tenant_public_id: str
    community_public_id: str
    community_name: str
    explicit: bool
    communities: tuple[PairedCommunity, ...]


def _truthy_active(value: Any) -> bool:
    """``is_active`` defaults true; only an explicit false disables the row."""
    return value is not False


async def paired_source(install_dal: AsyncDB, platform: str, source_id: str) -> PairedSource:
    """Return every active community paired to the source, or raise a scope error.

    Raises :class:`InvalidSourceError`, :class:`ScopeNotFoundError`, :class:`AmbiguousSourceError`.
    """
    plat, src = normalize(platform, source_id)
    meta = install_dal.metadata.tables
    servers, communities, tenants = meta["community_servers"], meta["communities"], meta["tenants"]
    async with install_dal.engine.connect() as conn:
        verified = (
            await conn.execute(
                select(servers.c.community_id, servers.c.id)
                .where(
                    (servers.c.platform == plat)
                    & (servers.c.platform_server_id == src)
                    & (servers.c.status == "approved")
                )
                .order_by(servers.c.id)
            )
        ).all()
        legacy = (
            await conn.execute(
                select(communities.c.id)
                .where((communities.c.platform == plat) & (communities.c.platform_server_id == src))
                .order_by(communities.c.id)
            )
        ).all()
        ordered: list[int] = []
        for row in verified:
            if int(row.community_id) not in ordered:
                ordered.append(int(row.community_id))
        if not ordered:  # a verified link beats a self-asserted one; legacy only as sole evidence
            ordered = [int(r.id) for r in legacy]
        if not ordered:
            raise ScopeNotFoundError("no community is linked to this source")
        rows = (
            await conn.execute(
                select(
                    communities.c.id,
                    communities.c.public_id,
                    communities.c.name,
                    communities.c.tenant_id,
                    communities.c.is_active,
                    tenants.c.public_id.label("tenant_public_id"),
                    tenants.c.is_active.label("tenant_active"),
                )
                .select_from(
                    communities.outerjoin(tenants, tenants.c.id == communities.c.tenant_id)
                )
                .where(communities.c.id.in_(ordered))
            )
        ).all()
    usable = {
        int(r.id): r
        for r in rows
        if r.tenant_id is not None
        and _truthy_active(r.is_active)
        and _truthy_active(r.tenant_active)
        and r.tenant_active is not None
    }
    chosen = [cid for cid in ordered if cid in usable]
    if not chosen:
        raise ScopeNotFoundError("no active community is linked to this source")
    if len({int(usable[cid].tenant_id) for cid in chosen}) > 1:
        logger.warning("app_command_registry.cross_tenant platform=%s n=%d", plat, len(chosen))
        raise AmbiguousSourceError("source is paired across tenants")
    return PairedSource(
        tenant_id=int(usable[chosen[0]].tenant_id),
        tenant_public_id=str(usable[chosen[0]].tenant_public_id),
        platform=plat,
        source_id=src,
        communities=tuple(
            PairedCommunity(cid, str(usable[cid].public_id), str(usable[cid].name), pos)
            for pos, cid in enumerate(chosen)
        ),
    )


def _platform_matches(platforms: list[str], platform: str) -> bool:
    """True when the row is offered on ``platform`` (or on the ``*`` wildcard)."""
    return platform in platforms or "*" in platforms


def _version(commands: list[tuple[str, str]]) -> str:
    """Deterministic version over exactly what a connector would register (name + description)."""
    canonical = json.dumps(sorted(commands), separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def desired_commands(install_dal: AsyncDB, platform: str, source_id: str) -> DesiredState:
    """Compute the desired slash registration for one source (see module docstring)."""
    pair = await paired_source(install_dal, platform, source_id)
    ids = [c.community_id for c in pair.communities]
    table = install_dal.metadata.tables["app_commands"]
    c = table.c
    async with install_dal.engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    c.community_id,
                    c.app_id,
                    c.command,
                    c.command_types,
                    c.platforms,
                    c.description,
                    c.registered_at,
                    c.id,
                )
                .where(
                    (c.tenant_id == pair.tenant_id)
                    & c.community_id.in_([*ids, _TENANT_WIDE])
                    & (c.command != "*")
                )
                .order_by(c.registered_at, c.id)
            )
        ).all()
        catalog = install_dal.metadata.tables["app_catalog"]
        app_rows = (
            await conn.execute(
                select(catalog.c.app_id, catalog.c.public_id).where(
                    catalog.c.app_id.in_({str(r.app_id) for r in rows} or {""})
                )
            )
        ).all()
    app_public = {str(r.app_id): str(r.public_id) for r in app_rows}
    by_id = {c.community_id: c.public_id for c in pair.communities}
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        if not int(row.command_types) & WEBHOOK_BIT:
            continue
        if not _platform_matches(list(row.platforms), pair.platform):
            continue
        if row.command == BUILTIN_CONTEXT:
            logger.warning("app_command_registry.reserved_command_ignored app_id=%s", row.app_id)
            continue
        grouped.setdefault(str(row.command), []).append(row)
    commands: list[DesiredCommand] = [
        DesiredCommand(
            name=BUILTIN_CONTEXT,
            description=BUILTIN_CONTEXT_DESCRIPTION,
            builtin=True,
            community_public_ids=tuple(c.public_id for c in pair.communities),
            app_public_ids=(),
        )
    ]
    # Oldest-registered command first; first row of a group (owner) supplies the description.
    for name, group in sorted(
        grouped.items(), key=lambda kv: (kv[1][0].registered_at, kv[1][0].id)
    ):
        owner = group[0]
        apps = tuple(dict.fromkeys(app_public[str(r.app_id)] for r in group))
        commands.append(
            DesiredCommand(
                name=name,
                description=str(owner.description or _DEFAULT_DESCRIPTION.format(name=name)),
                builtin=False,
                community_public_ids=tuple(
                    dict.fromkeys(
                        by_id[int(r.community_id)] for r in group if int(r.community_id) in by_id
                    )
                ),
                app_public_ids=apps,
            )
        )
    truncated = len(commands) > MAX_COMMANDS
    if truncated:
        logger.warning(
            "app_command_registry.truncated platform=%s total=%d cap=%d",
            pair.platform,
            len(commands),
            MAX_COMMANDS,
        )
        commands = commands[:MAX_COMMANDS]
    return DesiredState(
        version=_version([(cmd.name, cmd.description) for cmd in commands]),
        tenant_public_id=pair.tenant_public_id,
        platform=pair.platform,
        source_id=pair.source_id,
        commands=tuple(commands),
        truncated=truncated,
    )


async def list_sources(
    install_dal: AsyncDB, platform: str, *, limit: int, offset: int
) -> SourcePage:
    """Page the sources of ``platform`` that have a paired community (stable, sorted by id)."""
    plat, _ = normalize(platform, "x")
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise InvalidSourceError("limit/offset out of range")
    meta = install_dal.metadata.tables
    servers, communities = meta["community_servers"], meta["communities"]
    linked = select(servers.c.platform_server_id.label("sid")).where(
        (servers.c.platform == plat) & (servers.c.status == "approved")
    )
    legacy = select(communities.c.platform_server_id.label("sid")).where(
        (communities.c.platform == plat) & communities.c.platform_server_id.is_not(None)
    )
    merged = union(linked, legacy).subquery()
    async with install_dal.engine.connect() as conn:
        total = int((await conn.execute(select(func.count()).select_from(merged))).scalar_one())
        page = (
            await conn.execute(
                select(merged.c.sid).order_by(merged.c.sid).limit(limit).offset(offset)
            )
        ).all()
    return SourcePage(tuple(str(r.sid) for r in page), total, limit, offset)


def _active_table(install_dal: AsyncDB) -> Any:
    """The reflected ``user_active_context`` table."""
    return install_dal.metadata.tables["user_active_context"]


def _parse_user(user: str) -> str:
    """Return the canonical (hyphenated, lowercase) tokenized user UUID, else invalid input."""
    try:
        return str(uuid.UUID(user))
    except (ValueError, AttributeError, TypeError):
        raise InvalidSourceError("user is not a valid UUID") from None


async def get_active_context(
    install_dal: AsyncDB, platform: str, source_id: str, user: str
) -> ActiveContext:
    """Return the user's active community; a missing/stale choice falls back to the default."""
    pair = await paired_source(install_dal, platform, source_id)
    token = _parse_user(user)
    t = _active_table(install_dal)
    async with install_dal.engine.connect() as conn:
        row = (
            await conn.execute(
                select(t.c.community_id).where(
                    (t.c.user_uuid == token)
                    & (t.c.platform == pair.platform)
                    & (t.c.source_id == pair.source_id)
                )
            )
        ).first()
    chosen = pair.communities[0]
    explicit = False
    if row is not None:
        match = next((c for c in pair.communities if c.community_id == int(row.community_id)), None)
        if match is not None:
            chosen, explicit = match, True
        else:
            logger.debug("app_command_registry.stale_context platform=%s", pair.platform)
    return ActiveContext(
        pair.tenant_public_id, chosen.public_id, chosen.name, explicit, pair.communities
    )


def _select_community(pair: PairedSource, selector: str) -> PairedCommunity:
    """Resolve a community public_id UUID among the paired communities (names never resolve)."""
    try:
        wanted = str(uuid.UUID(selector))
    except (ValueError, AttributeError, TypeError):
        raise InvalidSourceError("community is not a valid UUID") from None
    found = next((c for c in pair.communities if c.public_id == wanted), None)
    if found is None:
        raise ContextNotFoundError("community is not paired to this source")
    return found


async def set_active_context(
    install_dal: AsyncDB, platform: str, source_id: str, user: str, selector: str
) -> ActiveContext:
    """Upsert the user's active community (a public_id UUID among the source's pairings)."""
    pair = await paired_source(install_dal, platform, source_id)
    token = _parse_user(user)
    target = _select_community(pair, selector)
    t = _active_table(install_dal)
    where = (
        (t.c.user_uuid == token)
        & (t.c.platform == pair.platform)
        & (t.c.source_id == pair.source_id)
    )
    values = {
        "tenant_id": pair.tenant_id,
        "community_id": target.community_id,
        "updated_at": datetime.now(UTC),
    }
    for attempt in (1, 2):
        try:
            async with install_dal.engine.begin() as conn:
                result = await conn.execute(update(t).where(where).values(**values))
                if result.rowcount == 0:
                    await conn.execute(
                        insert(t).values(
                            user_uuid=token,
                            platform=pair.platform,
                            source_id=pair.source_id,
                            **values,
                        )
                    )
            break
        except IntegrityError:
            if attempt == 2:  # lost the insert race twice: a real conflict, never swallow
                raise
    logger.info(
        "app_command_registry.context_set tenant=%s community=%s platform=%s",
        pair.tenant_public_id,
        target.public_id,
        pair.platform,
    )
    return ActiveContext(
        pair.tenant_public_id, target.public_id, target.name, True, pair.communities
    )


async def clear_active_context(
    install_dal: AsyncDB, platform: str, source_id: str, user: str
) -> bool:
    """Delete the user's explicit choice (back to the default); True when a row existed."""
    plat, src = normalize(platform, source_id)
    token = _parse_user(user)
    t = _active_table(install_dal)
    async with install_dal.engine.begin() as conn:
        result = await conn.execute(
            delete(t).where(
                and_(t.c.user_uuid == token, t.c.platform == plat, t.c.source_id == src)
            )
        )
    return bool(result.rowcount)
