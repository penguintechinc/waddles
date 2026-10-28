"""Command registry (`app_commands`) end-to-end against a REAL, fully-migrated Postgres.

The sqlite unit tests cannot see migration 0062's real constraints (FK to `app_catalog` /
`tenants`, the `command`/`command_types`/`platforms` CHECKs, the unique scope key) or real
transaction isolation. These tests drive activation, deactivation, ownership and the backfill
through `bundle_approval_service` over the `pg_db` container (migrated to alembic `head`).
A SKIP means docker is absent -- treat it as a gate FAILURE, never a pass.

Each test gets its own tenant (unique slug) and unique app ids so the module-scoped container
can be shared without cross-test registry bleed.
"""

# ruff: noqa: F811  -- imported pytest fixtures (`pg_db`/`pg_dal`) are re-bound as test params

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from services.app_command_backfill import backfill_active_commands
from services.app_command_service import (
    WARNING_COMMAND_CONFLICT,
    command_owner,
)
from services.bundle_approval_service import (
    activate_for_community_with_warnings,
    activate_tenant_wide_with_warnings,
    deactivate_for_community,
)
from tests.test_bundle_activation_commands import _manifest
from tests.test_bundle_activation_service import _install_and_make_available
from tests.test_seed_core_bundles_pg_integration import (  # noqa: F401  # fixtures
    pg_dal,
    pg_db,
)

_SEQ = itertools.count(1)


@pytest.fixture
def run_id() -> Iterator[int]:
    """Unique-per-test integer used to namespace tenant slug, app ids and community names."""
    yield next(_SEQ)


async def _tenant(pg_dal: Any, run_id: int) -> int:
    """Insert a fresh tenant row and return its numeric id."""
    async with pg_dal.engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text("INSERT INTO tenants (slug) VALUES (:s) RETURNING id"),
                    {"s": f"cmd-reg-{run_id}"},
                )
            ).scalar_one()
        )


async def _community(pg_dal: Any, tenant_id: int, name: str) -> int:
    """Insert a community under `tenant_id` and return its id."""
    async with pg_dal.engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text("INSERT INTO communities (tenant_id, name) VALUES (:t, :n) RETURNING id"),
                    {"t": tenant_id, "n": name},
                )
            ).scalar_one()
        )


async def _publish(pg_dal: Any, tenant_id: int, app_id: str, **kwargs: Any) -> None:
    """Catalog row (the real FK target) + actor + install + tenant availability for `app_id`."""
    async with pg_dal.engine.begin() as conn:
        # the install/availability helpers record `installed_by=1`/`updated_by=1`, real FKs here
        await conn.execute(
            text("INSERT INTO hub_users (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        )
        await conn.execute(
            text("INSERT INTO app_catalog (app_id) VALUES (:a) ON CONFLICT (app_id) DO NOTHING"),
            {"a": app_id},
        )
    await _install_and_make_available(
        pg_dal, tenant_id=tenant_id, manifest=_manifest(app_id, **kwargs)
    )


async def _rows(pg_dal: Any, tenant_id: int, app_id: str | None = None) -> list[Any]:
    """`app_commands` rows for a tenant (optionally one app), ordered by command then id."""
    table = pg_dal.metadata.tables["app_commands"]
    stmt = select(table).where(table.c.tenant_id == tenant_id).order_by(table.c.command, table.c.id)
    if app_id is not None:
        stmt = stmt.where(table.c.app_id == app_id)
    async with pg_dal.engine.connect() as conn:
        return list((await conn.execute(stmt)).all())


async def _activate(pg_dal: Any, tenant_id: int, community_id: int, app_id: str) -> Any:
    return await activate_for_community_with_warnings(
        pg_dal,
        tenant_id=tenant_id,
        community_id=community_id,
        app_id=app_id,
        activated_by=None,
        valkey_client=AsyncMock(),
    )


async def test_seed_then_activate_registers_inferred_rows_with_fk_enforced(
    pg_dal: Any, run_id: int
) -> None:
    """Activation writes inferred rows; the real FK rejects a row for an unknown app_id."""
    tenant_id = await _tenant(pg_dal, run_id)
    community_id = await _community(pg_dal, tenant_id, f"c-{run_id}")
    app_id = f"waddles.socials.fk{run_id}.default"
    await _publish(pg_dal, tenant_id, app_id)

    result = await _activate(pg_dal, tenant_id, community_id, app_id)

    assert result.warnings == ()
    rows = await _rows(pg_dal, tenant_id, app_id)
    assert [r.command for r in rows] == ["ping", "pong"]
    assert all(r.community_id == community_id for r in rows)
    assert all(list(r.platforms) == ["twitch"] and r.command_types == 1 for r in rows)

    table = pg_dal.metadata.tables["app_commands"]
    with pytest.raises(IntegrityError):
        async with pg_dal.engine.begin() as conn:
            await conn.execute(
                table.insert().values(
                    tenant_id=tenant_id,
                    community_id=community_id,
                    app_id=f"waddles.socials.unknown{run_id}.default",
                    command="ghost",
                    command_types=1,
                    platforms=["twitch"],
                )
            )


async def test_two_bundles_same_verb_first_registered_wins_on_real_pg(
    pg_dal: Any, run_id: int
) -> None:
    """Warning names the first app; deactivate + reactivate the first re-queues it behind."""
    tenant_id = await _tenant(pg_dal, run_id)
    community_id = await _community(pg_dal, tenant_id, f"c-{run_id}")
    first = f"waddles.socials.first{run_id}.default"
    second = f"waddles.socials.second{run_id}.default"
    await _publish(pg_dal, tenant_id, first)
    await _publish(pg_dal, tenant_id, second)

    assert (await _activate(pg_dal, tenant_id, community_id, first)).warnings == ()
    contested = (await _activate(pg_dal, tenant_id, community_id, second)).warnings

    assert {w.command for w in contested} == {"ping", "pong"}
    assert all(w.code == WARNING_COMMAND_CONFLICT and w.owner_app_id == first for w in contested)
    assert all(w.app_id == second for w in contested)
    assert (
        await command_owner(pg_dal, tenant_id=tenant_id, community_id=community_id, command="ping")
        == first
    )

    await deactivate_for_community(
        pg_dal, tenant_id=tenant_id, community_id=community_id, app_id=first, deactivated_by=None
    )
    assert await _rows(pg_dal, tenant_id, first) == []
    assert (
        await command_owner(pg_dal, tenant_id=tenant_id, community_id=community_id, command="ping")
        == second
    )

    # Re-queue via a NEW version: same-version reactivation trips the pre-existing
    # `uq_app_install_approvals_current` defect (see the xfail test below).
    await _publish(pg_dal, tenant_id, first, version="1.0.1")
    requeued = (await _activate(pg_dal, tenant_id, community_id, first)).warnings
    assert {w.owner_app_id for w in requeued} == {second}
    assert (
        await command_owner(pg_dal, tenant_id=tenant_id, community_id=community_id, command="ping")
        == second
    )


@pytest.mark.xfail(
    strict=True,
    raises=IntegrityError,
    reason=(
        "PRE-EXISTING defect (not the command registry): `_write_approval_and_activate` INSERTs "
        "the new approval before superseding the previous one, so re-activating the same "
        "app+version+community violates partial unique index uq_app_install_approvals_current "
        "on real Postgres (sqlite has no such index). Flips to XPASS-fail once fixed."
    ),
)
async def test_same_version_reactivation_on_real_pg(pg_dal: Any, run_id: int) -> None:
    """Deactivate then re-activate the SAME version -- must work, currently IntegrityError."""
    tenant_id = await _tenant(pg_dal, run_id)
    community_id = await _community(pg_dal, tenant_id, f"c-{run_id}")
    app_id = f"waddles.socials.again{run_id}.default"
    await _publish(pg_dal, tenant_id, app_id)
    await _activate(pg_dal, tenant_id, community_id, app_id)
    await deactivate_for_community(
        pg_dal, tenant_id=tenant_id, community_id=community_id, app_id=app_id, deactivated_by=None
    )

    await _activate(pg_dal, tenant_id, community_id, app_id)


async def test_tenant_wide_and_community_rows_coexist(pg_dal: Any, run_id: int) -> None:
    """A tenant-wide (scope 0) owner and a community activation conflict across scope."""
    tenant_id = await _tenant(pg_dal, run_id)
    community_id = await _community(pg_dal, tenant_id, f"c-{run_id}")
    wide = f"waddles.socials.wide{run_id}.default"
    scoped = f"waddles.socials.scoped{run_id}.default"
    await _publish(pg_dal, tenant_id, wide)
    await _publish(pg_dal, tenant_id, scoped)

    wide_result = await activate_tenant_wide_with_warnings(
        pg_dal, tenant_id=tenant_id, app_id=wide, activated_by=None, valkey_client=AsyncMock()
    )
    assert wide_result.warnings == ()
    assert {r.community_id for r in await _rows(pg_dal, tenant_id, wide)} == {0}

    result = await _activate(pg_dal, tenant_id, community_id, scoped)

    assert {w.command for w in result.warnings} == {"ping", "pong"}
    assert all(w.owner_app_id == wide for w in result.warnings)
    assert {r.community_id for r in await _rows(pg_dal, tenant_id, scoped)} == {community_id}
    assert (
        await command_owner(pg_dal, tenant_id=tenant_id, community_id=community_id, command="pong")
        == wide
    )


async def test_concurrent_activations_never_produce_two_owners(pg_dal: Any, run_id: int) -> None:
    """Racing activations of two apps on one verb: both succeed, exactly one deterministic owner.

    READ COMMITTED means each racer may miss the other's uncommitted row, so a conflict warning
    can be lost (tolerated). Split ownership is not: the owner must be a single, stable app that
    equals the earliest `(registered_at, id)` row.
    """
    tenant_id = await _tenant(pg_dal, run_id)
    community_id = await _community(pg_dal, tenant_id, f"c-{run_id}")
    app_a = f"waddles.socials.racea{run_id}.default"
    app_b = f"waddles.socials.raceb{run_id}.default"
    await _publish(pg_dal, tenant_id, app_a)
    await _publish(pg_dal, tenant_id, app_b)

    results = await asyncio.gather(
        _activate(pg_dal, tenant_id, community_id, app_a),
        _activate(pg_dal, tenant_id, community_id, app_b),
    )

    assert len(results) == 2  # both activations succeeded (gather re-raises otherwise)
    rows = await _rows(pg_dal, tenant_id)
    assert {r.app_id for r in rows} == {app_a, app_b}
    for verb in ("ping", "pong"):
        owner = await command_owner(
            pg_dal, tenant_id=tenant_id, community_id=community_id, command=verb
        )
        again = await command_owner(
            pg_dal, tenant_id=tenant_id, community_id=community_id, command=verb
        )
        by_verb = sorted(
            (r for r in rows if r.command == verb), key=lambda r: (r.registered_at, r.id)
        )
        assert owner in {app_a, app_b}
        assert owner == again == by_verb[0].app_id  # single deterministic owner
    # A lost warning is allowed; a warning, if present, must name the actual owner.
    for result in results:
        for warning in result.warnings:
            assert warning.owner_app_id != warning.app_id
            assert warning.owner_app_id in {app_a, app_b}


async def test_backfill_on_real_pg_is_idempotent(pg_dal: Any, run_id: int) -> None:
    """Active rows with no registry rows -> backfill twice -> same rows, same registered_at."""
    tenant_id = await _tenant(pg_dal, run_id)
    community_id = await _community(pg_dal, tenant_id, f"c-{run_id}")
    app_id = f"waddles.socials.fill{run_id}.default"
    await _publish(pg_dal, tenant_id, app_id)
    await _activate(pg_dal, tenant_id, community_id, app_id)
    async with pg_dal.engine.begin() as conn:  # simulate the pre-registry state
        await conn.execute(text("DELETE FROM app_commands WHERE tenant_id = :t"), {"t": tenant_id})
    assert await _rows(pg_dal, tenant_id) == []

    first = await backfill_active_commands(pg_dal, now=datetime(2030, 1, 1, tzinfo=UTC))
    assert first.examined >= 1 and first.failed == ()
    after_first = [(r.id, r.command, r.registered_at) for r in await _rows(pg_dal, tenant_id)]
    assert [c for _, c, _ in after_first] == ["ping", "pong"]

    active = pg_dal.metadata.tables["app_active_versions"]
    async with pg_dal.engine.connect() as conn:
        activated_at = (
            await conn.execute(
                select(active.c.activated_at).where(
                    (active.c.tenant_id == tenant_id) & (active.c.app_id == app_id)
                )
            )
        ).scalar_one()
    assert all(registered == activated_at for _, _, registered in after_first)

    second = await backfill_active_commands(pg_dal, now=datetime(2031, 1, 1, tzinfo=UTC))
    assert second.failed == ()
    after_second = [(r.id, r.command, r.registered_at) for r in await _rows(pg_dal, tenant_id)]
    assert after_second == after_first
