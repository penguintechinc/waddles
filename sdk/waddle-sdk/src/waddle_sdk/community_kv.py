"""Community-scoped `kv` helper -- all bundle-tracked state keyed by `community_id` only.

Per the data-scoping rule in `sdk/waddle-sdk/AUTHORING.md` (itself derived
from `critical-rules.md` PII Tokenization): every bundle-tracked kv entry
belongs to exactly the community it was produced in, never global/tenant-
wide -- reputation and user-details are the only two cross-community
exceptions in the platform, and neither goes through this helper (they have
their own dedicated, explicitly cross-community storage). A thin wrapper
over `waddle_sdk.kv` that prefixes every key with its `community_id`, so
two communities' `!count` totals (etc.) can never collide or leak into each
other.
"""

from __future__ import annotations

from waddle_sdk import kv


def _scoped_key(community_id: str, key: str) -> str:
    """Build the `c:<community_id>:<key>` prefix -- fails loud on a missing community_id.

    Raises `ValueError` rather than silently falling back to a global/
    unscoped key -- a bundle call site with no `community_id` yet is a bug
    to surface immediately, never a reason to leak state tenant-wide.
    """
    if not community_id:
        raise ValueError("community_kv requires a non-empty community_id")
    return f"c:{community_id}:{key}"


async def get(community_id: str, key: str) -> bytes | None:
    """Community-scoped `kv.get()` -- see module docstring."""
    return await kv.get(_scoped_key(community_id, key))


async def set(community_id: str, key: str, value: bytes, ttl_seconds: int = 0) -> None:
    """Community-scoped `kv.set()` -- see module docstring."""
    await kv.set(_scoped_key(community_id, key), value, ttl_seconds)


async def delete(community_id: str, key: str) -> None:
    """Community-scoped `kv.delete()` -- see module docstring."""
    await kv.delete(_scoped_key(community_id, key))


async def increment(community_id: str, key: str, delta: int, ttl_seconds: int = 0) -> int:
    """Community-scoped `kv.increment()` -- see module docstring."""
    return await kv.increment(_scoped_key(community_id, key), delta, ttl_seconds)
