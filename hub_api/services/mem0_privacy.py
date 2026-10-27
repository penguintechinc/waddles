"""DSAR access (Art. 15/20) + erasure (Art. 17) for the mem0/Qdrant chat-memory store.

The AI researcher module keeps semantic chat memory in Qdrant through mem0, one
collection per community (`community_{community_id}`), every point carrying the
mem0 owner key in its payload `user_id`. That store is a data store holding
personal data (message text the subject wrote), so the DSAR export and the
erasure must reach it -- the GRC release audit found neither did.

Keying (PII boundary, `critical-rules.md` PII Tokenization): mem0 owner keys are
`user_<uuid>` -- the PII-free `hub_users.uuid` / bundle `user_uuid` / pseudonym
tokens, NEVER a username, e-mail or numeric platform id -- or the community-wide
`community_<id>` key, which belongs to no individual. `mem0_user_key()` here and
`Mem0Service.user_key()` in the AI researcher module are the one convention; the
AI researcher rejects any other shape on write, so the store can't acquire raw PII
keys. Community-wide memories (`community_<id>`) cannot be attributed to a person
and are therefore outside a per-subject export/erasure; they are a documented
residual, see `docs/PRIVACY.md`.

hub-api talks to Qdrant's REST API directly rather than importing `mem0` (a heavy
LLM/embedder dependency that belongs to the AI researcher image): a payload-filtered
scroll/count/delete is exactly what mem0's own `get_all(user_id)`/`delete_all(user_id)`
do underneath. Vectors are never exported -- only the stored memory text, ids,
timestamps and metadata.

Not configured (`QDRANT_URL` unset): the store does not exist in this deployment,
so there is nothing to export or erase -- logged at WARNING, reported as
`configured=False`. Configured but unreachable/erroring: raises `Mem0PrivacyError`
(fail loud) -- erasure must stop rather than report a purge that did not happen.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Collection-name prefix the AI researcher module uses (`Mem0Service._build_mem0_config`).
COLLECTION_PREFIX = "community_"
#: Owner-key prefix for per-user memories.
USER_KEY_PREFIX = "user_"
_COLLECTION_RE = re.compile(r"^community_(\d+)$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_TIMEOUT_SECONDS = 15.0
_SCROLL_PAGE = 256


class Mem0PrivacyError(RuntimeError):
    """Qdrant could not be read/purged; never carries a payload (it may hold PII)."""


def mem0_user_key(user_uuid: str) -> str:
    """Return the mem0 owner key for a PII-free user token (`user_<uuid>`).

    Raises:
        ValueError: `user_uuid` is not a UUID (guards against a raw username/e-mail key).
    """
    token = str(user_uuid).strip()
    if not _UUID_RE.match(token):
        raise ValueError("mem0 user key must be built from a UUID token, never raw PII")
    return f"{USER_KEY_PREFIX}{token.lower()}"


@dataclass(slots=True, frozen=True)
class Mem0StoreConfig:
    """Where the Qdrant instance lives; built from env once per call."""

    url: str
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> Mem0StoreConfig | None:
        """Read `QDRANT_URL`/`QDRANT_API_KEY`; None when no Qdrant is deployed."""
        url = os.environ.get("QDRANT_URL", "").strip().rstrip("/")
        if not url:
            return None
        return cls(url=url, api_key=os.environ.get("QDRANT_API_KEY", "").strip())


@dataclass(slots=True, frozen=True)
class Mem0Memory:
    """One exported memory point: explicit fields only, no embedding vector."""

    community_id: int
    memory_id: str
    memory: str | None
    created_at: str | None
    updated_at: str | None
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the Art. 15 export."""
        return {
            "community_id": self.community_id,
            "memory_id": self.memory_id,
            "memory": self.memory,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass(slots=True, frozen=True)
class Mem0EraseResult:
    """Outcome of an erasure: configured flag and exact points removed."""

    configured: bool
    deleted: int


# Payload keys mem0 itself owns; everything else under the payload is caller metadata.
_MEM0_RESERVED = frozenset(
    {
        "data",
        "hash",
        "created_at",
        "updated_at",
        "user_id",
        "agent_id",
        "run_id",
        "actor_id",
        "role",
    }
)


def _client(config: Mem0StoreConfig) -> httpx.AsyncClient:
    headers = {"api-key": config.api_key} if config.api_key else {}
    return httpx.AsyncClient(base_url=config.url, timeout=_TIMEOUT_SECONDS, headers=headers)


def _owner_filter(keys: Collection[str]) -> dict[str, Any]:
    return {"must": [{"key": "user_id", "match": {"any": sorted(keys)}}]}


def _keys_for(user_uuids: Iterable[str]) -> list[str]:
    """Owner keys for the subject's tokens; non-UUID tokens are skipped (never matched)."""
    keys: set[str] = set()
    for token in user_uuids:
        try:
            keys.add(mem0_user_key(token))
        except ValueError:
            logger.debug("mem0_privacy.skipped_non_uuid_token")
    return sorted(keys)


async def _post(client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.post(path, json=body)
        response.raise_for_status()
        result = response.json().get("result")
    except (httpx.HTTPError, ValueError) as exc:
        # Type only: an HTTP error body can echo filter values/payload.
        raise Mem0PrivacyError(f"qdrant request failed: {type(exc).__name__}") from exc
    return result if isinstance(result, dict) else {}


async def _community_collections(
    client: httpx.AsyncClient, community_ids: Collection[int] | None
) -> list[tuple[str, int]]:
    try:
        response = await client.get("/collections")
        response.raise_for_status()
        raw = response.json()["result"]["collections"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise Mem0PrivacyError(f"qdrant list collections failed: {type(exc).__name__}") from exc
    found: list[tuple[str, int]] = []
    for item in raw:
        match = _COLLECTION_RE.match(str(item.get("name", "")))
        if match is None:
            continue
        community_id = int(match.group(1))
        if community_ids is not None and community_id not in community_ids:
            continue
        found.append((item["name"], community_id))
    return sorted(found, key=lambda pair: pair[1])


async def export_user_memories(
    user_uuids: Iterable[str],
    *,
    community_ids: Collection[int] | None = None,
    config: Mem0StoreConfig | None = None,
) -> list[dict[str, Any]]:
    """Return every mem0 memory owned by the subject's tokens (Art. 15/20).

    `community_ids=None` is the self-service export (every community); the tenant-admin
    console passes its tenant's community ids so another tenant's collections are never read.
    A subject with no memories yields `[]`.
    """
    cfg = config or Mem0StoreConfig.from_env()
    keys = _keys_for(user_uuids)
    if cfg is None:
        logger.warning("mem0_privacy.export_skipped_qdrant_not_configured")
        return []
    if not keys:
        return []
    out: list[Mem0Memory] = []
    async with _client(cfg) as client:
        for name, community_id in await _community_collections(client, community_ids):
            offset: Any = None
            while True:
                body: dict[str, Any] = {
                    "filter": _owner_filter(keys),
                    "limit": _SCROLL_PAGE,
                    "with_payload": True,
                    "with_vector": False,
                }
                if offset is not None:
                    body["offset"] = offset
                result = await _post(client, f"/collections/{name}/points/scroll", body)
                for point in result.get("points") or []:
                    payload = point.get("payload") or {}
                    out.append(
                        Mem0Memory(
                            community_id=community_id,
                            memory_id=str(point.get("id")),
                            memory=payload.get("data"),
                            created_at=payload.get("created_at"),
                            updated_at=payload.get("updated_at"),
                            metadata={k: v for k, v in payload.items() if k not in _MEM0_RESERVED},
                        )
                    )
                offset = result.get("next_page_offset")
                if offset is None:
                    break
    logger.debug("mem0_privacy.exported", extra={"memories": len(out)})
    return [m.as_dict() for m in out]


async def erase_user_memories(
    user_uuids: Iterable[str], *, config: Mem0StoreConfig | None = None
) -> Mem0EraseResult:
    """Delete every mem0 point owned by the subject's tokens, in every community (Art. 17).

    Exact count taken before the filtered delete (`wait=true`, so the points are gone when
    this returns). Idempotent: a second call -- or a subject with no memories -- deletes 0.
    Raises `Mem0PrivacyError` on any Qdrant failure so the caller can abort the erasure.
    """
    cfg = config or Mem0StoreConfig.from_env()
    keys = _keys_for(user_uuids)
    if cfg is None:
        logger.warning("mem0_privacy.erase_skipped_qdrant_not_configured")
        return Mem0EraseResult(configured=False, deleted=0)
    if not keys:
        return Mem0EraseResult(configured=True, deleted=0)
    deleted = 0
    async with _client(cfg) as client:
        for name, _community_id in await _community_collections(client, None):
            flt = _owner_filter(keys)
            counted = await _post(
                client, f"/collections/{name}/points/count", {"filter": flt, "exact": True}
            )
            count = int(counted.get("count") or 0)
            if count == 0:
                continue
            await _post(client, f"/collections/{name}/points/delete?wait=true", {"filter": flt})
            deleted += count
    logger.info("mem0_privacy.erased", extra={"memories_deleted": deleted})
    return Mem0EraseResult(configured=True, deleted=deleted)
