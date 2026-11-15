"""Bundle-admin gateway against a REAL, fully-migrated Postgres and the REAL reputation manifest.

Covers what sqlite cannot: migration 0082's table + UNIQUE race guard, JSONB round-trips, the
active-manifest join over the real `app_active_versions`/`app_versions`/`app_version_uploads`
schema, and the real `audit_log` + audit chain. The tool call crosses the real signed
`ValkeyToolTransport`; the host side is an emulator that verifies the MAC and answers with data
shaped by the reputation bundle's own `output_schema` (the real WASM host path needs S0 -- see
docs/BUNDLE_ADMIN_GATEWAY.md). A SKIP means docker is absent -- treat it as a gate FAILURE.
"""

# ruff: noqa: F811, E402  -- fixtures re-bound as params; sys.path set before pg_docker import

from __future__ import annotations

import asyncio
import itertools
import json
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import text

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services import bundle_admin_gateway_service as svc
from services import bundle_tool_transport as tt
from services.bundle_approval_service import activate_for_community_with_warnings
from services.bundle_install_dal import build_install_dal
from services.community_authz import CommunityMembership
from services.errors import ApiError
from tests.test_bundle_activation_service import _install_and_make_available

REPO = Path(__file__).resolve().parents[2]
_SEQ = itertools.count(1)
_RING = tt.parse_key_ring("k1:" + "ef" * 16, "k1")
_USER = "0b1e2f30-0000-4000-8000-000000000001"
APP = "waddles.socials.demo.default"
REP = "waddles.core.service.reputation"
#: Service bundles the reputation manifest requests `<platform>.moderation.ban` from.
_BAN_TARGETS = tuple(f"waddles.core.service.{p}" for p in ("twitch", "discord", "youtube"))

ADMIN = CommunityMembership("admin", frozenset({"community:manage_members"}), True, False)
MOD = CommunityMembership("mod", frozenset({"community:manage_channels"}), False, False)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """Own migrated Postgres container (unique name -- the shared docker host runs other agents)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-bundle-admin-gw") as db:
        yield db


@pytest_asyncio.fixture
async def pg_dal(pg_db: PgTestDatabase) -> AsyncIterator[Any]:
    """`install_dal` over the migrated container plus the production-only columns/tables."""
    dal = await build_install_dal(pg_db.dsn.replace("postgresql://", "postgresql+asyncpg://", 1), 2)
    async with dal.engine.begin() as conn:
        for ddl in (
            "ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER",
            "ALTER TABLE communities ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
            "ALTER TABLE communities ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT TRUE",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS name TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS manifest_version TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS module TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS feature TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS provider TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS execution_model TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS is_default BOOLEAN",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS platform_compatibility JSONB",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS status TEXT",
            "CREATE TABLE IF NOT EXISTS audit_log ("
            "id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL, "
            "action VARCHAR(100) NOT NULL, target_type VARCHAR(50), target_id VARCHAR(255), "
            "details JSONB DEFAULT '{}', ip_address VARCHAR(45), user_agent TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        ):
            await conn.execute(text(ddl))
    await dal.reflect()
    yield dal
    await dal.engine.dispose()


@pytest.fixture
def run_id() -> Iterator[int]:
    """Unique-per-test integer namespacing tenant slug and community name."""
    yield next(_SEQ)


def _rep_manifest() -> dict[str, Any]:
    return dict(yaml.safe_load((REPO / "bundles/rust/reputation/hub-manifest.yaml").read_text()))


def _demo_manifest() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "app_id": APP,
        "name": "Demo",
        "version": "1.0.0",
        "feature": "waddles.socials.demo",
        "module": "socials",
        "provider": "builtin",
        "language": "python",
        "artifact": "source",
        "stages": {"action": {"entry": "x:y"}},
        "requested_tools": [{"target_app_id": REP, "tool": "leaderboard", "access": "read"}],
        "community_config_schema": {
            "type": "object",
            "properties": {"greeting": {"type": "string"}},
            "required": ["greeting"],
            "additionalProperties": False,
        },
        "admin_tools": [
            {
                "name": "board",
                "tool": "leaderboard",
                "kind": "read",
                "scope": "community:moderate",
                "target_app_id": REP,
            }
        ],
    }


class HostEmulator:
    """Valkey stand-in: verifies the request MAC like the host would, then answers per schema."""

    def __init__(self) -> None:
        """Start with no recorded requests."""
        self.requests: list[dict[str, Any]] = []
        self._reply: dict[str, Any] = {}

    def __call__(self) -> HostEmulator:
        return self

    async def lpush(self, key: str, value: str) -> None:
        req = json.loads(value)
        call = tt.ToolCall(
            req["tenant_id"],
            req["community_id"],
            req["actor_user_id"],
            req["actor_scope"],
            req["caller_app_id"],
            req["target_app_id"],
            req["tool"],
            req["args_json"],
        )
        expected = tt.sign_call(
            _RING[0]["k1"], "k1", call, request_id=req["request_id"], exp_ms=req["exp_ms"]
        )
        assert req["sig"] == expected, "host would reject: bad MAC"
        self.requests.append(req)
        args = json.loads(req["args_json"])
        if req["tool"] == "leaderboard":
            n = min(int(args.get("limit", 100)), 3)
            self._reply = {
                "entries": [{"user": _USER, "score": 10 - i, "tier": "bronze"} for i in range(n)]
            }
        else:
            self._reply = {"user": args["user"], "score": args["absolute_score"], "tier": "gold"}

    async def blpop(self, key: str, timeout: int) -> Any:  # noqa: ASYNC109
        return key, json.dumps({"ok": True, "result": self._reply})

    async def aclose(self) -> None:
        return None


async def _setup(pg_dal: Any, run_id: int) -> tuple[int, int]:
    async with pg_dal.engine.begin() as conn:
        tenant = int(
            (
                await conn.execute(
                    text("INSERT INTO tenants (slug) VALUES (:s) RETURNING id"),
                    {"s": f"gw-{run_id}"},
                )
            ).scalar_one()
        )
        community = int(
            (
                await conn.execute(
                    text("INSERT INTO communities (tenant_id, name) VALUES (:t, :n) RETURNING id"),
                    {"t": tenant, "n": f"gw-c-{run_id}"},
                )
            ).scalar_one()
        )
        await conn.execute(
            text("INSERT INTO hub_users (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        )
        for app_id in (APP, REP, *_BAN_TARGETS):
            await conn.execute(
                text(
                    "INSERT INTO app_catalog (app_id) VALUES (:a) ON CONFLICT (app_id) DO NOTHING"
                ),
                {"a": app_id},
            )
        for app_id in (REP, *_BAN_TARGETS):
            await conn.execute(
                text("UPDATE app_catalog SET kind = 'service' WHERE app_id = :a"), {"a": app_id}
            )
    return tenant, community


async def _install_available(pg_dal: Any, tenant: int, manifest: dict[str, Any]) -> None:
    """Install (once per app/version across the shared DB) and make available to `tenant`."""
    async with pg_dal.engine.connect() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM app_versions WHERE app_id = :a AND version = :v"),
                {"a": manifest["app_id"], "v": manifest["version"]},
            )
        ).first()
    if exists is None:
        await _install_and_make_available(pg_dal, tenant_id=tenant, manifest=manifest)
    else:
        from services.tenant_app_availability_service import set_available

        await set_available(pg_dal, tenant_id=tenant, app_id=manifest["app_id"], updated_by=1)


async def _activate(pg_dal: Any, tenant: int, community: int, manifest: dict[str, Any]) -> None:
    """Install and activate for a community, first installing every requested tool target.

    Activation refuses a `requested_tools` target that is not an installed, tenant-available
    SERVICE bundle (`tool_target_not_found`), so a manifest that requests other bundles' tools
    (the reputation service's `<platform>.moderation.ban` floor-ban targets) gets the REAL
    manifests of those targets installed first -- no catalog stand-ins.
    """
    for req in manifest.get("requested_tools") or []:
        target = req["target_app_id"]
        if target in (REP, APP):
            continue
        slug = target.rsplit(".", 1)[1]
        target_manifest = dict(
            yaml.safe_load((REPO / f"bundles/rust/{slug}/hub-manifest.yaml").read_text())
        )
        assert target_manifest["app_id"] == target, f"{slug} manifest is not {target}"
        await _install_available(pg_dal, tenant, target_manifest)
    await _install_available(pg_dal, tenant, manifest)
    await activate_for_community_with_warnings(
        pg_dal,
        tenant_id=tenant,
        community_id=community,
        app_id=manifest["app_id"],
        activated_by=1,
        acknowledged_access_grants=frozenset(),
    )


async def test_real_reputation_tools_through_real_pg(pg_dal: Any, run_id: int) -> None:
    tenant, community = await _setup(pg_dal, run_id)
    await _activate(pg_dal, tenant, community, _rep_manifest())
    host = HostEmulator()
    transport = tt.ValkeyToolTransport(host, keys=_RING)
    bundle = await svc.load_active_bundle(
        pg_dal, tenant_id=tenant, community_id=community, app_id=REP
    )
    assert {t.name for t in bundle.manifest.admin_tools} == {
        "leaderboard",
        "override",
        "at_risk",
        "set_policy",
    }

    view = await svc.read_data_view(
        pg_dal,
        transport,
        bundle,
        MOD,
        view_name="leaderboard",
        tenant_id=tenant,
        community_id=community,
        actor_id=1,
        args={},
        limit=2,
        cursor=None,
    )
    assert len(view.result["entries"]) == 2 and view.limit == 2
    assert json.loads(host.requests[-1]["args_json"]) == {"limit": 2}
    assert host.requests[-1]["actor_scope"] == "community:moderate"

    done = await svc.invoke_admin_action(
        pg_dal,
        transport,
        bundle,
        ADMIN,
        tool_name="override",
        tenant_id=tenant,
        community_id=community,
        actor_id=1,
        args={"user": _USER, "absolute_score": 42},
    )
    assert done.result["score"] == 42
    assert host.requests[-1]["actor_scope"] == "community:admin"

    with pytest.raises(ApiError) as bad:  # real input_schema: absolute_score is an integer
        await svc.invoke_admin_action(
            pg_dal,
            transport,
            bundle,
            ADMIN,
            tool_name="override",
            tenant_id=tenant,
            community_id=community,
            actor_id=1,
            args={"user": _USER, "absolute_score": "x"},
        )
    assert bad.value.status_code == 422
    with pytest.raises(ApiError) as denied:  # moderator < the manifest's community:admin
        await svc.invoke_admin_action(
            pg_dal,
            transport,
            bundle,
            MOD,
            tool_name="override",
            tenant_id=tenant,
            community_id=community,
            actor_id=1,
            args={"user": _USER, "absolute_score": 1},
        )
    assert denied.value.status_code == 403
    assert len(host.requests) == 2  # refused calls never reached the host

    async with pg_dal.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT details FROM audit_log WHERE action = 'bundle_admin.tool_invoked' "
                        "AND (details->>'community_id')::int = :c ORDER BY id"
                    ),
                    {"c": community},
                )
            )
            .scalars()
            .all()
        )
    assert [(d["tool"], d["phase"]) for d in rows] == [
        ("override", "attempt"),
        ("override", "result"),
    ]
    assert _USER not in json.dumps(rows) and "42" not in json.dumps(rows)


async def test_cross_bundle_view_and_tenant_isolation(pg_dal: Any, run_id: int) -> None:
    tenant, community = await _setup(pg_dal, run_id)
    await _activate(pg_dal, tenant, community, _rep_manifest())
    await _activate(pg_dal, tenant, community, _demo_manifest())
    host = HostEmulator()
    transport = tt.ValkeyToolTransport(host, keys=_RING)
    bundle = await svc.load_active_bundle(
        pg_dal, tenant_id=tenant, community_id=community, app_id=APP
    )
    out = await svc.read_data_view(
        pg_dal,
        transport,
        bundle,
        MOD,
        view_name="board",
        tenant_id=tenant,
        community_id=community,
        actor_id=1,
        args={},
        limit=None,
        cursor=None,
    )
    assert host.requests[-1]["target_app_id"] == REP and host.requests[-1]["caller_app_id"] == APP
    assert len(out.result["entries"]) == 3
    other_tenant, other_community = await _setup(pg_dal, run_id + 10_000)
    with pytest.raises(ApiError) as exc:
        await svc.load_active_bundle(
            pg_dal, tenant_id=other_tenant, community_id=other_community, app_id=APP
        )
    assert exc.value.code == "bundle_not_active"


async def test_config_versions_and_unique_race_on_real_pg(pg_dal: Any, run_id: int) -> None:
    tenant, community = await _setup(pg_dal, run_id)
    await _activate(pg_dal, tenant, community, _rep_manifest())
    await _activate(pg_dal, tenant, community, _demo_manifest())
    bundle = await svc.load_active_bundle(
        pg_dal, tenant_id=tenant, community_id=community, app_id=APP
    )
    kw: dict[str, Any] = {"tenant_id": tenant, "community_id": community}
    snap = await svc.save_config(
        pg_dal, bundle, ADMIN, actor_id=1, config={"greeting": "hi"}, expected_version=0, **kw
    )
    assert (snap.version, snap.config) == (1, {"greeting": "hi"})
    with pytest.raises(ApiError) as bad:
        await svc.save_config(
            pg_dal, bundle, ADMIN, actor_id=1, config={"greeting": 1}, expected_version=1, **kw
        )
    assert bad.value.status_code == 422

    results = await asyncio.gather(
        *(
            svc.save_config(
                pg_dal, bundle, ADMIN, actor_id=1, config={"greeting": g}, expected_version=1, **kw
            )
            for g in ("a", "b")
        ),
        return_exceptions=True,
    )
    wins = [r for r in results if isinstance(r, svc.ConfigSnapshot)]
    losses = [r for r in results if isinstance(r, ApiError)]
    assert len(wins) == 1 and len(losses) == 1 and losses[0].status_code == 409
    latest = await svc.get_config(pg_dal, bundle, ADMIN, **kw)
    assert latest.version == 2
    hist = await svc.list_config_history(pg_dal, bundle, MOD, limit=10, **kw)
    assert [h.version for h in hist] == [2, 1]
    with pytest.raises(ApiError):  # moderators read but never write
        await svc.save_config(
            pg_dal, bundle, MOD, actor_id=1, config={"greeting": "z"}, expected_version=2, **kw
        )


async def test_runtime_config_merges_saved_over_defaults_on_real_pg(
    pg_dal: Any, run_id: int
) -> None:
    """`BundleConfigService`'s read path on the real schema: latest version wins, tenant-bound."""
    from services import bundle_community_config_runtime as rt

    tenant, community = await _setup(pg_dal, run_id)
    manifest = _demo_manifest()
    # The reputation manifest no longer parses on this base (pre-existing unknown_permission /
    # missing_reason_codes); runtime config does not depend on it, so no cross-bundle tool grant.
    manifest["requested_tools"] = []
    manifest["version"] = "1.0.9"  # own version: `_activate` installs once per (app, version)
    manifest["community_config_schema"] = {
        **manifest["community_config_schema"],
        "properties": {
            **manifest["community_config_schema"]["properties"],
            "greeting": {"type": "string", "maxLength": 20, "default": "hello"},
        },
        "required": [],
    }
    await _activate(pg_dal, tenant, community, manifest)
    bundle = await svc.load_active_bundle(
        pg_dal, tenant_id=tenant, community_id=community, app_id=APP
    )
    kw: dict[str, Any] = {"tenant_id": tenant, "community_id": community}
    before = await rt.get_runtime_config(pg_dal, app_id=APP, **kw)
    assert (before.version, before.config.get("greeting")) == (0, "hello")
    await svc.save_config(
        pg_dal, bundle, ADMIN, actor_id=1, config={"greeting": "hi"}, expected_version=0, **kw
    )
    after = await rt.get_runtime_config(pg_dal, app_id=APP, **kw)
    assert (after.version, after.config["greeting"]) == (1, "hi")
    other_tenant, _ = await _setup(pg_dal, run_id + 7_000)
    with pytest.raises(rt.CommunityNotInTenantError):
        await rt.get_runtime_config(
            pg_dal, tenant_id=other_tenant, community_id=community, app_id=APP
        )
