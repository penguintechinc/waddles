"""`marketplace_sources` -- external git-backed bundle sources (marketplace Phase 1, Task 1).

A source is a pinned git repo that supplies bundles. Design: docs/superpowers/specs/
2026-10-27-unified-bundle-marketplace-sources-design.md section 4.1.

- Entity identity: BIGINT surrogate `id` (internal) + `public_id` UUID (API/URLs/logs).
- Int enum codes, never strings: `build_mode` 1 prebuilt / 2 sandbox / 3 builtin-image;
  `status` 1 pending-scan / 2 active / 3 needs-approval / 4 disabled / 5 error.
- CHECKs: https-only `git_url` (no userinfo), 40-hex SHAs, prebuilt needs a publisher key,
  sandbox needs the full trust ack, exactly one `is_builtin` row (partial unique index) which is
  the only `build_mode=3` row and has no `git_url`.
- The built-in sentinel row is seeded here and is immutable (trigger: no delete, no change of
  url/mode/flag); `status` stays writable so the `waddles.disable-builtin-source` kill-switch works.
- Audit trigger -> `marketplace_sources_audit_log` (mirrors `app_versions_audit_log`, 0022).
- `source_id` nullable FK on `app_versions` / `app_catalog` (NULL = pre-source row; the built-in
  backfill is a later task). `created_by` / `sandbox_ack_by` are plain INTEGER hub user ids (no
  FK: an audit reference must survive user deactivation, and the sandbox CHECK forbids SET NULL).

Revision ID: 0067_marketplace_sources
Revises: 0066_one_time_secrets
Create Date: 2026-10-27
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0067_marketplace_sources"
down_revision = "0066_one_time_secrets"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset({"marketplace_sources", "marketplace_sources_audit_log"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    """Load `scripts/db/rbac_matrix.py` by path (it is not an importable package)."""
    spec = importlib.util.spec_from_file_location(
        "waddles_rbac_matrix_0067", _MATRIX_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0067"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Create the source table, guards, audit trigger, built-in row, FKs and grants."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS marketplace_sources (
            id BIGSERIAL PRIMARY KEY,
            public_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            name VARCHAR(100) NOT NULL CHECK (length(btrim(name)) > 0),
            is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
            git_url TEXT,
            ref VARCHAR(255),
            pinned_commit_sha CHAR(40),
            pending_commit_sha CHAR(40),
            build_mode SMALLINT NOT NULL,
            publisher_key_fingerprint VARCHAR(128),
            publisher_public_key TEXT,
            sandbox_ack_by INTEGER,
            sandbox_ack_at TIMESTAMPTZ,
            sandbox_ack_text_hash CHAR(64),
            sandbox_ack_commit_sha CHAR(40),
            status SMALLINT NOT NULL DEFAULT 1,
            last_scanned_at TIMESTAMPTZ,
            last_scan_error TEXT,
            created_by INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_marketplace_sources_build_mode CHECK (build_mode IN (1, 2, 3)),
            CONSTRAINT ck_marketplace_sources_status CHECK (status BETWEEN 1 AND 5),
            CONSTRAINT ck_marketplace_sources_builtin_shape CHECK (
                is_builtin = (build_mode = 3)
                AND (NOT is_builtin OR (git_url IS NULL AND ref IS NULL))
            ),
            CONSTRAINT ck_marketplace_sources_git_url_https CHECK (
                is_builtin OR (git_url ~ '^https://[^/[:space:]@]+(/[^[:space:]]*)?$' AND ref IS NOT NULL)
            ),
            CONSTRAINT ck_marketplace_sources_pinned_sha CHECK (
                pinned_commit_sha IS NULL OR pinned_commit_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_marketplace_sources_pending_sha CHECK (
                pending_commit_sha IS NULL OR pending_commit_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_marketplace_sources_ack_sha CHECK (
                sandbox_ack_commit_sha IS NULL OR sandbox_ack_commit_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_marketplace_sources_prebuilt_key CHECK (
                build_mode <> 1
                OR (publisher_key_fingerprint IS NOT NULL AND publisher_public_key IS NOT NULL)
            ),
            CONSTRAINT ck_marketplace_sources_sandbox_ack CHECK (
                build_mode <> 2
                OR (
                    sandbox_ack_by IS NOT NULL AND sandbox_ack_at IS NOT NULL
                    AND sandbox_ack_text_hash IS NOT NULL AND sandbox_ack_commit_sha IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_sources_one_builtin "
        "ON marketplace_sources (is_builtin) WHERE is_builtin"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_marketplace_sources_status ON marketplace_sources (status)"
    )
    op.execute(
        "COMMENT ON TABLE marketplace_sources IS "
        "'External git-backed bundle sources. build_mode: 1 prebuilt, 2 sandbox, 3 builtin-image. "
        "status: 1 pending-scan, 2 active, 3 needs-approval, 4 disabled, 5 error. Exactly one "
        "immutable is_builtin row. pinned_commit_sha is the only thing ever cloned/built.'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS marketplace_sources_audit_log (
            id BIGSERIAL PRIMARY KEY,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            db_role VARCHAR(100) NOT NULL,
            operation VARCHAR(10) NOT NULL,
            source_public_id UUID NOT NULL,
            old_status SMALLINT,
            new_status SMALLINT,
            old_build_mode SMALLINT,
            new_build_mode SMALLINT,
            old_git_url TEXT,
            new_git_url TEXT,
            old_pinned_commit_sha CHAR(40),
            new_pinned_commit_sha CHAR(40)
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_marketplace_sources_audit() RETURNS trigger AS $$
        BEGIN
            INSERT INTO public.marketplace_sources_audit_log (
                db_role, operation, source_public_id,
                old_status, new_status, old_build_mode, new_build_mode,
                old_git_url, new_git_url, old_pinned_commit_sha, new_pinned_commit_sha
            ) VALUES (
                session_user, TG_OP, COALESCE(NEW.public_id, OLD.public_id),
                CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.status END,
                CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.status END,
                CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.build_mode END,
                CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.build_mode END,
                CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.git_url END,
                CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.git_url END,
                CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.pinned_commit_sha END,
                CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.pinned_commit_sha END
            );
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_marketplace_sources_audit ON marketplace_sources;
        CREATE TRIGGER trg_marketplace_sources_audit
            AFTER INSERT OR UPDATE OR DELETE ON marketplace_sources
            FOR EACH ROW EXECUTE FUNCTION fn_marketplace_sources_audit()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_marketplace_sources_builtin_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.is_builtin THEN
                    RAISE EXCEPTION 'the built-in marketplace source cannot be deleted';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.is_builtin AND (
                NEW.is_builtin IS DISTINCT FROM OLD.is_builtin
                OR NEW.git_url IS DISTINCT FROM OLD.git_url
                OR NEW.build_mode IS DISTINCT FROM OLD.build_mode
            ) THEN
                RAISE EXCEPTION 'the built-in marketplace source is immutable (url/mode/flag)';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_marketplace_sources_builtin_guard ON marketplace_sources;
        CREATE TRIGGER trg_marketplace_sources_builtin_guard
            BEFORE UPDATE OR DELETE ON marketplace_sources
            FOR EACH ROW EXECUTE FUNCTION fn_marketplace_sources_builtin_guard()
        """
    )

    op.execute(
        "INSERT INTO marketplace_sources (name, is_builtin, build_mode, status) "
        "SELECT 'Waddles (built-in)', TRUE, 3, 2 "
        "WHERE NOT EXISTS (SELECT 1 FROM marketplace_sources WHERE is_builtin)"
    )

    for table in ("app_versions", "app_catalog"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS source_id BIGINT "
            "REFERENCES marketplace_sources(id)"
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_source_id ON {table} (source_id)"
        )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get(
        "RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH)
    )
    rows = matrix_module.load_matrix(matrix_path)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)
    op.execute("GRANT USAGE ON SEQUENCE marketplace_sources_id_seq TO hub_api;")
    op.execute(
        "GRANT USAGE ON SEQUENCE marketplace_sources_audit_log_id_seq TO hub_api;"
    )


def downgrade() -> None:
    """Drop the FK columns, then the audit/guard triggers, audit log and source table."""
    for table in ("app_catalog", "app_versions"):
        op.execute(f"DROP INDEX IF EXISTS idx_{table}_source_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS source_id")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_marketplace_sources_builtin_guard ON marketplace_sources"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_marketplace_sources_audit ON marketplace_sources"
    )
    op.execute("DROP FUNCTION IF EXISTS fn_marketplace_sources_builtin_guard()")
    op.execute("DROP FUNCTION IF EXISTS fn_marketplace_sources_audit()")
    op.execute("DROP TABLE IF EXISTS marketplace_sources_audit_log")
    op.execute("DROP TABLE IF EXISTS marketplace_sources")
