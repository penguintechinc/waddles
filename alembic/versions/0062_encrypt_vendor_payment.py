"""Field-encrypt `vendor_submissions.payment_details` (paypal_email / bank_account).

Security-audit MED: financial details sat plaintext in a JSONB column, protected only by
volume encryption. New writes are now AES-256-GCM encrypted in the application
(`hub_api/services/marketplace_crypto.py`, key `MARKETPLACE_ENCRYPTION_KEY`); this revision
encrypts the rows written before that change into the same `{"v": 1, "enc": <hex>}` envelope
(IV-then-ciphertext hex, AAD `vendor_submissions.payment_details`).

Key handling: read from `MARKETPLACE_ENCRYPTION_KEY` only (64 hex chars, never a default). If
any plaintext row needs encrypting and the key is absent or malformed the upgrade RAISES --
never a silent skip. Empty `{}` / NULL rows hold nothing sensitive and are left as-is. A DB
without the legacy table (minimal CI bootstrap) is skipped with a WARNING.

Downgrade decrypts envelopes back to plaintext JSON (needs the same key) so a rollback to
pre-encryption code keeps working; it is idempotent and also raises without the key.

Revision ID: 0062_encrypt_vendor_payment
Revises: 0061_port_legacy_sql_103_codec
Create Date: 2026-10-18
"""

from __future__ import annotations

import json
import logging
import os

import sqlalchemy as sa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.engine import Connection

from alembic import op

revision = "0062_encrypt_vendor_payment"
down_revision = "0061_port_legacy_sql_103_codec"
branch_labels = None
depends_on = None

_LOG = logging.getLogger("alembic.runtime.migration")
_AAD = b"vendor_submissions.payment_details"
_IV_LENGTH = 12
_KEY_ENV = "MARKETPLACE_ENCRYPTION_KEY"


def _key() -> bytes:
    """Load the 32-byte AES key from the environment; fail closed when absent/malformed."""
    hex_key = os.environ.get(_KEY_ENV, "")
    if len(hex_key) != 64:
        raise RuntimeError(
            f"{_KEY_ENV} (64 hex chars) is required to migrate payment_details"
        )
    return bytes.fromhex(hex_key)


def _is_envelope(value: object) -> bool:
    """True for the `{"v": 1, "enc": <hex>}` envelope."""
    return (
        isinstance(value, dict)
        and value.get("v") == 1
        and isinstance(value.get("enc"), str)
        and set(value) == {"v", "enc"}
    )


def _table_exists(conn: Connection) -> bool:
    """True when the legacy `vendor_submissions` table exists."""
    return bool(
        conn.execute(
            sa.text("SELECT to_regclass('public.vendor_submissions') IS NOT NULL")
        ).scalar()
    )


def _rows(conn: Connection) -> list[tuple[int, object]]:
    """All (id, payment_details) pairs with a non-NULL value."""
    result = conn.execute(
        sa.text(
            "SELECT id, payment_details FROM vendor_submissions WHERE payment_details IS NOT NULL"
        )
    )
    return [(r[0], r[1]) for r in result]


def _update(conn: Connection, row_id: int, value: dict[str, object]) -> None:
    """Write one converted JSON value back."""
    conn.execute(
        sa.text(
            "UPDATE vendor_submissions SET payment_details = CAST(:v AS JSONB) WHERE id = :i"
        ),
        {"v": json.dumps(value), "i": row_id},
    )


def upgrade() -> None:
    """Encrypt every plaintext, non-empty payment_details row."""
    conn = op.get_bind()
    if not _table_exists(conn):
        _LOG.warning(
            "vendor_submissions absent (CI bootstrap); skipping payment_details encryption"
        )
        return
    todo = [(i, v) for i, v in _rows(conn) if v != {} and not _is_envelope(v)]
    if not todo:
        return
    aes = AESGCM(_key())
    for row_id, value in todo:
        iv = os.urandom(_IV_LENGTH)
        pt = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        _update(conn, row_id, {"v": 1, "enc": (iv + aes.encrypt(iv, pt, _AAD)).hex()})
    _LOG.info("encrypted payment_details on %d vendor_submissions rows", len(todo))


def downgrade() -> None:
    """Decrypt envelopes back to plaintext JSON (requires the same key)."""
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    todo = [(i, v) for i, v in _rows(conn) if _is_envelope(v)]
    if not todo:
        return
    aes = AESGCM(_key())
    for row_id, value in todo:
        raw = bytes.fromhex(str(value["enc"]))  # type: ignore[index]  # _is_envelope proved dict
        plain = json.loads(aes.decrypt(raw[:_IV_LENGTH], raw[_IV_LENGTH:], _AAD))
        _update(conn, row_id, plain)
