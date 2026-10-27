"""Art. 17 registry against a REAL, migrated Postgres: economy, SSO subject, identity events.

The sqlite suites prove the rules; only Postgres proves what Postgres does to the statements the
registry executor emits through pydal: the `information_schema` catalog read, `rname`-quoted alias
tables, `uuid`-typed columns compared with text tokens (`user_uuid IN (...)`), composite-key tables
with no `id` column (`economy_balances`), and `BIGSERIAL` ids. The tables used here come from the
REAL migrations (`economy_*` 0051, `sso_*` 0049, `identity_resolution_events` 0045); the legacy
tables of the registry are absent from this harness and must be reported absent, not raise.

Skipped (never failed) where the `docker` CLI is unavailable, like every other real-Postgres test.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from flask_core.database import AsyncDAL

from services import erasure_executor as executor
from services import erasure_registry as registry
from services.erasure_executor import ErasureSubject
from tests.test_dsar_member_erasure_pg_integration import (  # noqa: F401 - fixtures re-exported
    _community,
    _hub_user,
    _one,
    _q,
    _tenant,
    adal,
    pg_db,
    pg_only,
)

pytestmark = pg_only


def _seed_sso(dal: Any, tenant_id: int, user_id: int, subject: str) -> None:
    connection_id = _one(
        dal,
        "INSERT INTO sso_connections (public_id, tenant_id, protocol, display_name) "
        "VALUES (%s, %s, 'oidc', %s) RETURNING id",
        [str(uuid.uuid4()), tenant_id, f"idp-{uuid.uuid4().hex[:6]}"],
    )
    _q(
        dal,
        "INSERT INTO sso_identities (connection_id, subject, hub_user_id) VALUES (%s, %s, %s)",
        [connection_id, subject, user_id],
    )


def _seed_economy(dal: Any, tenant_id: int, community_id: int, mine: str, other: str) -> None:
    _q(
        dal,
        "INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) "
        "VALUES (%s, %s, %s::uuid, 10), (%s, %s, %s::uuid, 20)",
        [tenant_id, community_id, mine, tenant_id, community_id, other],
    )
    _q(
        dal,
        "INSERT INTO economy_ledger (tenant_id, community_id, app_id, user_uuid, "
        "counterparty_uuid, kind, delta, balance_after) VALUES "
        "(%s, %s, 'app', %s::uuid, %s::uuid, 'transfer_out', -5, 10), "
        "(%s, %s, 'app', %s::uuid, %s::uuid, 'transfer_in', 5, 20)",
        [tenant_id, community_id, mine, other, tenant_id, community_id, other, mine],
    )


def test_registry_erases_economy_and_sso_on_real_postgres(adal: AsyncDAL) -> None:  # noqa: F811
    dal = adal.dal
    tenant_id = _tenant(dal, "acme")
    community_id = _community(dal, tenant_id)
    user_id, user_uuid = _hub_user(dal, "subject")
    other_id, other_uuid = _hub_user(dal, "bystander")
    _seed_sso(dal, tenant_id, user_id, "idp-subject-sentinel@example.com")
    _seed_sso(dal, tenant_id, other_id, "bystander-subject")
    _seed_economy(dal, tenant_id, community_id, user_uuid, other_uuid)

    result = executor.run_registry_erasure(
        dal, ErasureSubject(user_id=user_id, user_uuids=(user_uuid,))
    )
    dal.commit()

    assert result.counts["sso_identities"] == 1
    assert result.counts["economy_balances"] == 1
    assert result.counts["economy_ledger"] == 1
    assert result.counts[executor.scrubbed_key("economy_ledger")] == 1
    assert _one(dal, "SELECT COUNT(*) FROM sso_identities WHERE hub_user_id = %s", [user_id]) == 0
    assert _one(dal, "SELECT COUNT(*) FROM sso_identities WHERE subject LIKE 'idp-subject%%'") == 0
    assert _one(dal, "SELECT COUNT(*) FROM sso_identities WHERE hub_user_id = %s", [other_id]) == 1
    assert (
        _one(dal, "SELECT COUNT(*) FROM economy_balances WHERE user_uuid = %s::uuid", [user_uuid])
        == 0
    )
    assert (
        _one(dal, "SELECT COUNT(*) FROM economy_balances WHERE user_uuid = %s::uuid", [other_uuid])
        == 1
    )
    # the other side of the transfer survives but no longer names the erased user
    rows = _q(dal, "SELECT user_uuid::text, counterparty_uuid FROM economy_ledger")
    assert [(str(u), c) for u, c in rows] == [(other_uuid, None)]


def test_catalog_and_absent_tables_on_real_postgres(adal: AsyncDAL) -> None:  # noqa: F811
    dal = adal.dal
    catalog = executor.table_catalog(dal)
    assert {"user_uuid", "counterparty_uuid"} <= catalog["economy_ledger"]
    assert "subject" in catalog["sso_identities"]
    result = executor.run_registry_erasure(dal, ErasureSubject(user_id=999_999))
    dal.commit()
    # the harness lacks the legacy tables: reported, never raised
    assert "activity_logs" in result.tables_absent
    assert result.tables_examined + len(result.tables_absent) == len(registry.executed_tables())
    assert result.tables_examined >= 3


def test_an_unlinked_subject_erases_nothing_and_raises_nothing(adal: AsyncDAL) -> None:  # noqa: F811
    dal = adal.dal
    result = executor.run_registry_erasure(dal, ErasureSubject(user_id=123_456))
    dal.commit()
    assert result.counts == {}


@pytest.mark.parametrize("token", ["not-a-uuid", "'; DROP TABLE economy_ledger; --"])
def test_a_malformed_uuid_token_is_dropped_never_queried(adal: AsyncDAL, token: str) -> None:  # noqa: F811
    """A uuid-typed column compared with garbage must neither run SQL nor abort the erasure."""
    dal = adal.dal
    result = executor.run_registry_erasure(dal, ErasureSubject(user_id=1, user_uuids=(token,)))
    dal.commit()
    assert "economy_ledger" not in result.counts  # the malformed token was dropped, not queried
    assert _one(dal, "SELECT to_regclass('economy_ledger') IS NOT NULL") is True
