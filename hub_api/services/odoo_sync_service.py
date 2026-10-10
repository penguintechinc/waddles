"""Odoo / external-WaddleDB connector -- one-way community-member sync (Bar Citizen #15).

**Scope note.** `docs/plans/2026-08-26-v3-scbm-apps-design.md` has no Odoo /
WaddleDB data model (it only names Odoo as the "class" of CRM the Customer
module resembles), and issue #15 says only "a different external WaddleDB
connection for Odoo". This module is therefore the smallest coherent core:
a periodic, idempotent **push** of each flagged community's active member
roster (role + reputation) into an Odoo model via Odoo's external JSON-RPC
API (`/jsonrpc`, `common.authenticate` + `object.execute_kw`). The Odoo
model/field names are env-configurable so the Odoo side owns its schema.
Bidirectional sync, webhooks and per-tenant Odoo instances are deferred.

**PII boundary.** Nothing identifying crosses to Odoo. Each member is keyed
by an opaque reference `uuid(HMAC-SHA256(ODOO_SYNC_REF_KEY, community:user))`
-- stable (idempotent upserts) but not reversible or linkable to a platform
identity without the key. The Odoo partner `name` (required by `res.partner`)
is the placeholder `Waddles member <ref8>`; no username, email, display name
or platform id is sent. Only role, reputation and active flag ride along.

**Credentials** (`ODOO_URL`, `ODOO_DB`, `ODOO_LOGIN`, `ODOO_API_KEY`,
`ODOO_SYNC_REF_KEY`) come only from the environment (K8s Secret); missing
values raise `OdooConfigError` -- never a silent skip. Logs and spans carry
IDs and counts only, never the API key, login or any member data.

**Gating.** `waddles.bar_citizen.odoo_sync` (default OFF), evaluated per
tenant via `feature_enabled`; `WADDLES_ODOO_SYNC_ENABLED=true` is the Docker
ENV baseline (alpha has no PostHog). The Helm CronJob is a separate deploy-
time kill switch (`pipeline.odooSync.enabled`).

**Failure model.** Transport errors / HTTP 5xx / 429 retry with exponential
backoff; auth and Odoo RPC errors never retry. A failing community is
logged at ERROR and counted, never aborts the batch; `main()` exits non-zero
if any community failed or none were examined.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from services.bundle_telemetry import bundle_span, get_meter

try:
    from flask_core.feature_flags import feature_enabled
except ImportError:  # pragma: no cover -- only outside a real flask_core install
    feature_enabled = None

logger = logging.getLogger(__name__)

FEATURE_BAR_CITIZEN_ODOO_SYNC = "waddles.bar_citizen.odoo_sync"
ENV_BASELINE_VAR = "WADDLES_ODOO_SYNC_ENABLED"

_meter = get_meter()
_created_counter = _meter.create_counter(
    "odoo_sync.records_created", description="Odoo records created"
)
_updated_counter = _meter.create_counter(
    "odoo_sync.records_updated", description="Odoo records updated"
)
_errors_counter = _meter.create_counter(
    "odoo_sync.errors", description="Odoo sync failures (per community or per RPC)"
)
_retries_counter = _meter.create_counter(
    "odoo_sync.rpc_retries", description="Odoo RPC retries after transient failure"
)
_rpc_duration = _meter.create_histogram(
    "odoo_sync.rpc_duration_ms", unit="ms", description="Odoo JSON-RPC call latency"
)
_batch_size = _meter.create_histogram(
    "odoo_sync.community_batch_size", description="Members synced per community"
)


class OdooConfigError(RuntimeError):
    """Required Odoo configuration is missing or invalid -- fail loud."""


class OdooAuthError(RuntimeError):
    """Odoo rejected the credentials. Never retried."""


class OdooRpcError(RuntimeError):
    """Odoo returned a JSON-RPC error object. Never retried (not transient)."""


class OdooTransientError(RuntimeError):
    """Retries exhausted on a transport / 5xx / 429 failure."""


@dataclass(slots=True, frozen=True)
class OdooConfig:
    """Connection settings, loaded from env; `repr` never exposes secrets."""

    url: str
    db: str
    login: str
    api_key: str = field(repr=False)
    ref_key: bytes = field(repr=False)
    model: str = "res.partner"
    timeout_s: float = 10.0
    max_attempts: int = 4
    backoff_base_s: float = 0.5

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OdooConfig:
        """Build from `os.environ`; raise `OdooConfigError` naming every missing var."""
        src = os.environ if env is None else env
        required = ["ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_API_KEY", "ODOO_SYNC_REF_KEY"]
        missing = [name for name in required if not src.get(name)]
        if missing:
            raise OdooConfigError(f"missing required env: {', '.join(missing)}")
        url = src["ODOO_URL"].rstrip("/")
        if not url.startswith(("https://", "http://")):
            raise OdooConfigError("ODOO_URL must be an http(s) URL")
        return cls(
            url=url,
            db=src["ODOO_DB"],
            login=src["ODOO_LOGIN"],
            api_key=src["ODOO_API_KEY"],
            ref_key=src["ODOO_SYNC_REF_KEY"].encode(),
            model=src.get("ODOO_SYNC_MODEL", "res.partner"),
            timeout_s=float(src.get("ODOO_TIMEOUT_SECONDS", "10")),
            max_attempts=int(src.get("ODOO_MAX_ATTEMPTS", "4")),
            backoff_base_s=float(src.get("ODOO_BACKOFF_BASE_SECONDS", "0.5")),
        )


@dataclass(slots=True)
class SyncSummary:
    """Denominators for one batch -- a zero `communities_examined` is a failure."""

    communities_examined: int = 0
    communities_synced: int = 0
    communities_failed: int = 0
    communities_flag_off: int = 0
    members_examined: int = 0
    records_created: int = 0
    records_updated: int = 0


def member_ref(ref_key: bytes, community_id: int, user_id: str) -> str:
    """Opaque, stable, non-reversible UUID for a member -- the only identity Odoo sees."""
    digest = hmac.new(ref_key, f"{community_id}:{user_id}".encode(), hashlib.sha256).digest()
    return str(uuid.UUID(bytes=digest[:16], version=5))


class OdooClient:
    """Minimal async Odoo JSON-RPC client with auth caching and bounded retry/backoff."""

    def __init__(self, config: OdooConfig, http: httpx.AsyncClient | None = None) -> None:
        """Wrap `config`; pass `http` to share/inject a client (tests, mock Odoo endpoint)."""
        self._cfg = config
        self._http = http or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_http = http is None
        self._uid: int | None = None

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_http:
            await self._http.aclose()

    async def _call(self, service: str, method: str, args: list[Any]) -> Any:
        """POST one JSON-RPC call; retry transient failures with exponential backoff."""
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "id": 1,
            "params": {"service": service, "method": method, "args": args},
        }
        last: Exception | None = None
        for attempt in range(1, self._cfg.max_attempts + 1):
            started = time.perf_counter()
            try:
                async with bundle_span("odoo_sync.rpc", service=service, method=method):
                    resp = await self._http.post(f"{self._cfg.url}/jsonrpc", json=payload)
                _rpc_duration.record((time.perf_counter() - started) * 1000.0)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"odoo http {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                body = resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    if exc.response.status_code != 429:
                        raise OdooRpcError(f"odoo http {exc.response.status_code}") from exc
                last = exc
                _retries_counter.add(1)
                logger.warning(
                    "odoo rpc transient failure",
                    extra={
                        "service": service,
                        "method": method,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                    },
                )
                if attempt < self._cfg.max_attempts:
                    await asyncio.sleep(self._cfg.backoff_base_s * (2 ** (attempt - 1)))
                continue
            if "error" in body:
                err = body["error"]
                msg = str(err.get("data", {}).get("message") or err.get("message") or "rpc error")
                raise OdooRpcError(msg)
            return body.get("result")
        _errors_counter.add(1)
        raise OdooTransientError(
            f"odoo {service}.{method} failed after {self._cfg.max_attempts} attempts: "
            f"{type(last).__name__}"
        ) from last

    async def authenticate(self) -> int:
        """Authenticate once and cache the uid; `False`/None from Odoo means bad credentials."""
        if self._uid is not None:
            return self._uid
        uid = await self._call(
            "common",
            "authenticate",
            [self._cfg.db, self._cfg.login, self._cfg.api_key, {}],
        )
        if not uid:
            raise OdooAuthError("odoo rejected credentials")
        self._uid = int(uid)
        return self._uid

    async def _execute(self, method: str, args: list[Any], kwargs: dict[str, Any] | None) -> Any:
        """`object.execute_kw` against the configured model."""
        uid = await self.authenticate()
        return await self._call(
            "object",
            "execute_kw",
            [
                self._cfg.db,
                uid,
                self._cfg.api_key,
                self._cfg.model,
                method,
                args,
                kwargs or {},
            ],
        )

    async def existing_ids(self, refs: list[str]) -> dict[str, int]:
        """Map `x_waddles_ref` -> Odoo record id for refs already present."""
        rows = await self._execute(
            "search_read",
            [[["x_waddles_ref", "in", refs]]],
            {"fields": ["x_waddles_ref"]},
        )
        return {str(r["x_waddles_ref"]): int(r["id"]) for r in rows or []}

    async def create(self, values: dict[str, Any]) -> int:
        """Create one record; returns its Odoo id."""
        result = await self._execute("create", [[values]], None)
        # Odoo 17+ returns a list of ids for a list payload; older versions a bare id.
        return int(result[0] if isinstance(result, list) else result)

    async def write(self, record_id: int, values: dict[str, Any]) -> None:
        """Update one record."""
        await self._execute("write", [[record_id], values], None)


def _record_values(ref: str, community_ref: str, role: str, reputation: int) -> dict[str, Any]:
    """Odoo field payload for one member -- opaque ref + non-identifying attributes only."""
    return {
        "name": f"Waddles member {ref[:8]}",
        "x_waddles_ref": ref,
        "x_waddles_community_ref": community_ref,
        "x_waddles_role": role,
        "x_waddles_reputation": reputation,
        "active": True,
    }


async def _flag_enabled(tenant_slug: str) -> bool:
    """ENV baseline (`WADDLES_ODOO_SYNC_ENABLED`) OR the per-tenant flag; default OFF."""
    if os.environ.get(ENV_BASELINE_VAR, "").lower() in ("1", "true", "yes"):
        return True
    if feature_enabled is None:  # pragma: no cover
        return False
    return bool(await feature_enabled(FEATURE_BAR_CITIZEN_ODOO_SYNC, tenant=tenant_slug))


async def sync_community(
    dal: Any, client: OdooClient, config: OdooConfig, community_id: int
) -> tuple[int, int, int]:
    """Upsert one community's active members into Odoo; returns (examined, created, updated)."""
    rows = dal(
        (dal.community_members.community_id == community_id)
        & (dal.community_members.is_active == True)  # noqa: E712 -- pydal expression
    ).select()
    community_ref = member_ref(config.ref_key, community_id, "community")
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = str(row.user_id or row.platform_user_id or row.id)
        ref = member_ref(config.ref_key, community_id, uid)
        records[ref] = _record_values(
            ref, community_ref, str(row.role or "member"), int(row.reputation or 0)
        )
    _batch_size.record(len(records))
    if not records:
        return 0, 0, 0
    existing = await client.existing_ids(list(records))
    created = updated = 0
    for ref, values in records.items():
        if ref in existing:
            await client.write(existing[ref], values)
            updated += 1
        else:
            await client.create(values)
            created += 1
    _created_counter.add(created)
    _updated_counter.add(updated)
    return len(records), created, updated


async def run_odoo_sync_batch(dal: Any, client: OdooClient, config: OdooConfig) -> SyncSummary:
    """One pass over every community whose tenant has the flag on; fail-closed per community."""
    summary = SyncSummary()
    for community in dal(dal.communities.id > 0).select():
        summary.communities_examined += 1
        tenant = dal.tenants[community.tenant_id] if community.tenant_id else None
        slug = str(tenant.slug) if tenant else ""
        if not await _flag_enabled(slug):
            summary.communities_flag_off += 1
            logger.debug("odoo sync skipped: flag off", extra={"community_id": community.id})
            continue
        try:
            async with bundle_span("odoo_sync.community", community_id=int(community.id)):
                examined, created, updated = await sync_community(
                    dal, client, config, int(community.id)
                )
        except Exception as exc:  # noqa: BLE001 -- per-community isolation; counted + logged
            summary.communities_failed += 1
            _errors_counter.add(1)
            logger.error(
                "odoo sync failed for community",
                extra={
                    "community_id": community.id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                exc_info=True,
            )
            continue
        summary.communities_synced += 1
        summary.members_examined += examined
        summary.records_created += created
        summary.records_updated += updated
        logger.info(
            "odoo sync community done",
            extra={
                "community_id": community.id,
                "members": examined,
                "created": created,
                "updated": updated,
            },
        )
    return summary


async def main() -> int:
    """CronJob entrypoint: one pass, denominators printed; non-zero on failure/zero examined."""
    from services.bundle_install_dal import build_install_dal  # local: heavy import

    config = OdooConfig.from_env()
    install_dal = await build_install_dal(os.environ["DATABASE_URL"], pool_size=1)
    client = OdooClient(config)
    try:
        summary = await run_odoo_sync_batch(install_dal.dal, client, config)
    finally:
        await client.aclose()
    print(
        f"odoo_sync: communities_examined={summary.communities_examined} "
        f"synced={summary.communities_synced} failed={summary.communities_failed} "
        f"flag_off={summary.communities_flag_off} members={summary.members_examined} "
        f"created={summary.records_created} updated={summary.records_updated}"
    )
    if summary.communities_examined == 0 or summary.communities_failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
