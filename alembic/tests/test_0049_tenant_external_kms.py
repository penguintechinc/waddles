"""Tests for 0049_tenant_external_kms (per-tenant key store + Enterprise external-KMS config).

Text-level (no database), same split as the sibling migration tests: revision metadata and the
single-head invariant, the emitted DDL/GRANT shape, and the downgrade's deliberate refusal to
drop the key store. The same SQL is executed for real -- constraints, the one-active-key unique
index, and the per-role RBAC grants -- by ``hub_api/tests/envelope/test_repository_pg.py``
against a fully migrated Postgres.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _ALEMBIC_DIR.parent
_VERSIONS = _ALEMBIC_DIR / "versions"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_0049_tenant_external_kms", _VERSIONS / "0049_tenant_external_kms.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load()


def _emitted(migration: ModuleType, fn: str) -> list[str]:
    statements: list[str] = []
    with patch.object(migration.op, "execute", side_effect=statements.append):
        getattr(migration, fn)()
    return [" ".join(s.split()) for s in statements]


def test_revision_metadata_and_a_single_alembic_head(migration: ModuleType) -> None:
    assert migration.revision == "0049_tenant_external_kms"
    assert len(migration.revision) <= 32  # alembic_version.version_num is VARCHAR(32)
    assert migration.down_revision == "0048_identity_forged_uuid"
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1, (
        "two alembic heads: re-point down_revision in merge order"
    )
    assert script.get_revision(migration.revision) is not None


def test_ddl_creates_an_isolated_key_store_and_the_byok_config(
    migration: ModuleType,
) -> None:
    ddl = _emitted(migration, "upgrade")
    joined = "\n".join(ddl)
    assert ddl[0] == "CREATE SCHEMA IF NOT EXISTS keystore"
    assert "CREATE TABLE IF NOT EXISTS keystore.tenant_encryption_keys" in joined
    assert "CREATE TABLE IF NOT EXISTS tenant_kms_configs" in joined
    assert "CHECK (kek_kind IN ('platform', 'customer_kms'))" in joined
    assert "UNIQUE (tenant_id, purpose, dek_version)" in joined
    assert "WHERE status = 'active'" in joined  # one active DEK per tenant and lineage
    assert "REFERENCES tenants(id) ON DELETE CASCADE" in joined
    # No secret ever lands in the config table: only identifiers and the proof-of-control token.
    for forbidden in ("secret", "password", "access_key", "private_key"):
        assert (
            forbidden not in joined.split("tenant_kms_configs")[1].split(")")[0].lower()
        )


def test_grants_make_hub_api_the_only_principal_and_never_allow_key_deletes(
    migration: ModuleType,
) -> None:
    sql = _emitted(migration, "upgrade")
    grants = [
        s
        for s in sql
        if s.startswith("GRANT") and "keystore.tenant_encryption_keys" in s
    ]
    to_hub_api = [
        g
        for g in grants
        if g.rstrip(";").endswith(" TO hub_api") and "SEQUENCE" not in g
    ]
    assert len(to_hub_api) == 1
    for privilege in ("SELECT", "INSERT", "UPDATE"):
        assert privilege in to_hub_api[0]
    assert (
        "DELETE" not in to_hub_api[0]
    )  # keys are retired, never hard-deleted by the app
    for role in ("svc_ingest", "svc_process", "svc_action", "svc_streaming", "webui"):
        assert not any(f" {role}" in g for g in grants), role
    assert "REVOKE ALL ON SCHEMA keystore FROM PUBLIC;" in sql
    assert any(s.startswith("GRANT USAGE ON SCHEMA keystore TO hub_api") for s in sql)


def test_downgrade_never_drops_the_key_store(migration: ModuleType) -> None:
    """Dropping it would irreversibly crypto-shred every tenant's wrapped DEKs."""
    down = _emitted(migration, "downgrade")
    assert "DROP TABLE IF EXISTS tenant_kms_configs" in down
    assert not any(
        "tenant_encryption_keys" in s and s.startswith("DROP TABLE") for s in down
    )
    assert not any("DROP SCHEMA" in s for s in down)


def test_rbac_matrix_lists_both_tables_with_explicit_empty_rows_for_the_data_plane() -> (
    None
):
    import yaml

    matrix = yaml.safe_load(
        (_REPO_ROOT / "config" / "postgres" / "rbac-matrix.yaml").read_text()
    )
    assert "keystore.tenant_encryption_keys" in matrix["tables"]
    assert "tenant_kms_configs" in matrix["tables"]
    by_role = {
        (g["role"], g["table"]): g["privileges"]
        for g in matrix["grants"]
        if g["table"] in ("keystore.tenant_encryption_keys", "tenant_kms_configs")
    }
    for table in ("keystore.tenant_encryption_keys", "tenant_kms_configs"):
        for role in (
            "svc_ingest",
            "svc_process",
            "svc_action",
            "svc_streaming",
            "webui",
        ):
            assert by_role[(role, table)] == []  # explicit, not merely absent
    assert by_role[("hub_api", "keystore.tenant_encryption_keys")] == [
        "SELECT",
        "INSERT",
        "UPDATE",
    ]
