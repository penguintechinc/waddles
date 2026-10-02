"""Drop the global UNIQUE on `app_versions.artifact_digest`, keep a non-unique index.

# regression: global artifact_digest UNIQUE blocked manifest-only release (alpha 2026-10-02)

A manifest-only release of the core `ping` bundle (1.0.2 -> 1.0.3, the
manifest gained a `discord` `consumes` rule) rebuilt to byte-identical
wasm. `_publish_prebuilt_version()`'s INSERT of the 1.0.3 row then
violated migration 0022's `UNIQUE (artifact_digest)`, the seeder hook
failed, and the alpha deploy failed.

That constraint conflated content-addressed storage with versioning:
the same artifact bytes can legitimately map to several `(app_id,
version)` rows (a manifest-only change, a version bump with no source
change), and two different apps/tenants can independently produce
identical bytes. Uniqueness belongs on `(app_id, version)` -- already
present as `app_versions_app_id_version_key` from migration 0022, kept
here for new-constraint-name parity in case a prior environment
diverged, re-added only if genuinely absent (checked by column set,
not by name) -- and NOT on the digest alone. `artifact_digest` keeps a
plain, non-unique index: every reader in this codebase
(`bundle_versions.py::get_version`, `seed_core_bundles.py`'s
idempotency check, the Rust `bundle_active_set`/`svc_action`/
`svc_process` readers) already resolves a row by `id`/`version_id`/
`(app_id, version)` first and only reads `artifact_digest` off that
already-scoped row -- never a bare digest-only lookup -- so dropping
the uniqueness guarantee changes no query's correctness, only removes
the spurious write-time rejection.

Revision ID: 0033_artifact_digest_not_unique
Revises: 0032_bundle_reader_role
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033_artifact_digest_not_unique"
down_revision = "0032_bundle_reader_role"
branch_labels = None
depends_on = None

_OLD_DIGEST_UNIQUE_CONSTRAINT = "app_versions_artifact_digest_key"
_DIGEST_INDEX = "idx_app_versions_artifact_digest"
_APP_ID_VERSION_UNIQUE_CONSTRAINT = "app_versions_app_id_version_key"


def upgrade() -> None:
    # Drop the global digest-uniqueness guarantee -- the actual bug.
    op.execute(
        f"ALTER TABLE app_versions DROP CONSTRAINT IF EXISTS {_OLD_DIGEST_UNIQUE_CONSTRAINT}"
    )
    # Keep a plain index: every reader still filters/joins on artifact_digest
    # for display (bundle_versions.py) and idempotency comparison
    # (seed_core_bundles.py) -- just never as the sole uniqueness key.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_DIGEST_INDEX} ON app_versions (artifact_digest)"
    )

    # Defensive: make sure UNIQUE(app_id, version) exists. Migration 0022
    # already creates it inline as app_versions_app_id_version_key, so this
    # is normally a no-op -- checked by actual column set (not by constraint
    # name) so a differently-named equivalent constraint in a drifted
    # environment is still recognized instead of causing a duplicate add.
    op.execute(
        f"""
        DO $$
        DECLARE
            has_constraint boolean;
        BEGIN
            SELECT EXISTS (
                SELECT 1
                FROM pg_constraint c
                WHERE c.conrelid = 'app_versions'::regclass
                  AND c.contype = 'u'
                  AND (
                      SELECT array_agg(a.attname ORDER BY a.attname)
                      FROM unnest(c.conkey) AS k(attnum)
                      JOIN pg_attribute a
                        ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                  ) = ARRAY['app_id', 'version']::name[]
            ) INTO has_constraint;

            IF NOT has_constraint THEN
                ALTER TABLE app_versions
                    ADD CONSTRAINT {_APP_ID_VERSION_UNIQUE_CONSTRAINT} UNIQUE (app_id, version);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_DIGEST_INDEX}")

    # Restoring the global UNIQUE is only safe if no two rows already share
    # a digest -- exactly the scenario this migration exists to allow
    # (a manifest-only release reusing byte-identical wasm). Fail loudly
    # with the offending digest rather than silently dropping data or
    # raising an opaque constraint-violation from Postgres itself.
    conn = op.get_bind()
    dup = conn.execute(
        sa.text(
            "SELECT artifact_digest, COUNT(*) AS n FROM app_versions "
            "WHERE artifact_digest IS NOT NULL "
            "GROUP BY artifact_digest HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).fetchone()
    if dup is not None:
        raise RuntimeError(
            f"cannot downgrade 0033_artifact_digest_not_unique: artifact_digest "
            f"{dup[0]!r} is shared by {dup[1]} app_versions rows -- the old global "
            "UNIQUE(artifact_digest) constraint cannot be restored until duplicates "
            "are resolved (merge/retire the extra version rows first)"
        )
    op.execute(
        f"ALTER TABLE app_versions "
        f"ADD CONSTRAINT {_OLD_DIGEST_UNIQUE_CONSTRAINT} UNIQUE (artifact_digest)"
    )
