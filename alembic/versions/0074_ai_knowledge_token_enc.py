"""Encrypt plaintext ``ai_knowledge_sources.encrypted_token`` values at rest.

The Node-era service stored the admin-supplied access token verbatim in the
``encrypted_token`` column ("encryption handled at storage layer" -- it never was), and the
Python port kept that. hub-api now encrypts on write (AES-256-GCM, the same
``GITHUB_SYNC_ENCRYPTION_KEY`` scheme as ``github_sync_service.encrypt_token``) and decrypts in
``index_source``. This data migration seals every row still holding plaintext.

Idempotent: a value that already decrypts under the key is left alone, so a re-run (or rows
written by the new code) are never double-encrypted. Fails loudly if plaintext rows exist and the
key is unset/invalid -- never leaves them behind silently. Downgrade is a deliberate no-op:
plaintext tokens are never restored.

Revision ID: 0074_ai_knowledge_token_enc
Revises: 0073_youtube_connection_resource
Create Date: 2026-11-05
"""

from __future__ import annotations

import base64
import binascii
import os

import sqlalchemy as sa
from alembic import op
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

revision = "0074_ai_knowledge_token_enc"
down_revision = "0073_youtube_connection_resource"
branch_labels = None
depends_on = None

_KEY_ENV = "GITHUB_SYNC_ENCRYPTION_KEY"


def _key() -> bytes:
    """Return the 32-byte AES key from the env, or raise (never fall back silently)."""
    raw = os.environ.get(_KEY_ENV, "")
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise RuntimeError(
            f"{_KEY_ENV} must be a 64-char hex string: ai_knowledge_sources holds plaintext "
            "tokens that must be encrypted before this migration can complete"
        )
    return key


def _is_sealed(value: str, key: bytes) -> bool:
    """True if ``value`` already decrypts under ``key`` (i.e. was written by encrypt_token)."""
    try:
        raw = base64.b64decode(value, validate=True)
        AESGCM(key).decrypt(raw[:12], raw[12:], None)
    except (InvalidTag, ValueError, binascii.Error):
        return False
    return True


def _seal(plaintext: str, key: bytes) -> str:
    """AES-256-GCM encrypt; base64(iv(12) + ciphertext+tag) -- matches encrypt_token()."""
    iv = os.urandom(12)
    return base64.b64encode(iv + AESGCM(key).encrypt(iv, plaintext.encode(), None)).decode()


def upgrade() -> None:
    """Seal every plaintext token row; no-op when the table is absent or has none."""
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("ai_knowledge_sources"):
        return
    rows = bind.execute(
        sa.text(
            "SELECT id, encrypted_token FROM ai_knowledge_sources "
            "WHERE encrypted_token IS NOT NULL AND encrypted_token <> ''"
        )
    ).fetchall()
    if not rows:
        return
    key = _key()
    for row_id, value in rows:
        if _is_sealed(value, key):
            continue
        bind.execute(
            sa.text("UPDATE ai_knowledge_sources SET encrypted_token = :t WHERE id = :i"),
            {"t": _seal(value, key), "i": row_id},
        )


def downgrade() -> None:
    """Intentionally empty: plaintext tokens are never restored."""
