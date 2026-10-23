"""Slash-command registry: pure desired-row resolution from a bundle manifest.

Registry contract (`app_commands`, migration 0062):

* Scope sentinel: `community_id = 0` (`TENANT_WIDE_COMMUNITY_SENTINEL`) means a
  tenant-wide row; real communities start at 1, so 0 never collides.
* Ownership order (highest wins): an explicit `commands:` declaration in the
  bundle manifest replaces an inferred row of the same name; inferred rows come
  from the manifest's `consumes` rules.
* Inference rules: each `consumes` rule on a supported platform yields one
  STANDARD row per `filters.command_prefix` verb (one leading non-alphanumeric
  char stripped, remainder must match `COMMAND_NAME_RE`); a rule with no/empty
  `command_prefix` yields the `*` catch-all. Rows with the same command merge
  by sorted platform union. Unusable prefixes are skipped with a warning.

Desired-row resolution (`resolve_desired_commands`) is pure. The write path
(`sync_commands` / `clear_commands`) runs inside the caller's transaction against the
reflected `app_commands` table (SQLAlchemy Core, parameter-bound); `command_owner` is a
read helper. Ownership: earliest `(registered_at, id)` among overlapping non-wildcard
rows wins; `*` rows never conflict. No request context anywhere in this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB
from sqlalchemy import delete, insert, select, update

from services.bundle_manifest_v2 import (
    COMMAND_NAME_RE,
    COMMAND_WILDCARD,
    BundleManifestV2,
    CommandType,
)

logger = logging.getLogger(__name__)

#: Mirrors `app_source_binding_service.TENANT_WIDE_COMMUNITY_SENTINEL` -- kept as this
#: module's own constant (not imported) to avoid a circular import with the services
#: that will import this module.
TENANT_WIDE_COMMUNITY_SENTINEL = 0


@dataclass(slots=True, frozen=True)
class DesiredCommand:
    """One `app_commands` row a bundle wants registered (command, types bitmask, platforms)."""

    command: str
    types: CommandType
    platforms: tuple[str, ...]
    description: str | None


def verb_from_prefix(prefix: str) -> str | None:
    """Strip exactly one leading non-alphanumeric char; return the rest if a valid command name."""
    if len(prefix) < 2 or prefix[0].isalnum():
        return None
    verb = prefix[1:]
    return verb if COMMAND_NAME_RE.fullmatch(verb) else None


def infer_commands(manifest: BundleManifestV2) -> tuple[DesiredCommand, ...]:
    """Infer STANDARD rows from the manifest's `consumes` rules on supported platforms.

    Same-command rows merge by sorted platform union; output is sorted by command.
    """
    platforms_by_command: dict[str, set[str]] = {}
    unusable = 0
    for rule in manifest.consumes:
        if not manifest.supports_platform(rule.platform):
            continue
        raw_prefixes = rule.filters.get("command_prefix")
        if not raw_prefixes:
            platforms_by_command.setdefault(COMMAND_WILDCARD, set()).add(rule.platform)
            continue
        for prefix in raw_prefixes:
            verb = verb_from_prefix(prefix) if isinstance(prefix, str) else None
            if verb is None:
                unusable += 1
                continue
            platforms_by_command.setdefault(verb, set()).add(rule.platform)
    if unusable:
        logger.warning(
            "app command inference: unusable command_prefix",
            extra={"app_id": manifest.app_id, "op": "infer_commands", "count": unusable},
        )
    return tuple(
        DesiredCommand(command, CommandType.STANDARD, tuple(sorted(platforms)), None)
        for command, platforms in sorted(platforms_by_command.items())
    )


def _default_platforms(manifest: BundleManifestV2) -> tuple[str, ...]:
    """Platforms for an explicit declaration with `platforms=None`."""
    consumed = {r.platform for r in manifest.consumes if manifest.supports_platform(r.platform)}
    if consumed:
        return tuple(sorted(consumed))
    if manifest.supported_platforms:
        return tuple(sorted(manifest.supported_platforms))
    return (COMMAND_WILDCARD,)


def resolve_desired_commands(manifest: BundleManifestV2) -> tuple[DesiredCommand, ...]:
    """Merge inferred rows with explicit `commands:` declarations (explicit wins by name)."""
    by_name = {row.command: row for row in infer_commands(manifest)}
    default_platforms = _default_platforms(manifest)
    for decl in manifest.command_declarations:
        platforms = default_platforms if decl.platforms is None else tuple(sorted(decl.platforms))
        by_name[decl.name] = DesiredCommand(decl.name, decl.types, platforms, decl.description)
    return tuple(by_name[name] for name in sorted(by_name))


WARNING_COMMAND_CONFLICT = "command_conflict"
WARNING_NO_ROUTABLE_COMMAND = "no_routable_command"

_CONFLICT_MESSAGE = "command %s is already registered by %s; this app will not receive it"
_NO_ROUTABLE_MESSAGE = "app has a process stage but registers no routable command"


@dataclass(slots=True, frozen=True)
class ActivationWarning:
    """A non-fatal activation-time finding surfaced to the operator (never blocks activation)."""

    code: str
    app_id: str
    command: str | None
    owner_app_id: str | None
    message: str


def _platforms_overlap(left: list[str], right: list[str]) -> bool:
    """True when two platform lists share a platform or either contains the `*` wildcard."""
    if COMMAND_WILDCARD in left or COMMAND_WILDCARD in right:
        return True
    return bool(set(left) & set(right))


def _scope(community_id: int | None) -> int:
    """Stored scope for a community: the tenant-wide sentinel when `None`."""
    return TENANT_WIDE_COMMUNITY_SENTINEL if community_id is None else community_id


async def sync_commands(
    conn: Any,
    *,
    tenant_id: int,
    community_id: int | None,
    app_id: str,
    manifest: BundleManifestV2,
    commands_table: Any,
    now: datetime | None = None,
) -> tuple[ActivationWarning, ...]:
    """Replace `app_id`'s registry rows for its scope and return activation warnings.

    Runs inside the caller's transaction. Rows no longer desired are deleted, changed rows
    are updated in place (keeping `registered_at` so a re-sync never loses queue position),
    new rows are inserted with `registered_at=now`. Commands come only from
    `resolve_desired_commands`, whose names are `COMMAND_NAME_RE`-capped (<=32), so no
    unvalidated string reaches an insert.
    """
    scope = _scope(community_id)
    stamp = now or datetime.now(UTC)
    desired = {row.command: row for row in resolve_desired_commands(manifest)}
    c = commands_table.c

    own_rows = (
        await conn.execute(
            select(c.id, c.command, c.command_types, c.platforms, c.description).where(
                (c.tenant_id == tenant_id) & (c.community_id == scope) & (c.app_id == app_id)
            )
        )
    ).all()
    existing = {r.command: r for r in own_rows}

    removed = [name for name in existing if name not in desired]
    if removed:
        await conn.execute(
            delete(commands_table).where(
                (c.tenant_id == tenant_id)
                & (c.community_id == scope)
                & (c.app_id == app_id)
                & c.command.in_(removed)
            )
        )
    for name, row in desired.items():
        platforms = sorted(row.platforms)
        current = existing.get(name)
        if current is None:
            await conn.execute(
                insert(commands_table).values(
                    tenant_id=tenant_id,
                    community_id=scope,
                    app_id=app_id,
                    command=name,
                    command_types=int(row.types),
                    platforms=platforms,
                    description=row.description,
                    registered_at=stamp,
                )
            )
        elif (
            current.command_types != int(row.types)
            or sorted(current.platforms) != platforms
            or current.description != row.description
        ):
            await conn.execute(
                update(commands_table)
                .where(c.id == current.id)
                .values(
                    command_types=int(row.types),
                    platforms=platforms,
                    description=row.description,
                )
            )

    warnings: list[ActivationWarning] = []
    if "process" in manifest.stages and not desired:
        warnings.append(
            ActivationWarning(
                code=WARNING_NO_ROUTABLE_COMMAND,
                app_id=app_id,
                command=None,
                owner_app_id=None,
                message=_NO_ROUTABLE_MESSAGE,
            )
        )
    elif desired:
        warnings.extend(
            await _conflict_warnings(
                conn,
                tenant_id=tenant_id,
                scope=scope,
                app_id=app_id,
                desired_names=[n for n in sorted(desired) if n != COMMAND_WILDCARD],
                commands_table=commands_table,
            )
        )

    logger.info(
        "app commands: synced",
        extra={
            "app_id": app_id,
            "tenant_id": tenant_id,
            "community_id": scope,
            "rows": len(desired),
            "warnings": len(warnings),
        },
    )
    return tuple(warnings)


async def _conflict_warnings(
    conn: Any,
    *,
    tenant_id: int,
    scope: int,
    app_id: str,
    desired_names: list[str],
    commands_table: Any,
) -> list[ActivationWarning]:
    """Return one `command_conflict` per desired command owned (earliest) by another app."""
    if not desired_names:
        return []
    c = commands_table.c
    rows = (
        await conn.execute(
            select(c.id, c.app_id, c.community_id, c.command, c.platforms, c.registered_at).where(
                (c.tenant_id == tenant_id) & c.command.in_(desired_names)
            )
        )
    ).all()
    warnings: list[ActivationWarning] = []
    for name in desired_names:
        same = [r for r in rows if r.command == name]
        own = next(r for r in same if r.app_id == app_id and r.community_id == scope)
        candidates = [own]
        for other in same:
            if other.app_id == app_id:
                continue
            if scope != TENANT_WIDE_COMMUNITY_SENTINEL and other.community_id not in (
                scope,
                TENANT_WIDE_COMMUNITY_SENTINEL,
            ):
                continue
            if _platforms_overlap(list(own.platforms), list(other.platforms)):
                candidates.append(other)
        owner = min(candidates, key=lambda r: (r.registered_at, r.id))
        if owner.app_id != app_id:
            warnings.append(
                ActivationWarning(
                    code=WARNING_COMMAND_CONFLICT,
                    app_id=app_id,
                    command=name,
                    owner_app_id=owner.app_id,
                    message=_CONFLICT_MESSAGE % (name, owner.app_id),
                )
            )
    return warnings


async def clear_commands(
    conn: Any,
    *,
    tenant_id: int,
    community_id: int | None,
    app_id: str,
    commands_table: Any,
) -> None:
    """Delete `app_id`'s registry rows in its scope (`None` -> tenant-wide), in the caller's tx."""
    c = commands_table.c
    await conn.execute(
        delete(commands_table).where(
            (c.tenant_id == tenant_id)
            & (c.community_id == _scope(community_id))
            & (c.app_id == app_id)
        )
    )


async def command_owner(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    command: str,
    platform: str | None = None,
) -> str | None:
    """Return the app owning `command` in a community (first-registered non-wildcard), else None.

    Visible scope is `community_id` plus the tenant-wide sentinel; `platform=None` matches any
    platform. Read helper for tests and Phase 2 -- not used on any hot path.
    """
    table = install_dal.metadata.tables["app_commands"]
    c = table.c
    async with install_dal.engine.connect() as conn:
        rows = (
            await conn.execute(
                select(c.app_id, c.platforms)
                .where(
                    (c.tenant_id == tenant_id)
                    & c.community_id.in_((community_id, TENANT_WIDE_COMMUNITY_SENTINEL))
                    & (c.command == command)
                )
                .order_by(c.registered_at, c.id)
            )
        ).all()
    for row in rows:
        if platform is None or _platforms_overlap(list(row.platforms), [platform]):
            return str(row.app_id)
    return None
