"""Enterprise SSO: per-tenant IdP connections + external-identity links.

Backs the SAML 2.0 / OIDC (Enterprise tier, `waddles.auth.sso_saml`) and
Google OAuth2 (Professional tier, `waddles.auth.sso_google`) login flows in
`hub_api/services/sso_service.py`.

Tables:

- `sso_connections` -- one row per identity provider a tenant admin has
  configured. `public_id` is the opaque UUID that appears in the public
  login/callback/ACS URLs and in log lines (never the integer `id`, never
  an email). `config` holds only NON-secret protocol settings (issuer, IdP
  SSO URL, signing certificates, allowed email domains, ...); the one secret
  a connection can carry (an OIDC/Google `client_secret`) lives in
  `secret_ciphertext`, AES-256-GCM encrypted under the auto-provisioned
  `SSO_ENCRYPTION_KEY` with the connection's `public_id` bound as AAD (see
  `hub_api/services/sso_crypto.py`). `enabled` defaults FALSE: a connection
  is inert until a tenant admin deliberately turns it on.

- `sso_identities` -- links an IdP-asserted subject (OIDC `sub` / SAML
  `NameID`) to a `hub_users` row, scoped per connection so the same subject
  string from two different IdPs can never collide. `subject` may be an
  email address (SAML emailAddress NameID) and is therefore treated as PII:
  the table is hub-api-only (explicit empty grants for every other role in
  `config/postgres/rbac-matrix.yaml`) and cascades on `hub_users` deletion
  so an erasure request leaves nothing behind.

Login never adopts an existing `hub_users` row by email -- see
`sso_service.complete_login` -- so there is deliberately no email column
here.

Revision ID: 0049_sso_connections
Revises: 0048_identity_forged_uuid
Create Date: 2026-10-10
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0049_sso_connections"
down_revision = "0048_identity_forged_uuid"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"sso_connections", "sso_identities"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0049", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0049"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Create `sso_connections` + `sso_identities` and apply the RBAC matrix grants."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sso_connections (
            id BIGSERIAL PRIMARY KEY,
            public_id VARCHAR(36) NOT NULL UNIQUE,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            protocol VARCHAR(16) NOT NULL
                CHECK (protocol IN ('saml', 'oidc', 'google')),
            display_name VARCHAR(100) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            config JSONB NOT NULL DEFAULT '{}'::jsonb,
            secret_ciphertext TEXT,
            created_by_user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, display_name)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE sso_connections IS "
        "'Per-tenant enterprise SSO identity providers (SAML 2.0 / OIDC / Google). "
        "config is non-secret protocol settings only; secret_ciphertext is the "
        "AES-256-GCM-encrypted client_secret (AAD = public_id). enabled defaults FALSE.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sso_connections_tenant_enabled "
        "ON sso_connections (tenant_id) WHERE enabled"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sso_identities (
            id BIGSERIAL PRIMARY KEY,
            connection_id BIGINT NOT NULL REFERENCES sso_connections(id) ON DELETE CASCADE,
            subject VARCHAR(512) NOT NULL,
            hub_user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_login_at TIMESTAMPTZ,
            UNIQUE (connection_id, subject)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE sso_identities IS "
        "'PII boundary: hub-api only. subject (OIDC sub / SAML NameID) may be an email "
        "address. Cascades on hub_users deletion so erasure leaves no residue.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sso_identities_user ON sso_identities (hub_user_id)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute(
        "GRANT USAGE ON SEQUENCE sso_connections_id_seq, sso_identities_id_seq TO hub_api;"
    )


def downgrade() -> None:
    """Drop both SSO tables (identities first -- it references connections)."""
    op.execute("DROP TABLE IF EXISTS sso_identities")
    op.execute("DROP TABLE IF EXISTS sso_connections")
