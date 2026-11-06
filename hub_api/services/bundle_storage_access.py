"""Cross-prefix object-storage grants: manifest `storage_access` -> permission ids.

Spec `docs/superpowers/specs/2026-11-06-bundle-object-storage.md`. A bundle owns its
own object-storage prefix automatically; it may REQUEST access to ANOTHER bundle's
prefix. Each `(op, prefix_owner)` is one `dangerous` permission id
`storage.<op>:<prefix_owner>` (never auto-granted) so it rides the existing approval /
re-consent / revocation machinery unchanged. Even once granted, the host only ever
exposes objects tagged `app-bundle:<requester>` (host-side invariant).

Leaf module (no service imports). Mirrored in
`core/bundle_capability_gate/src/storage_access.rs` (owner charset + ops).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from services.bundle_access_grants import AccessGrantKind, register_kind

STORAGE_OPS = ("delete", "list", "read", "write")
MAX_REASON_LEN = 280

_OWNER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
_ID_RE = re.compile(r"storage\.(?P<op>read|list|write|delete):(?P<owner>.+)")
_PREFIXES = tuple(f"storage.{op}:" for op in STORAGE_OPS)


class StorageAccessError(ValueError):
    """A `storage_access` entry failed validation; `reason` is a stable machine code."""

    def __init__(self, reason: str, detail: str) -> None:
        """Keep the machine `reason` code and human `detail`."""
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(slots=True, frozen=True)
class StorageAccessRequest:
    """One validated manifest `storage_access` entry (consent-screen shape)."""

    prefix_owner: str
    ops: tuple[str, ...]
    reason: str


def is_valid_owner(owner: str) -> bool:
    """`True` for a well-formed bundle app_id usable as a prefix owner."""
    return _OWNER_RE.fullmatch(owner) is not None and ".." not in owner


def is_storage_access_id(permission_id: str) -> bool:
    """`True` for ANY `storage.<op>:` id (not `storage.kv` / `storage.tables`)."""
    return permission_id.startswith(_PREFIXES)


def parse_storage_access_id(permission_id: str) -> tuple[str, str] | None:
    """`(op, owner)` for a well-formed storage-access id, else `None`."""
    match = _ID_RE.fullmatch(permission_id)
    if match is None or not is_valid_owner(match.group("owner")):
        return None
    return match.group("op"), match.group("owner")


def storage_access_permission_id(op: str, owner: str) -> str:
    """Canonical permission id for one `(op, owner)`."""
    return f"storage.{op}:{owner}"


def parse_storage_access(
    raw_entries: object, *, own_app_id: str = ""
) -> tuple[StorageAccessRequest, ...]:
    """Validate the manifest `storage_access:` list; `StorageAccessError` on any violation."""
    if raw_entries is None:
        return ()
    if not isinstance(raw_entries, list):
        raise StorageAccessError("invalid_storage_access", "storage_access must be a list")
    out: list[StorageAccessRequest] = []
    seen: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise StorageAccessError("invalid_storage_access", "each entry must be a mapping")
        owner, ops, reason = entry.get("prefix_owner"), entry.get("ops"), entry.get("reason")
        if not isinstance(owner, str) or not is_valid_owner(owner):
            raise StorageAccessError("invalid_storage_access", f"bad prefix_owner {owner!r}")
        if owner == own_app_id:
            raise StorageAccessError(
                "invalid_storage_access", "a bundle already owns its own prefix; do not request it"
            )
        if owner in seen:
            raise StorageAccessError("invalid_storage_access", f"duplicate entry for {owner!r}")
        seen.add(owner)
        if not isinstance(ops, list) or not ops or not set(ops) <= set(STORAGE_OPS):
            raise StorageAccessError(
                "invalid_storage_access", f"{owner}: ops must be a subset of {STORAGE_OPS}"
            )
        if not isinstance(reason, str) or not (0 < len(reason.strip()) <= MAX_REASON_LEN):
            raise StorageAccessError(
                "missing_justification", f"{owner}: reason is mandatory (1-{MAX_REASON_LEN} chars)"
            )
        out.append(
            StorageAccessRequest(
                prefix_owner=owner, ops=tuple(sorted(set(ops))), reason=reason.strip()
            )
        )
    return tuple(out)


def storage_access_permission_ids(requests: tuple[StorageAccessRequest, ...]) -> tuple[str, ...]:
    """Every permission id a validated `storage_access` block expands to, sorted, unique."""
    return tuple(
        sorted({storage_access_permission_id(op, r.prefix_owner) for r in requests for op in r.ops})
    )


def _summarize(requests: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Consent-summary entries (prefixOwner/ops/reason), sorted by owner."""
    return sorted(
        ({"prefixOwner": r.prefix_owner, "ops": list(r.ops), "reason": r.reason} for r in requests),
        key=lambda e: e["prefixOwner"],
    )


def _atoms(entry: dict[str, Any]) -> set[str]:
    """One diff atom per (op, owner) of a summary entry."""
    return {f"storageaccess:{op}:{entry['prefixOwner']}" for op in entry["ops"]}


register_kind(
    AccessGrantKind(
        kind="storage_prefix",
        id_prefix=_PREFIXES,
        summary_key="storageAccess",
        requests_of=lambda manifest: manifest.storage_access,
        summarize=_summarize,
        atoms=_atoms,
    )
)
