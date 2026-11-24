"""One OPEN public DSAR request per (tenant, email, type): partial unique index.

The public form checked for an existing pending request and inserted in two steps, so two
concurrent submits (a double click, or an attacker pre-submitting a victim's address) could both
create a row. The database now enforces it: at most one `verification_pending` (status 2) request
per `(tenant_id, contact_email_hash, request_type)`. The route catches the integrity error and
re-verifies the existing request (token rotation + mail) instead of failing.

Existing duplicates (unreleased tables, so normally none) are resolved first: all but the newest
pending row of a group are moved to `expired` (status 8) so the index can be built; their contact
data is blanked by the normal retention purge.

Revision ID: 0124_dsar_open_public_unique
Revises: 0123_dsar_mail_outbox
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op

revision = "0124_dsar_open_public_unique"
down_revision = "0123_dsar_mail_outbox"
branch_labels = None
depends_on = None

_UPGRADE = (
    """
UPDATE dsar_requests d
   SET status = 8, completed_at = now(), updated_at = now(), verification_token_hash = NULL
 WHERE d.status = 2
   AND d.contact_email_hash IS NOT NULL
   AND EXISTS (
        SELECT 1 FROM dsar_requests n
         WHERE n.tenant_id = d.tenant_id
           AND n.contact_email_hash = d.contact_email_hash
           AND n.request_type = d.request_type
           AND n.status = 2
           AND n.id > d.id)
""",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_dsar_requests_open_public "
        "ON dsar_requests (tenant_id, contact_email_hash, request_type) "
        "WHERE status = 2 AND contact_email_hash IS NOT NULL"
    ),
)

_DOWNGRADE = ("DROP INDEX IF EXISTS ux_dsar_requests_open_public",)


def upgrade() -> None:
    """Resolve any duplicate pending rows, then build the partial unique index."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop the index (the expired duplicates are not resurrected)."""
    for statement in _DOWNGRADE:
        op.execute(statement)
