"""`source_group_reconciler` against a REAL Postgres (migrated to head) and a REAL Valkey 8.

regression: alpha 2026-11-14 -- svc-process logged `NOGROUP` forever for six already-active apps
because migration 0098 renamed the catch-all tenant slug `global` -> `default` (stream keys embed
the slug) and nothing re-provisioned their consumer groups. Docker absent is a FAILURE (no skip),
matching the other real-infra gates.
"""

# ruff: noqa: F811  -- imported pytest fixtures (`pg_db`/`pg_dal`) are re-bound as test params

from __future__ import annotations

import asyncio
import itertools
import logging
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as aioredis
from flask_core.stream_pipeline import source_stream_key
from penguin_dal import AsyncDB
from sqlalchemy import text

from services import source_group_reconciler as rec
from tests.test_seed_core_bundles_pg_integration import (  # noqa: F401  # fixtures
    pg_dal,
    pg_db,
)

_SEQ = itertools.count(1)


def _free_port() -> int:
    """An unused localhost TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_valkey(*extra: str) -> Iterator[str]:
    """Own `valkey/valkey:8` container (optional server args); yields its host:port."""
    port = _free_port()
    name = f"waddles-hubapi-grp-{uuid.uuid4().hex[:8]}"
    subprocess.run(  # noqa: S603, S607
        [  # noqa: S607
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:6379",
            "valkey/valkey:8",
            "valkey-server",
            *extra,
        ],
        check=True,
        capture_output=True,
    )
    try:
        deadline = time.monotonic() + 180
        while True:
            probe = subprocess.run(  # noqa: S603, S607
                ["docker", "exec", name, "valkey-cli", "ping"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.stdout.strip() == "PONG":
                break
            assert time.monotonic() < deadline, "valkey never became ready"
            time.sleep(0.3)
        yield f"127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)  # noqa: S603, S607


@pytest.fixture(scope="module")
def valkey_addr() -> Iterator[str]:
    """A plain Valkey whose default user may do everything (the production default-user shape)."""
    yield from _start_valkey()


@pytest.fixture(scope="module")
def valkey_no_stream_addr() -> Iterator[str]:
    """Valkey where user `lim` is denied every `@stream` command (XGROUP included)."""
    yield from _start_valkey("--user", "lim", "on", "nopass", "~*", "&*", "+@all", "-@stream")


@pytest.fixture
async def client(valkey_addr: str) -> AsyncIterator[Any]:
    """A decoded `redis.asyncio` client on the plain Valkey, flushed per test."""
    c = aioredis.from_url(f"redis://{valkey_addr}/0", decode_responses=True)
    await c.flushall()
    yield c
    await c.aclose()


_SQLITE_SCHEMA = (
    "CREATE TABLE tenants (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE)",
    "CREATE TABLE communities (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL,"
    " name TEXT NOT NULL)",
    "CREATE TABLE app_source_bindings (tenant_id INTEGER NOT NULL, community_id INTEGER NOT NULL"
    " DEFAULT 0, app_id TEXT NOT NULL, platform TEXT NOT NULL, source_id TEXT NOT NULL)",
)


@pytest.fixture
async def dal(tmp_path: Path) -> AsyncIterator[AsyncDB]:
    """A real SQL `AsyncDB` (sqlite file) holding the three tables the reconcile query joins.

    The join is plain portable SQL, so these tests need no Postgres container; the exact query is
    additionally proven against the real migrated Postgres schema in the `pg_` test below.
    """
    db = AsyncDB(f"sqlite://{tmp_path / 'reconcile.db'}", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        for ddl in _SQLITE_SCHEMA:
            await conn.exec_driver_sql(ddl)
    yield db
    await db.close()


class _Counters:
    """Records `.add(value, labels)` calls in place of the OTel instruments."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, str]]] = []

    def add(self, value: int, labels: dict[str, str]) -> None:
        self.calls.append((value, labels))

    def record(self, value: float, labels: dict[str, str]) -> None:
        self.calls.append((int(value), labels))

    def count(self, result: str) -> int:
        return sum(v for v, lab in self.calls if lab.get("result") == result)


@pytest.fixture(autouse=True)
def metrics(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Counters]:
    """Swap the lazily-built instruments for recorders so emission is asserted with counts."""
    inst = rec._Instruments(groups=_Counters(), passes=_Counters(), duration_ms=_Counters())
    monkeypatch.setattr(rec, "_INSTRUMENTS", inst)
    return {"groups": inst.groups, "passes": inst.passes, "duration": inst.duration_ms}


async def _seed(
    dal: Any,
    *,
    slug: str,
    community: str | None,
    apps: list[str],
    platform: str = "discord",
    source_id: str = "dg-1",
    catalog: bool = False,
) -> tuple[int, int | None]:
    """Insert a tenant (+ community unless tenant-wide) and one binding per app; return ids."""
    async with dal.engine.begin() as conn:
        tenant_id = int(
            (
                await conn.execute(
                    text("INSERT INTO tenants (slug) VALUES (:s) RETURNING id"), {"s": slug}
                )
            ).scalar_one()
        )
        community_id: int | None = None
        if community is not None:
            community_id = int(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO communities (tenant_id, name) VALUES (:t, :n) RETURNING id"
                        ),
                        {"t": tenant_id, "n": community},
                    )
                ).scalar_one()
            )
        for app_id in apps:
            if catalog:  # real Postgres schema: app_source_bindings.app_id FKs app_catalog
                await conn.execute(
                    text(
                        "INSERT INTO app_catalog (app_id) VALUES (:a)"
                        " ON CONFLICT (app_id) DO NOTHING"
                    ),
                    {"a": app_id},
                )
            await conn.execute(
                text(
                    "INSERT INTO app_source_bindings "
                    "(tenant_id, community_id, app_id, platform, source_id) "
                    "VALUES (:t, :c, :a, :p, :s)"
                ),
                {
                    "t": tenant_id,
                    "c": community_id or 0,
                    "a": app_id,
                    "p": platform,
                    "s": source_id,
                },
            )
    return tenant_id, community_id


async def _groups(client: Any, stream: str) -> set[str]:
    """Consumer-group names on `stream` (empty set when the stream does not exist)."""
    try:
        return {g["name"] for g in await client.xinfo_groups(stream)}
    except Exception:  # noqa: BLE001 - no such key -> no groups
        return set()


def _uniq(prefix: str) -> str:
    return f"{prefix}{next(_SEQ)}-{uuid.uuid4().hex[:6]}"


async def test_fresh_binding_gets_its_group(dal: Any, client: Any) -> None:
    """A binding with no group at all is provisioned on the stream svc-process will read."""
    slug, comm = _uniq("t"), _uniq("c")
    apps = ["waddles.core.example.ping", "waddles.core.example.roll"]
    await _seed(dal, slug=slug, community=comm, apps=apps)

    result = await rec.reconcile_once(dal, client)

    stream = source_stream_key(slug, comm, "discord", "dg-1")
    assert (result.examined, result.created, result.failed) >= (2, 2, 0)
    assert await _groups(client, stream) == set(apps)


async def test_renamed_tenant_slug_is_reconciled_to_the_new_stream(dal: Any, client: Any) -> None:
    """The incident: groups exist only on the OLD-slug stream; the new-slug stream gets them."""
    old_slug, new_slug, comm = _uniq("global"), _uniq("default"), _uniq("c")
    apps = ["waddles.core.example.ping", "waddles.core.example.dj"]
    tenant_id, _ = await _seed(dal, slug=old_slug, community=comm, apps=apps)
    old_stream = source_stream_key(old_slug, comm, "discord", "dg-1")
    for app in apps:  # what hub-api provisioned BEFORE migration 0098's rename
        await client.xgroup_create(old_stream, app, id="$", mkstream=True)
    async with dal.engine.begin() as conn:  # migration 0098: slug renamed, id kept
        await conn.execute(
            text("UPDATE tenants SET slug = :s WHERE id = :i"), {"s": new_slug, "i": tenant_id}
        )
    new_stream = source_stream_key(new_slug, comm, "discord", "dg-1")
    assert await _groups(client, new_stream) == set()  # NOGROUP precondition

    result = await rec.reconcile_once(dal, client)

    assert result.failed == 0
    assert await _groups(client, new_stream) == set(apps)
    assert await _groups(client, old_stream) == set(apps)  # old keys are never destroyed


async def test_second_run_is_idempotent_and_keeps_group_state(
    dal: Any, client: Any, metrics: dict[str, _Counters]
) -> None:
    """Re-running creates nothing, never resets a group's cursor, and counts `exists`."""
    slug, comm = _uniq("t"), _uniq("c")
    await _seed(dal, slug=slug, community=comm, apps=["waddles.core.example.ping"])
    stream = source_stream_key(slug, comm, "discord", "dg-1")

    first = await rec.reconcile_once(dal, client)
    await client.xadd(stream, {"k": "v"})
    got = await client.xreadgroup("waddles.core.example.ping", "c1", {stream: ">"}, count=1)
    assert got and got[0][1], "group created by the reconcile must deliver later events"
    before = (await client.xinfo_groups(stream))[0]
    second = await rec.reconcile_once(dal, client)
    after = (await client.xinfo_groups(stream))[0]

    assert first.created >= 1
    assert (second.created, second.failed) == (0, 0)
    assert second.already_present == second.examined >= 1
    assert after["last-delivered-id"] == before["last-delivered-id"]
    assert after["pending"] == before["pending"] == 1
    assert metrics["groups"].count("exists") >= 1


async def test_group_starts_at_dollar_so_old_backlog_is_not_replayed(dal: Any, client: Any) -> None:
    """Start id is `$`: pre-existing entries are not replayed into a late-provisioned group."""
    slug, comm = _uniq("t"), _uniq("c")
    await _seed(dal, slug=slug, community=comm, apps=["waddles.core.example.ping"])
    stream = source_stream_key(slug, comm, "discord", "dg-1")
    await client.xadd(stream, {"old": "1"})

    await rec.reconcile_once(dal, client)
    old = await client.xreadgroup("waddles.core.example.ping", "c1", {stream: ">"}, count=10)
    assert not old or not old[0][1]
    await client.xadd(stream, {"new": "1"})
    new = await client.xreadgroup("waddles.core.example.ping", "c1", {stream: ">"}, count=10)
    assert [fields for _id, fields in new[0][1]] == [{"new": "1"}]


async def test_tenant_wide_binding_uses_the_tenant_segment(dal: Any, client: Any) -> None:
    """community_id 0 (tenant-wide sentinel) renders `c:_tenant`, matching Scope::source_stream."""
    slug = _uniq("t")
    await _seed(dal, slug=slug, community=None, apps=["waddles.core.example.ping"])

    result = await rec.reconcile_once(dal, client)

    assert result.failed == 0
    stream = source_stream_key(slug, None, "discord", "dg-1")
    assert f":t:{slug}:c:_tenant:src:" in stream
    assert await _groups(client, stream) == {"waddles.core.example.ping"}


async def test_denied_xgroup_fails_loudly(
    dal: Any,
    valkey_no_stream_addr: str,
    caplog: pytest.LogCaptureFixture,
    metrics: dict[str, _Counters],
) -> None:
    """Wrong ACL: ERROR log per binding, `failed` counted, pass marked error, nothing swallowed."""
    slug, comm = _uniq("t"), _uniq("c")
    apps = ["waddles.core.example.ping", "waddles.core.example.roll"]
    await _seed(dal, slug=slug, community=comm, apps=apps)
    limited = aioredis.from_url(f"redis://lim:x@{valkey_no_stream_addr}/0", decode_responses=True)
    try:
        with caplog.at_level(logging.ERROR, logger=rec.logger.name):
            result = await rec.reconcile_once(dal, limited)
    finally:
        await limited.aclose()

    assert result.failed >= 2
    assert not result.ok
    assert all(f.startswith("NoPermissionError") for f in result.failures)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) >= 2
    assert all(getattr(r, "error_type", "") == "NoPermissionError" for r in errors)
    assert metrics["groups"].count("failed") >= 2


async def test_run_pass_marks_error_and_logs_when_valkey_unreachable(
    dal: Any, caplog: pytest.LogCaptureFixture, metrics: dict[str, _Counters]
) -> None:
    """A dead Valkey: the pass logs at ERROR with a traceback, counts `error`, never raises."""
    await _seed(dal, slug=_uniq("t"), community=_uniq("c"), apps=["waddles.core.example.ping"])

    def dead() -> Any:
        return aioredis.from_url(
            f"redis://127.0.0.1:{_free_port()}/0", socket_connect_timeout=0.5, decode_responses=True
        )

    with caplog.at_level(logging.ERROR, logger=rec.logger.name):
        result = await rec.run_pass(dal, dead)

    assert result is not None and result.failed >= 1 and not result.ok
    assert metrics["passes"].count("error") == 1
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    done = [r for r in caplog.records if r.getMessage().endswith("pass complete")]
    assert done and done[0].groups_failed >= 1 and done[0].bindings_examined >= 1


async def test_run_pass_survives_client_factory_failure(
    dal: Any, caplog: pytest.LogCaptureFixture, metrics: dict[str, _Counters]
) -> None:
    """A plaintext-URL refusal (`ValueError` at build) is an ERROR + `error` pass, not a crash."""

    def boom() -> Any:
        raise ValueError("VALKEY_URL must use rediss://")

    with caplog.at_level(logging.ERROR, logger=rec.logger.name):
        result = await rec.run_pass(dal, boom)

    assert result is None
    assert metrics["passes"].count("error") == 1
    assert any("pass failed" in r.getMessage() for r in caplog.records)


async def test_loop_waits_for_readiness_then_reconciles_and_repeats(
    dal: Any, client: Any, valkey_addr: str
) -> None:
    """Gated until `is_ready`; then reconciles at startup and each interval until cancelled."""
    slug, comm = _uniq("t"), _uniq("c")
    await _seed(dal, slug=slug, community=comm, apps=["waddles.core.example.ping"])
    stream = source_stream_key(slug, comm, "discord", "dg-1")
    ready = {"v": False}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            assert await _groups(client, stream) == set()  # not ready -> nothing yet
            ready["v"] = True
        elif len(sleeps) == 3:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await rec.run_reconcile_loop(
            dal,
            client_factory=lambda: aioredis.from_url(
                f"redis://{valkey_addr}/0", decode_responses=True
            ),
            interval_seconds=7.0,
            is_ready=lambda: ready["v"],
            sleep=fake_sleep,
        )

    assert sleeps == [7.0, 7.0, 7.0]
    assert await _groups(client, stream) == {"waddles.core.example.ping"}


async def test_binding_with_missing_community_is_counted_unresolvable(
    dal: Any, client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-sentinel community id with no community row cannot be keyed: ERROR, never guessed."""
    slug = _uniq("t")
    tenant_id, _ = await _seed(dal, slug=slug, community=None, apps=[])
    async with dal.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO app_source_bindings "
                "(tenant_id, community_id, app_id, platform, source_id) "
                "VALUES (:t, 999999, 'waddles.core.example.orph', 'discord', 'dg-x')"
            ),
            {"t": tenant_id},
        )

    with caplog.at_level(logging.ERROR, logger=rec.logger.name):
        result = await rec.reconcile_once(dal, client)

    assert result.unresolvable >= 1 and not result.ok
    assert any("missing community" in r.getMessage() for r in caplog.records)


async def test_pg_real_schema_incident_reconcile(pg_dal: Any, client: Any) -> None:
    """The reconcile query + incident scenario against the real migrated Postgres schema."""
    old_slug, new_slug, comm = _uniq("global"), _uniq("default"), _uniq("c")
    apps = ["waddles.core.example.ping", "waddles.core.example.roll"]
    tenant_id, _ = await _seed(pg_dal, slug=old_slug, community=comm, apps=apps, catalog=True)
    old_stream = source_stream_key(old_slug, comm, "discord", "dg-1")
    for app in apps:
        await client.xgroup_create(old_stream, app, id="$", mkstream=True)
    async with pg_dal.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET slug = :s WHERE id = :i"), {"s": new_slug, "i": tenant_id}
        )

    result = await rec.reconcile_once(pg_dal, client)

    new_stream = source_stream_key(new_slug, comm, "discord", "dg-1")
    assert result.failed == 0 and result.created >= 2
    assert await _groups(client, new_stream) == set(apps)
