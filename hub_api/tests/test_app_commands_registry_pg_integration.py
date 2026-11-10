"""Slash-command desired state + active context against a REAL, fully-migrated Postgres.

Proves what sqlite cannot: the reflected PG `UUID` column binds the canonical user token, the real
FKs/CHECKs hold under the service's writes, the tenant `is_active` boolean gates pairing, concurrent
first-writes of the same (user, source) converge to one row, and the HTTP surface works end to end.
A SKIP means docker is absent -- treat it as a gate FAILURE, never a pass.
"""

# ruff: noqa: F811  -- imported pytest fixtures are re-bound as test params

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey
from quart import Quart
from quart_schema import QuartSchema
from sqlalchemy import func, select, text

from blueprints.v1.app_commands_internal import SCOPE_APP_COMMANDS, app_commands_internal_bp
from services import app_command_registry as registry
from services.dataplane_scope import ScopeNotFoundError
from tests.test_seed_core_bundles_pg_integration import (
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
    pg_dal,  # noqa: F401  # fixture
)

_SEQ = itertools.count(1)
INGEST = "spiffe://penguintech.io/alpha/svc-ingest"
BASE = "/api/v1/internal/dataplane"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """Own uniquely-named container (never the seeder test's), migrated to alembic `head`."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-slash-registry") as db:
        yield db


@pytest_asyncio.fixture
async def dal(pg_dal: Any) -> AsyncIterator[Any]:  # noqa: F811
    """`install_dal` plus the production `community_servers` / platform columns, re-reflected."""
    async with pg_dal.engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE communities ADD COLUMN IF NOT EXISTS platform VARCHAR(50)")
        )
        await conn.execute(
            text("ALTER TABLE communities ADD COLUMN IF NOT EXISTS platform_server_id VARCHAR(255)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS community_servers ("
                "id SERIAL PRIMARY KEY, "
                "community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, "
                "platform VARCHAR(50) NOT NULL, platform_server_id VARCHAR(255) NOT NULL, "
                "status VARCHAR(50) DEFAULT 'pending', is_primary BOOLEAN DEFAULT false, "
                "UNIQUE(community_id, platform, platform_server_id))"
            )
        )
    pg_dal.metadata.clear()  # reflect() keeps already-loaded tables; re-read the altered columns
    await pg_dal.reflect()
    yield pg_dal


class World:
    """One tenant, two communities paired to a unique guild, one registered app."""

    tenant = 0
    first = 0
    second = 0
    guild = ""
    app_id = ""
    tenant_pub = ""
    first_pub = ""
    second_pub = ""
    app_pub = ""


async def _world(dal: Any) -> World:
    n = next(_SEQ)
    w = World()
    w.guild = f"{7000 + n}00000000000"
    w.app_id = f"slash-app-{n}"
    async with dal.engine.begin() as conn:
        row = (
            await conn.execute(
                text("INSERT INTO tenants (slug) VALUES (:s) RETURNING id, public_id"),
                {"s": f"slash-pg-{n}"},
            )
        ).one()
        w.tenant, w.tenant_pub = int(row.id), str(row.public_id)
        for attr, name in (("first", "Alpha"), ("second", "Beta")):
            crow = (
                await conn.execute(
                    text(
                        "INSERT INTO communities (tenant_id, name) VALUES (:t, :n) "
                        "RETURNING id, public_id"
                    ),
                    {"t": w.tenant, "n": f"{name}-{n}"},
                )
            ).one()
            cid = int(crow.id)
            setattr(w, attr, cid)
            setattr(w, f"{attr}_pub", str(crow.public_id))
            await conn.execute(
                text(
                    "INSERT INTO community_servers (community_id, platform, platform_server_id, "
                    "status) VALUES (:c, 'discord', :g, 'approved')"
                ),
                {"c": cid, "g": w.guild},
            )
        w.app_pub = str(
            (
                await conn.execute(
                    text("INSERT INTO app_catalog (app_id) VALUES (:a) RETURNING public_id"),
                    {"a": w.app_id},
                )
            ).scalar_one()
        )
    return w


async def _command(dal: Any, w: World, command: str, community_id: int, types: int = 2) -> None:
    table = dal.metadata.tables["app_commands"]
    async with dal.engine.begin() as conn:
        await conn.execute(
            table.insert().values(
                tenant_id=w.tenant,
                community_id=community_id,
                app_id=w.app_id,
                command=command,
                command_types=types,
                platforms=["discord"],
                description=f"{command} desc",
                registered_at=datetime.now(UTC),
            )
        )


async def test_desired_state_on_real_pg(dal: Any) -> None:
    w = await _world(dal)
    await _command(dal, w, "ping", w.first)
    await _command(dal, w, "chat", w.first, types=1)
    await _command(dal, w, "roll", 0)
    state = await registry.desired_commands(dal, "discord", w.guild)
    assert [c.name for c in state.commands] == ["context", "ping", "roll"]
    assert state.tenant_public_id == w.tenant_pub and state.version.startswith("sha256:")
    ping = next(c for c in state.commands if c.name == "ping")
    assert ping.community_public_ids == (w.first_pub,) and ping.app_public_ids == (w.app_pub,)
    roll = next(c for c in state.commands if c.name == "roll")
    assert roll.community_public_ids == ()  # tenant-wide
    assert state == await registry.desired_commands(dal, "discord", w.guild)


async def test_inactive_tenant_does_not_pair(dal: Any) -> None:
    w = await _world(dal)
    async with dal.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET is_active = FALSE WHERE id = :t"), {"t": w.tenant}
        )
    with pytest.raises(ScopeNotFoundError):
        await registry.paired_source(dal, "discord", w.guild)


async def test_list_sources_contains_world_and_paginates(dal: Any) -> None:
    w = await _world(dal)
    total = (await registry.list_sources(dal, "discord", limit=1, offset=0)).total
    seen: list[str] = []
    for offset in range(0, total, 2):
        seen += (await registry.list_sources(dal, "discord", limit=2, offset=offset)).items
    assert w.guild in seen and len(seen) == total == len(set(seen))


async def test_context_roundtrip_binds_uuid_and_upserts(dal: Any) -> None:
    w = await _world(dal)
    user = str(uuid.uuid4())
    ctx = await registry.set_active_context(
        dal, "discord", w.guild, user.upper(), w.second_pub.upper()
    )
    assert ctx.community_public_id == w.second_pub and ctx.explicit
    await registry.set_active_context(dal, "discord", w.guild, user, w.first_pub)
    got = await registry.get_active_context(dal, "discord", w.guild, user)
    assert got.community_public_id == w.first_pub and got.explicit
    table = dal.metadata.tables["user_active_context"]
    async with dal.engine.connect() as conn:
        count = (await conn.execute(select(func.count()).select_from(table))).scalar_one()
        stored = (await conn.execute(select(table.c.user_uuid, table.c.tenant_id))).first()
    assert count >= 1 and str(stored.user_uuid) == user and stored.tenant_id == w.tenant
    assert await registry.clear_active_context(dal, "discord", w.guild, user) is True
    assert await registry.clear_active_context(dal, "discord", w.guild, user) is False


async def test_concurrent_first_writes_converge_to_one_row(dal: Any) -> None:
    w = await _world(dal)
    user = str(uuid.uuid4())
    results = await asyncio.gather(
        *[
            registry.set_active_context(
                dal, "discord", w.guild, user, w.first_pub if i % 2 else w.second_pub
            )
            for i in range(8)
        ],
        return_exceptions=True,
    )
    assert not [r for r in results if isinstance(r, Exception)], results
    table = dal.metadata.tables["user_active_context"]
    async with dal.engine.connect() as conn:
        rows = (
            await conn.execute(select(table.c.id).where(table.c.user_uuid == uuid.UUID(user)))
        ).all()
    assert len(rows) == 1


async def test_community_delete_cascades_context(dal: Any) -> None:
    w = await _world(dal)
    user = str(uuid.uuid4())
    await registry.set_active_context(dal, "discord", w.guild, user, w.second_pub)
    async with dal.engine.begin() as conn:
        await conn.execute(text("DELETE FROM communities WHERE id = :c"), {"c": w.second})
    ctx = await registry.get_active_context(dal, "discord", w.guild, user)
    assert ctx.community_public_id == w.first_pub and ctx.explicit is False


async def test_http_end_to_end(dal: Any) -> None:
    w = await _world(dal)
    await _command(dal, w, "ping", w.first)
    private = Ed25519PrivateKey.generate()
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())
    issuer = ServiceJwtIssuer(
        keys={"k1": key},
        active_kid="k1",
        identities={
            INGEST: ServiceIdentity(
                service_id=INGEST,
                k8s_namespace="waddlebot",
                k8s_service_account="svc-ingest",
                allowed_scopes=frozenset({SCOPE_APP_COMMANDS}),
                tenant="system",
            )
        },
        audience="waddlebot-internal",
    )
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(app_commands_internal_bp)
    app.config["install_dal"] = dal
    app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    client = app.test_client()
    cmds = {"Authorization": f"Bearer {issuer.issue(INGEST, SCOPE_APP_COMMANDS)}"}
    desired = await client.get(
        f"{BASE}/app-commands/desired?platform=discord&source_id={w.guild}", headers=cmds
    )
    body = await desired.get_json()
    assert desired.status_code == 200 and [c["name"] for c in body["commands"]] == [
        "context",
        "ping",
    ]
