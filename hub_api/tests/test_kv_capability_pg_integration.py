"""`storage.kv` capability recorded in install consent summaries, against a REAL migrated Postgres.

Regression for the alpha outage where every `!`-command answered "Something went wrong updating
the counter storage": bundles declaring the structured `- id: storage.kv` permission were recorded
without the `storage.kv` capability, so the data plane's per-app kv snapshot (built from
`app_install_approvals.summary_json -> capabilities`) denied every kv call (`not_granted`).

Covers the fixed derivation end to end (real install + activation write the capability) and the
`0106_backfill_kv_capability` repair SQL over rows recorded before the fix. A SKIP means docker is
absent -- treat it as a gate FAILURE, never a pass.
"""

# ruff: noqa: F811  -- imported pytest fixtures (`pg_db`/`pg_dal`) are re-bound as test params

from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from sqlalchemy import text

from services.bundle_approval_service import activate_for_community_with_warnings
from tests.test_bundle_activation_commands import _manifest
from tests.test_bundle_activation_service import _install_and_make_available
from tests.test_seed_core_bundles_pg_integration import (  # noqa: F401  # fixtures
    pg_dal,
    pg_db,
)

_SEQ = itertools.count(1)
_KV_DECL = {"id": "storage.kv", "justification": "persists per-community counters"}
_CHAT_DECL = {"id": "chat.send:discord", "justification": "replies to the invoking user"}


def _backfill_module() -> Any:
    """Loads the migration module by path (revision files are not importable packages)."""
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0106_backfill_kv_capability.py"
    )
    spec = importlib.util.spec_from_file_location("backfill_kv_capability", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _publish_and_activate(pg_dal: Any, run_id: int, permissions: list[Any]) -> str:
    """Real GLOBAL install + tenant availability + community activation; returns the app id."""
    app_id = f"waddles.socials.kvcap{run_id}.default"
    async with pg_dal.engine.begin() as conn:
        tenant_id = int(
            (
                await conn.execute(
                    text("INSERT INTO tenants (slug) VALUES (:s) RETURNING id"),
                    {"s": f"kvcap-{run_id}"},
                )
            ).scalar_one()
        )
        community_id = int(
            (
                await conn.execute(
                    text("INSERT INTO communities (tenant_id, name) VALUES (:t, :n) RETURNING id"),
                    {"t": tenant_id, "n": f"c-{run_id}"},
                )
            ).scalar_one()
        )
        await conn.execute(
            text("INSERT INTO hub_users (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        )
        await conn.execute(
            text("INSERT INTO app_catalog (app_id) VALUES (:a) ON CONFLICT (app_id) DO NOTHING"),
            {"a": app_id},
        )
    manifest = {**_manifest(app_id), "permissions": permissions}
    await _install_and_make_available(pg_dal, tenant_id=tenant_id, manifest=manifest)
    await activate_for_community_with_warnings(
        pg_dal,
        tenant_id=tenant_id,
        community_id=community_id,
        app_id=app_id,
        activated_by=None,
        valkey_client=AsyncMock(),
    )
    return app_id


async def _capabilities(pg_dal: Any, table: str, app_id: str) -> list[str]:
    """`summary_json -> capabilities` of the live row for `app_id` in `table`."""
    async with pg_dal.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    f"SELECT summary_json -> 'capabilities' FROM {table} "  # noqa: S608 - fixed names
                    "WHERE app_id = :a AND superseded_by IS NULL"
                ),
                {"a": app_id},
            )
        ).scalar_one()
    return list(row)


async def _strip_kv(pg_dal: Any, app_id: str) -> None:
    """Rewrites both summaries to the pre-fix shape (no `storage.kv`)."""
    async with pg_dal.engine.begin() as conn:
        for table in ("app_install_approvals", "app_global_installs"):
            await conn.execute(
                text(
                    f"UPDATE {table} SET summary_json = jsonb_set("  # noqa: S608
                    "summary_json, '{capabilities}', "
                    "(summary_json -> 'capabilities') - 'storage.kv') WHERE app_id = :a"
                ),
                {"a": app_id},
            )


async def _run_backfill(pg_dal: Any) -> None:
    mod = _backfill_module()
    async with pg_dal.engine.begin() as conn:
        for table in ("app_install_approvals", "app_global_installs"):
            await conn.execute(text(mod.backfill_sql(table)))


async def test_structured_storage_kv_is_recorded_in_both_consent_summaries(
    pg_dal: Any,
) -> None:
    """The fixed derivation writes `storage.kv` for the structured declaration shape."""
    app_id = await _publish_and_activate(pg_dal, next(_SEQ), [_KV_DECL, _CHAT_DECL])
    for table in ("app_install_approvals", "app_global_installs"):
        assert "storage.kv" in await _capabilities(pg_dal, table, app_id), table


async def test_a_bundle_that_does_not_declare_kv_is_never_granted_it(pg_dal: Any) -> None:
    """Undeclared still means denied -- the fix widens nothing for a manifest without kv."""
    app_id = await _publish_and_activate(pg_dal, next(_SEQ), [_CHAT_DECL])
    for table in ("app_install_approvals", "app_global_installs"):
        assert "storage.kv" not in await _capabilities(pg_dal, table, app_id), table
    await _run_backfill(pg_dal)
    for table in ("app_install_approvals", "app_global_installs"):
        assert "storage.kv" not in await _capabilities(pg_dal, table, app_id), table


async def test_backfill_repairs_rows_recorded_before_the_fix_and_is_idempotent(
    pg_dal: Any,
) -> None:
    """Pre-fix rows (declared kv, capability missing) are repaired; a rerun changes nothing."""
    app_id = await _publish_and_activate(pg_dal, next(_SEQ), [_KV_DECL, _CHAT_DECL])
    await _strip_kv(pg_dal, app_id)
    for table in ("app_install_approvals", "app_global_installs"):
        assert "storage.kv" not in await _capabilities(pg_dal, table, app_id), table

    await _run_backfill(pg_dal)
    repaired = {
        t: await _capabilities(pg_dal, t, app_id)
        for t in ("app_install_approvals", "app_global_installs")
    }
    for table, caps in repaired.items():
        assert "storage.kv" in caps, table
        assert caps == sorted(set(caps)), f"{table} capabilities must stay sorted and unique"

    await _run_backfill(pg_dal)
    for table, caps in repaired.items():
        assert await _capabilities(pg_dal, table, app_id) == caps, f"{table} not idempotent"


async def test_backfill_repairs_a_bare_string_declaration_too(pg_dal: Any) -> None:
    """The legacy bare-string `permissions: [storage.kv]` shape is repaired the same way."""
    app_id = await _publish_and_activate(pg_dal, next(_SEQ), ["storage.kv"])
    await _strip_kv(pg_dal, app_id)
    await _run_backfill(pg_dal)
    assert "storage.kv" in await _capabilities(pg_dal, "app_install_approvals", app_id)
