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

Everything here is pure -- no DB, no I/O, no request context; a later task wires
the result into the write path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
