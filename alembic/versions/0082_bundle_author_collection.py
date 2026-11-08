"""Bundle `collection` designation + normalize stored `author` to the validated form.

`author` becomes a validated GitHub-profile-or-name value (`flask_core.bundle_author`), stored
canonically as `github:<handle>` or a plain name. This migration:

* adds `app_catalog.collection` (publisher designation, e.g. `superpenguintv`, nullable);
* normalizes existing `app_catalog.author` / `app_versions.author` rows: GitHub spellings
  (`@h`, `https://github.com/h[/]`) -> `github:h`; legacy free text is reduced to a valid
  plain name (invalid characters stripped, separators collapsed, 64 chars max) or NULL when
  nothing usable remains;
* special-cases the SuperPenguinTV roll port (`superpenguintv (Psychoboy)`) to
  `github:Psychoboy` + `collection = 'superpenguintv'`, replacing the old `alternatives`
  category as its grouping.

The normalization is self-contained (no app imports) so the migration stays replayable.
Downgrade drops the column only; the author rewrite is a lossless-enough canonicalization and
is not reversed.

Revision ID: 0082_bundle_author_collection
Revises: 0081_spectrum_conn_resource
Create Date: 2026-11-07
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0082_bundle_author_collection"
down_revision = "0081_spectrum_conn_resource"
branch_labels = None
depends_on = None

_HANDLE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
_NAME_RE = re.compile(r"[^\W_]+(?:[ _.\-][^\W_]+)*")
_ROLL_APP_ID = "waddles.integrations.superpenguin.roll"


def normalize_author(raw: str | None) -> str | None:
    """Return the canonical author for a legacy stored value, or None if nothing is usable."""
    if raw is None:
        return None
    text = raw.strip()
    handle: str | None = None
    if text.startswith("https://github.com/"):
        handle = text[len("https://github.com/") :].removesuffix("/")
    elif text.startswith("github:"):
        handle = text[len("github:") :]
    elif text.startswith("@"):
        handle = text[1:]
    if handle is not None and _HANDLE_RE.fullmatch(handle):
        return f"github:{handle}"
    # Free text -> valid name: keep letters/digits, turn runs of other chars into one space.
    words = re.findall(r"[^\W_]+", text)
    name = " ".join(words)[:64].rstrip()
    return name if name and _NAME_RE.fullmatch(name) else None


def upgrade() -> None:
    """Add `collection`, then normalize existing author values."""
    op.execute(
        "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS collection VARCHAR(50)"
    )
    bind = op.get_bind()
    for table, key in (("app_catalog", "app_id"), ("app_versions", "id")):
        rows = bind.execute(
            sa.text(f"SELECT {key}, author FROM {table} WHERE author IS NOT NULL")
        ).fetchall()
        for row_key, author in rows:
            normalized = normalize_author(author)
            if normalized != author:
                bind.execute(
                    sa.text(f"UPDATE {table} SET author = :a WHERE {key} = :k"),
                    {"a": normalized, "k": row_key},
                )
    bind.execute(
        sa.text(
            "UPDATE app_catalog SET author = 'github:Psychoboy', collection = 'superpenguintv' "
            "WHERE app_id = :app_id"
        ),
        {"app_id": _ROLL_APP_ID},
    )
    bind.execute(
        sa.text(
            "UPDATE app_versions SET author = 'github:Psychoboy' WHERE app_id = :app_id"
        ),
        {"app_id": _ROLL_APP_ID},
    )


def downgrade() -> None:
    """Drop `collection` (author normalization is not reversed)."""
    op.execute("ALTER TABLE app_catalog DROP COLUMN IF EXISTS collection")
