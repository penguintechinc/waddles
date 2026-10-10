"""Ban-evasion suppression: per-tenant keyed one-way restriction codes (GDPR Art. 6(1)(f)).

`suppression_records` is the ONLY thing Waddles keeps about a user who erases their
account while they have an *active* ban (`services/ban_suppression.py`); a user with no
active restriction leaves nothing behind -- no tombstone, no reputation. Columns:

- ``tenant_id`` -- the controller. A code only ever matches inside the tenant that wrote
  it (Waddles is a processor per tenant; a cross-tenant ban list would make it a
  controller). ``ON DELETE CASCADE``: deleting a tenant deletes its suppression list.
- ``platform`` -- the platform name only (``discord``, ``twitch``), never an id.
- ``key_ver`` -- which master key produced the code (``sha256(key)[:8]`` hex), so the
  platform KEK can rotate without orphaning rows (`services/suppression_keys.py`).
- ``hmac`` -- ``HMAC-SHA256(HKDF(K_master, tenant_id), platform || platform_user_id)``,
  64 lowercase hex chars. Keyed and per-tenant, NOT a salted hash: without ``K_master``
  the value can neither be reversed nor even tested against a guessed id.
- ``reason_enum`` -- a CLOSED vocabulary of restriction types (``ban``, ``auto_ban``).
  Deliberately no free text and no offence detail (Art. 9/10 special-category and
  criminal-offence data). Widening the vocabulary is a migration, i.e. a reviewed change.
- ``expires_at`` -- the restriction's expiry; NULL = until lifted. A row is deleted at
  expiry (`ban_suppression.purge_expired`) or when a moderator lifts the restriction.
- ``created_at`` -- when retention began (storage-limitation evidence).

There is deliberately NO column that identifies the person (no hub user id, no uuid, no
display name, no community id): the table cannot be joined back to anyone without the
master key.

Hub-api-only privileges (explicit empty rows for every other role in
`config/postgres/rbac-matrix.yaml`): the key never leaves hub-api, so no other service
can compute or test a code either.

Chain note: ``down_revision`` is the tail of the open-PR migration chain when written
(``0059_streaming_codec_columns``, #799; 0055-0059 are #785, #787, #798, #800, #799 in
that order, so this revision is red on the single-head check until they land). Re-point
at the then-current head right before merging
(``bash scripts/ci/check-alembic-single-head.sh``).

Revision ID: 0060_suppression_records
Revises: 0059_streaming_codec_columns
Create Date: 2026-10-10
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0060_suppression_records"
down_revision = "0059_streaming_codec_columns"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"suppression_records"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0060", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0060"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Create `suppression_records` and apply the RBAC matrix grants (hub-api only)."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS suppression_records (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            platform VARCHAR(50) NOT NULL,
            key_ver VARCHAR(32) NOT NULL,
            hmac VARCHAR(64) NOT NULL CHECK (hmac ~ '^[0-9a-f]{64}$'),
            reason_enum VARCHAR(16) NOT NULL CHECK (reason_enum IN ('ban', 'auto_ban')),
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_suppression_records_code UNIQUE (tenant_id, platform, key_ver, hmac)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE suppression_records IS "
        "'Ban-evasion suppression (GDPR Art. 6(1)(f), counsel to verify). One row per erased "
        "user who had an ACTIVE restriction: a per-tenant keyed one-way code of the platform "
        "id (HMAC-SHA256 under an HKDF-derived tenant key), a closed restriction-type enum "
        "and an expiry. No person identifier, no offence detail. Pseudonymised = still "
        "personal data; deleted at expiry or when lifted. Hub-api only.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_suppression_records_expiry "
        "ON suppression_records (expires_at) WHERE expires_at IS NOT NULL"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute("GRANT USAGE ON SEQUENCE suppression_records_id_seq TO hub_api;")


def downgrade() -> None:
    """Drop the table (the suppression list is derived state; erasure never needs it back)."""
    op.execute("DROP TABLE IF EXISTS suppression_records")
