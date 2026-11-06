"""Explicit wire DTOs for the dangerous access-grant preview (never a raw dict/model)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class TableAccessDTO:
    """One requested platform-table access block (table/columns/ops/reason) -- explicit shape."""

    table: str
    columns: list[str]
    ops: list[str]
    reason: str


@dataclass(slots=True, frozen=True)
class AccessGrantsResponse:
    """Response DTO: the dangerous access grants a version requests and their approval state.

    `requestedIds` is every `platform.table.*` (etc.) permission id the manifest
    declares; `approvedIds` the globally-approved subset (anything requested but not
    approved is unusable until a global admin re-approves it). A new grant kind adds
    its own list field here (e.g. `storageAccess`).
    """

    success: bool
    requestedIds: list[str]
    approvedIds: list[str]
    dangerousIds: list[str]
    tableAccess: list[TableAccessDTO] = field(default_factory=list)


def access_grants_response(
    blocks: dict[str, list[dict[str, Any]]],
    requested: frozenset[str],
    approved: frozenset[str],
    dangerous: frozenset[str],
) -> AccessGrantsResponse:
    """Map the service-layer summary blocks onto the explicit wire DTO (never a raw dict)."""
    return AccessGrantsResponse(
        success=True,
        requestedIds=sorted(requested),
        approvedIds=sorted(approved),
        dangerousIds=sorted(dangerous),
        tableAccess=[
            TableAccessDTO(
                table=e["table"], columns=list(e["columns"]), ops=list(e["ops"]), reason=e["reason"]
            )
            for e in blocks.get("tableAccess", [])
        ],
    )
