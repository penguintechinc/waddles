"""Allowlisted platform-table access over the WIT ``platform-table`` import.

``stage-next``/``stage-v2``/``stage-tools`` only (spec
``docs/superpowers/specs/2026-11-06-bundle-table-access-grants.md``), for APP
bundles that declare ``table_access`` in their manifest (platform SERVICE bundles
expose tools instead). Each column needs its own approved, never-auto-granted
``platform.table.<read|write>:<table>.<column>`` grant::

    row = await platform_table.read("community_members", user, ["role"])
    await platform_table.write("community_members", user, {"role": "vip"})

The guest names only the table, the target ``user`` community UUID (from
``waddle_sdk.identity``) and the columns/values. The tenant/community scope is
ALWAYS host-injected: a row of another community or tenant is
:class:`NotAMemberError`, indistinguishable from an absent one. Values are text
(booleans come back as ``"true"``/``"false"``). Every refusal raises one of this
module's exceptions; nothing is ever defaulted.

**Eager import (componentize-py):** the ``from wit_world.imports import
platform_table`` below forces the binding to be wizened (see ``identity.py``);
try/except-guarded for host-side tests and ``stage`` 1.0.0 builds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

try:  # eager: forces componentize-py to wizen the binding -- see module docstring
    from wit_world.imports import platform_table as _platform_table_binding  # noqa: F401
except (ImportError, AttributeError):  # host-side tests / stage-1.0.0 world: no such binding
    _platform_table_binding = None

#: Mirrors ``core/bundle_executor/src/host/stage_next_platform_table.rs``.
MAX_NAME_LEN = 64
MAX_COLUMNS = 8


class PlatformTableError(Exception):
    """Base for every error this facade raises."""


class InvalidArgError(PlatformTableError, ValueError):
    """An argument is malformed (raised before any host call, or by the host as ``invalid``)."""


class DeniedError(PlatformTableError):
    """The host gate denied the call; ``code`` is the gate's stable denial code.

    e.g. ``not_granted`` (no approved grant for a named column/op), ``rate_limited``,
    ``instance_denied``.
    """

    def __init__(self, message: str, code: str) -> None:
        """Store the gate's stable denial ``code`` alongside the message."""
        super().__init__(message)
        self.code = code


class NotAMemberError(PlatformTableError):
    """Target absent, not an active member of THIS community, or an owner/admin row."""


class NotAllowlistedError(PlatformTableError):
    """The table/column/op is not on the platform allowlist."""


class UnavailableError(PlatformTableError):
    """The capability is unprovisioned or flag-off on this host."""


class BackendError(PlatformTableError):
    """Host-side failure (details are deliberately not exposed to bundles)."""


def _validate(table: str, user: str, columns: Sequence[str]) -> str:
    """Return ``user`` as canonical UUID text; raise :class:`InvalidArgError` on bad input."""
    if not isinstance(table, str) or not 0 < len(table) <= MAX_NAME_LEN:
        raise InvalidArgError(f"table must be 1..={MAX_NAME_LEN} chars")
    if not columns or len(columns) > MAX_COLUMNS:
        raise InvalidArgError(f"columns must be 1..={MAX_COLUMNS}")
    for column in columns:
        if not isinstance(column, str) or not 0 < len(column) <= MAX_NAME_LEN:
            raise InvalidArgError(f"column names must be 1..={MAX_NAME_LEN} chars")
    if len(set(columns)) != len(columns):
        raise InvalidArgError("duplicate column")
    try:
        text = str(UUID(str(user)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgError("user must be a community user UUID") from exc
    return text


def _raise_for(exc: Exception, op: str) -> None:
    """Classify a raised WIT ``platform-table.error`` and re-raise as this module's exception."""
    detail = getattr(exc, "value", exc)
    type_name = type(detail).__name__
    payload = getattr(detail, "value", None)
    message = f"platform_table.{op} failed: {type_name}" + (
        f": {payload}" if payload is not None else ""
    )
    if type_name == "Error_Denied":
        raise DeniedError(message, code=str(payload)) from exc
    if type_name == "Error_NotAMember":
        raise NotAMemberError(message) from exc
    if type_name == "Error_NotAllowlisted":
        raise NotAllowlistedError(message) from exc
    if type_name == "Error_Invalid":
        raise InvalidArgError(message) from exc
    if type_name == "Error_Unavailable":
        raise UnavailableError(message) from exc
    raise BackendError(message) from exc


async def read(table: str, user: str, columns: Sequence[str]) -> dict[str, str]:
    """Read ``columns`` of ``user``'s row; returns ``{column: text_value}`` in request order.

    Each column needs its own ``platform.table.read:<table>.<column>`` grant.
    """
    uid = _validate(table, user, columns)
    import wit_world

    try:
        rows = wit_world.imports.platform_table.read(table, uid, list(columns))
    except PlatformTableError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see _raise_for
        _raise_for(exc, "read")
        raise  # unreachable -- _raise_for always raises
    out = {str(r.column): str(r.value) for r in rows}
    if set(out) != set(columns):
        raise BackendError("platform_table.read returned an unexpected column set")
    return out


async def write(table: str, user: str, changes: Mapping[str, str]) -> None:
    """Write ``changes`` (``{column: text_value}``) to ``user``'s row atomically (audited).

    Each column needs its own ``platform.table.write:<table>.<column>`` grant; the host
    validates values (``role``: ``member``/``vip``/``moderator`` only).
    """
    uid = _validate(table, user, list(changes))
    for value in changes.values():
        if not isinstance(value, str):
            raise InvalidArgError("values must be strings")
    import wit_world

    binding = wit_world.imports.platform_table
    try:
        binding.write(
            table,
            uid,
            [binding.ColumnValue(column=c, value=v) for c, v in changes.items()],
        )
    except PlatformTableError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see _raise_for
        _raise_for(exc, "write")
        raise  # unreachable -- _raise_for always raises
