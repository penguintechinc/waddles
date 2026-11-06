"""Generic registry of DANGEROUS, never-auto-granted bundle resource-access grants.

Spec `docs/superpowers/specs/2026-11-06-bundle-table-access-grants.md` Sec7. A
"grant kind" is a manifest block (`table_access`, `storage_access`, ...) that
expands into dangerous permission ids sharing one id prefix. Everything
downstream is kind-agnostic and keyed off this registry:

* never auto-granted (core seeder carve-out) -- `is_access_grant_id`
* direct declaration in `permissions:` rejected -- `is_access_grant_id`
* consent summary block (hashed -> widening forces re-approval) --
  `summarize_access_grants`
* widening diff atoms -- `access_grant_diff_atoms`
* webui renders each `summary[kind.summary_key]` entry in the bold-red warning.

A new kind calls `register_kind(AccessGrantKind(...))` at import time; no other
hub-api code changes. The kind's own module must NOT import this one at module
level in a cycle (this module imports nothing from services).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class AccessGrantKind:
    """One grant kind.

    `kind` is the stable name (`table`, `storage_prefix`); `id_prefix` the
    permission-id prefix (`platform.table.`); `summary_key` the consent-summary /
    API key (`tableAccess`); `requests_of(manifest)` the validated requests the
    manifest carries; `summarize(requests)` their JSON-safe summary entries
    (each MUST carry a `reason`); `atoms(entry)` the diff atoms of one summary
    entry (a widened set -> `classify_diff` == `widened`).
    """

    kind: str
    id_prefix: str
    summary_key: str
    requests_of: Callable[[Any], tuple[Any, ...]]
    summarize: Callable[[tuple[Any, ...]], list[dict[str, Any]]]
    atoms: Callable[[dict[str, Any]], set[str]]


_KINDS: dict[str, AccessGrantKind] = {}


def register_kind(kind: AccessGrantKind) -> None:
    """Register `kind`; a duplicate kind name or id prefix is a programming error."""
    if kind.kind in _KINDS or any(k.id_prefix == kind.id_prefix for k in _KINDS.values()):
        raise ValueError(f"access grant kind already registered: {kind.kind!r}")
    _KINDS[kind.kind] = kind


def kinds() -> tuple[AccessGrantKind, ...]:
    """Every registered kind."""
    return tuple(_KINDS.values())


def is_access_grant_id(permission_id: str) -> bool:
    """`True` for any permission id in a registered grant-kind family (never auto-granted)."""
    return any(permission_id.startswith(k.id_prefix) for k in _KINDS.values())


def summarize_access_grants(manifest: Any) -> dict[str, list[dict[str, Any]]]:
    """Consent-summary blocks for every kind the manifest uses (absent kinds omitted)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for k in _KINDS.values():
        requests = k.requests_of(manifest)
        if requests:
            out[k.summary_key] = k.summarize(requests)
    return out


def access_grant_diff_atoms(summary: dict[str, Any]) -> set[str]:
    """Diff atoms of every access-grant block in a stored/new consent summary."""
    atoms: set[str] = set()
    for k in _KINDS.values():
        for entry in summary.get(k.summary_key, []):
            atoms |= k.atoms(entry)
    return atoms
