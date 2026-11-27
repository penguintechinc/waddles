"""Account MFA: TOTP (preferred) + email one-time code, recovery codes, MFA-pending challenges.

Three tables, all keyed by `hub_users.id` and erased with the user (`ON DELETE CASCADE` here,
explicit DELETE rules in `services/erasure_registry.py`):

* `hub_user_mfa` -- one row per user: the TOTP secret (envelope-encrypted, `wenv1.` wire form,
  never plaintext), its status code and the last accepted TOTP step (replay prevention), plus the
  email-method status. Status codes (SMALLINT): 0 none, 1 pending (enrolled, not yet confirmed
  with a first code), 2 active.
* `hub_user_mfa_recovery_codes` -- ten single-use recovery codes per user, stored as SHA-256 of
  the normalized code (the codes are 50-bit random, shown to the user exactly once).
* `hub_mfa_challenges` -- short-lived MFA-pending state after primary auth (purpose 1 = login)
  and account-action email codes (purpose 2). The bearer value is stored only as a SHA-256
  hash; an emailed code is stored only as a salted SHA-256 hash with its own expiry, send count
  and the shared failed-attempt counter.

Revision ID: 0145_hub_user_mfa
Revises: 0144_ingest_owner_public_ids
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0145_hub_user_mfa"
down_revision = "0144_ingest_owner_public_ids"
branch_labels = None
depends_on = None

UPGRADE_SQL = (
    """
CREATE TABLE IF NOT EXISTS hub_user_mfa (
    id                 BIGSERIAL    PRIMARY KEY,
    user_id            INTEGER      NOT NULL UNIQUE REFERENCES hub_users(id) ON DELETE CASCADE,
    totp_secret_enc    TEXT,
    totp_status        SMALLINT     NOT NULL DEFAULT 0,
    totp_last_step     BIGINT,
    totp_confirmed_at  TIMESTAMPTZ,
    email_status       SMALLINT     NOT NULL DEFAULT 0,
    email_confirmed_at TIMESTAMPTZ,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_hub_user_mfa_totp_status CHECK (totp_status IN (0, 1, 2)),
    CONSTRAINT ck_hub_user_mfa_email_status CHECK (email_status IN (0, 1, 2))
)
""",
    """
CREATE TABLE IF NOT EXISTS hub_user_mfa_recovery_codes (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    INTEGER     NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    code_hash  VARCHAR(64) NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
""",
    """
CREATE INDEX IF NOT EXISTS idx_hub_user_mfa_recovery_codes_user
    ON hub_user_mfa_recovery_codes (user_id)
""",
    """
CREATE TABLE IF NOT EXISTS hub_mfa_challenges (
    id                    BIGSERIAL    PRIMARY KEY,
    public_id             UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    user_id               INTEGER      NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    purpose               SMALLINT     NOT NULL,
    token_hash            VARCHAR(64)  NOT NULL UNIQUE,
    tenant_id             INTEGER,
    tenant_slug           VARCHAR(100),
    auth_method           VARCHAR(32),
    attempts              SMALLINT     NOT NULL DEFAULT 0,
    email_code_hash       VARCHAR(64),
    email_code_salt       VARCHAR(32),
    email_code_expires_at TIMESTAMPTZ,
    email_sends           SMALLINT     NOT NULL DEFAULT 0,
    email_last_sent_at    TIMESTAMPTZ,
    expires_at            TIMESTAMPTZ  NOT NULL,
    consumed_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_hub_mfa_challenges_purpose CHECK (purpose IN (1, 2))
)
""",
    """
CREATE INDEX IF NOT EXISTS idx_hub_mfa_challenges_user
    ON hub_mfa_challenges (user_id, purpose)
""",
)


def upgrade() -> None:
    """Create the three MFA tables (idempotent)."""
    for stmt in UPGRADE_SQL:
        op.execute(stmt)


def downgrade() -> None:
    """Drop the MFA tables (enrolled users fall back to single-factor login)."""
    op.execute("DROP TABLE IF EXISTS hub_mfa_challenges")
    op.execute("DROP TABLE IF EXISTS hub_user_mfa_recovery_codes")
    op.execute("DROP TABLE IF EXISTS hub_user_mfa")
