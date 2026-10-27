"""Art. 17 erasure of the subject's data in the two stores that are NOT Postgres.

* **Object storage (SeaweedFS / S3)** -- the avatar/banner the subject uploaded. The URLs come from
  the rows holding them (`erasure_registry.OBJECT_SOURCES`), collected before those rows go.
* **Valkey bundle KV** -- per-user bundle state. Keys are
  `bundlekv:{tenant}:{community|_tenant}:{app_id}:data:{guest_key}` (`core/bundle_host_kv/src/
  scope.rs`); a bundle that keeps per-user state embeds the user's PII-free uuid token in
  `guest_key`, so a SCAN for the subject's uuid finds it. State keyed by a handle or a free-form
  name cannot be located this way -- listed in `docs/PRIVACY.md` as a known limit.

Both run synchronously (the erasure transaction is one executor job) and FAIL LOUD: an error
propagates, `_sync_erase()` rolls the database back and records a `failed` ledger row, and the
user's retry runs the whole erasure again (every operation here is idempotent). Swallowing a
delete error would report a "completed" erasure that left data behind -- the defect this module
exists to remove.

`ExternalErasure` is the seam; tests inject a fake and production uses `EnvExternalErasure`.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from services import erasure_registry as registry
from services import storage_service

logger = logging.getLogger(__name__)

_SCAN_BATCH = 500


class ExternalErasure(Protocol):
    """Deletes the subject's data from the non-Postgres stores; returns non-zero counts."""

    def erase(self, *, object_urls: Sequence[str], user_uuids: Sequence[str]) -> dict[str, int]:
        """Delete `object_urls` and every KV key naming one of `user_uuids`."""
        ...


def _tokens(user_uuids: Iterable[str]) -> list[str]:
    """Each valid uuid in its dashed AND hex spellings; invalid values are dropped (glob-safety)."""
    tokens: list[str] = []
    for raw in user_uuids:
        try:
            parsed = uuid.UUID(str(raw))
        except ValueError:
            logger.warning("dsar.kv_token_skipped_not_a_uuid")
            continue
        for spelling in (str(parsed), parsed.hex):
            if spelling not in tokens:
                tokens.append(spelling)
    return tokens


def erase_kv_keys(client: Any, user_uuids: Iterable[str]) -> int:
    """SCAN the bundle KV namespace for the subject's uuid tokens, UNLINK every hit.

    Also lowers each affected app's live-key counter (`...:count`, the quota the Rust host
    enforces) by the number removed, deleting it if it would go negative so the host's own
    reconcile pass recomputes it. Returns the number of keys removed.
    """
    removed: dict[str, int] = {}
    seen: set[str] = set()
    for token in _tokens(user_uuids):
        pattern = f"{registry.KV_DATA_GLOB_PREFIX}*{token}*"
        for raw_key in client.scan_iter(match=pattern, count=_SCAN_BATCH):
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            if key in seen or registry.KV_DATA_SEGMENT not in key:
                continue
            seen.add(key)
            client.unlink(key)
            prefix = key.split(registry.KV_DATA_SEGMENT, 1)[0]
            removed[prefix] = removed.get(prefix, 0) + 1
    for prefix, count in removed.items():
        count_key = prefix + registry.KV_COUNT_SUFFIX
        if client.exists(count_key) and int(client.decrby(count_key, count)) < 0:
            client.delete(count_key)
    return len(seen)


class EnvExternalErasure:
    """Production default: this deployment's S3 bucket and the Valkey from `VALKEY_URL`/`REDIS_URL`.

    A deployment with neither env var set has no bundle KV to erase; that is logged (WARNING) and
    reported as zero keys, never raised -- an unconfigured store holds nothing of ours.
    """

    def erase(self, *, object_urls: Sequence[str], user_uuids: Sequence[str]) -> dict[str, int]:
        """Delete the subject's uploaded objects and per-user bundle KV keys."""
        counts: dict[str, int] = {}
        if object_urls:
            deleted = storage_service.delete_owned_objects_strict(object_urls)
            if deleted:
                counts["objects_deleted"] = deleted
        url = os.getenv("VALKEY_URL") or os.getenv("REDIS_URL")
        if not user_uuids:
            return counts
        if not url:
            logger.warning("dsar.kv_erasure_skipped_unconfigured")
            return counts
        import redis  # local import: only the KV path needs the sync client

        client = redis.Redis.from_url(url, socket_timeout=10, socket_connect_timeout=5)
        try:
            keys = erase_kv_keys(client, user_uuids)
        finally:
            client.close()
        if keys:
            counts["kv_keys_deleted"] = keys
        return counts
