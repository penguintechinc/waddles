"""Community-scoped `kv` helper -- all bundle-tracked state keyed by `community_id` only.

Per the data-scoping rule in `sdk/waddle-sdk/AUTHORING.md` (itself derived
from `critical-rules.md` PII Tokenization): every bundle-tracked kv entry
belongs to exactly the community it was produced in -- reputation and
user-details are the only two cross-community exceptions in the platform,
and neither goes through this helper (they have their own dedicated,
explicitly cross-community storage). A thin wrapper over `waddle_sdk.kv`
that prefixes every key with its `community_id`, so two communities' `!count`
totals (etc.) can never collide or leak into each other.

**The tenant-wide sentinel (`community_id=None`) is one such community, not
an opt-out.** `None` is the host's own explicit, deliberate signal for a
tenant-wide activation (`core/bundle_active_set/src/scope.rs::resolve_scope`:
`community_id == 0` is the DB-level sentinel, matching `app_active_versions
.community_id`'s own convention; `core/svc_ingest/src/config.rs::ingest_
scope` collapses an empty `RUNNER_COMMUNITY` to the same `None` before it
ever reaches a bundle) -- exactly how `core/bundle_host_kv/src/scope.rs::
KvScope` already treats it for the host-scoped `kv` capability itself
(`community: None` renders the literal `_tenant` segment, never an error).
Today it is alpha's *only* activation shape (one static scope per
`svc-ingest-rust` pod), so refusing it here would make `community_kv` -- and
every bundle built on it -- permanently nonfunctional in that environment.
Scoped under the literal segment `"0"` ([`TENANT_WIDE_SENTINEL`]) so it
reads consistently with the DB's own numeric convention. An **empty
string** is different and keeps failing loud: the host never emits one, so
one reaching here can only be a caller-side bug (e.g. `community or ""`
somewhere upstream) -- never silently treated as tenant-wide or as a
global/unscoped key.
"""

from __future__ import annotations

from waddle_sdk import kv

#: Guest-visible scope segment for the host's tenant-wide sentinel -- see
#: module docstring. Exported so a bundle that needs to recognize/render
#: this exact scope (e.g. in a log line or an admin-facing message) shares
#: one literal instead of hardcoding `"0"` itself.
TENANT_WIDE_SENTINEL = "0"


def _scoped_key(community_id: str | None, key: str) -> str:
    """Build the `c.<community_id>.<key>` prefix -- fails loud on an empty-string community_id.

    `community_id is None` is the host's own tenant-wide sentinel (module
    docstring) and scopes under [`TENANT_WIDE_SENTINEL`] (`"0"`).
    `community_id == ""` still raises `ValueError` rather than silently
    falling back to a global/unscoped key -- the host never emits an empty
    string, so one reaching here is a bug to surface immediately, never a
    reason to leak state tenant-wide.

    Uses `.` as the separator, never `:` (gh-631): the whole string this
    builds is passed to `waddle_sdk.kv` as a single guest-supplied key, and
    `:` is the real `kv` host capability's own reserved namespace separator
    (`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) -- the host
    has no notion of a "community_kv prefix", so a `:` here would be
    rejected exactly like any other guest key. `waddle_sdk.kv.validate_key`
    (called by every function below) enforces this on the final key.
    """
    if community_id == "":
        raise ValueError(
            "community_kv requires a non-empty community_id "
            "(pass None for the host's tenant-wide sentinel, never '')"
        )
    scope = community_id if community_id is not None else TENANT_WIDE_SENTINEL
    return f"c.{scope}.{key}"


async def get(community_id: str | None, key: str) -> bytes | None:
    """Community-scoped `kv.get()` -- see module docstring."""
    return await kv.get(_scoped_key(community_id, key))


async def set(community_id: str | None, key: str, value: bytes, ttl_seconds: int = 0) -> None:
    """Community-scoped `kv.set()` -- see module docstring."""
    await kv.set(_scoped_key(community_id, key), value, ttl_seconds)


async def delete(community_id: str | None, key: str) -> None:
    """Community-scoped `kv.delete()` -- see module docstring."""
    await kv.delete(_scoped_key(community_id, key))


async def increment(community_id: str | None, key: str, delta: int, ttl_seconds: int = 0) -> int:
    """Community-scoped `kv.increment()` -- see module docstring."""
    return await kv.increment(_scoped_key(community_id, key), delta, ttl_seconds)
