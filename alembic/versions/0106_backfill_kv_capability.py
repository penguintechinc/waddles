"""Backfill the ``storage.kv`` capability into already-recorded install consent summaries.

``bundle_approval_service._derive_capabilities`` only looked at the bare-string ``permissions:``
list, so every bundle using the structured ``- id: storage.kv`` form was recorded WITHOUT the
``storage.kv`` capability in ``app_install_approvals.summary_json`` /
``app_global_installs.summary_json``. The data plane builds its per-app ``kv`` capability snapshot
from exactly that array, so ``bundle_host_kv`` denied every kv call (``not_granted``) and the
``count``/``cringe``/``hype`` commands answered "Something went wrong updating the counter
storage". The code is fixed for NEW approvals; this revision repairs the rows already written.

Only rows whose own uploaded manifest declares ``storage.kv`` (structured or bare string) and
whose summary lacks it are touched -- nothing is ever granted that the manifest did not declare.
``permission_hash`` is left as recorded (it identifies the consent the user saw); the next real
approval recomputes both from the fixed derivation. Idempotent. Data-only, so ``downgrade`` is a
deliberate no-op: removing the capability again would only re-break kv for every affected bundle.

Revision ID: 0106_backfill_kv_capability
Revises: 0105_twitch_handle_freshness
Create Date: 2026-11-14
"""

from __future__ import annotations

from alembic import op

revision = "0106_backfill_kv_capability"
down_revision = "0105_twitch_handle_freshness"
branch_labels = None
depends_on = None

#: Rows whose uploaded manifest declares `storage.kv` in either `permissions:` shape.
DECLARES_KV_SQL = """
    jsonb_typeof(u.manifest_json -> 'permissions') = 'array'
    AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(u.manifest_json -> 'permissions') AS p(entry)
         WHERE entry = to_jsonb('storage.kv'::text)
            OR (jsonb_typeof(entry) = 'object' AND entry ->> 'id' = 'storage.kv')
    )
"""

#: `capabilities` plus `storage.kv`, de-duplicated and sorted (the summary stores it sorted).
WITH_KV_SQL = """
    jsonb_set(
        t.summary_json, '{capabilities}',
        (SELECT jsonb_agg(c ORDER BY c) FROM (
            SELECT jsonb_array_elements_text(
                       CASE WHEN jsonb_typeof(t.summary_json -> 'capabilities') = 'array'
                            THEN t.summary_json -> 'capabilities' ELSE '[]'::jsonb END) AS c
            UNION SELECT 'storage.kv') AS caps),
        true)
"""

MISSING_KV_SQL = """
    NOT (CASE WHEN jsonb_typeof(t.summary_json -> 'capabilities') = 'array'
              THEN t.summary_json -> 'capabilities' ELSE '[]'::jsonb END
         ? 'storage.kv')
"""


def backfill_sql(table: str) -> str:
    """The idempotent UPDATE that adds `storage.kv` to `table`'s summaries (see module doc)."""
    return f"""
        UPDATE {table} AS t
           SET summary_json = {WITH_KV_SQL}
          FROM app_version_uploads AS u
         WHERE u.app_id = t.app_id AND u.version = t.version
           AND {DECLARES_KV_SQL}
           AND {MISSING_KV_SQL}
    """  # noqa: S608 - `table` is one of two fixed literals, never user input


def upgrade() -> None:
    """Repair both consent-summary tables in place."""
    op.execute(backfill_sql("app_install_approvals"))
    op.execute(backfill_sql("app_global_installs"))


def downgrade() -> None:
    """Data-only repair; intentionally not reversed (see module docstring)."""
