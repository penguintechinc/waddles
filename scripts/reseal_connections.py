#!/usr/bin/env python3
"""Re-seal legacy connection secrets as row-bound (AAD) ciphertexts -- finding M4.

New writes are AAD-bound (`seal_version = 1`); this job walks the rows written before that
(`seal_version = 0`), opens them with the legacy no-AAD layout and seals them again bound to
their own row identity, in the same transaction that holds the row `FOR UPDATE`:

* ``platform_connections`` -- AAD ``conn:<public_id>:<platform>``
* ``platform_integrations`` (``community_oauth`` rows only; the ``bot``/``user_oauth`` lanes belong
  to ``credential_manager_module`` and stay legacy) -- AAD ``<community_id>:<platform>``

Rows already at version 1 are *examined* too: each is opened with its AAD, so a row that no longer
authenticates (tampered, swapped, wrong key) is reported instead of silently skipped.

Run from ``hub_api``'s environment (``make reseal-connections``): ``DATABASE_URL`` or the usual
``DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS``, plus ``CREDENTIAL_ENCRYPTION_KEY``. Idempotent and
safe to re-run or interrupt -- every batch is its own transaction.

Output: one final line ``examined=N resealed=M failed=K`` (counts only, never a token, id list or
ciphertext). Exit codes -- ``0`` clean; ``1`` at least one row failed (the rest were still
processed); ``2`` nothing was examined although the tables hold rows (a check over nothing is not a
pass), or ``--expect-rows`` was given and there were none; ``3`` could not run (no usable key,
database unreachable).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

_HUB_API = Path(__file__).resolve().parents[1] / "hub_api"
if str(_HUB_API) not in sys.path:
    sys.path.insert(0, str(_HUB_API))

import psycopg2  # noqa: E402
from flask_core.safe_logging import log_exc_safe  # noqa: E402

from services.platform_integrations_crypto import (  # noqa: E402
    PlatformCredentialCryptoError,
    PlatformCredentialKeyError,
    connection_aad,
    platform_connection_aad,
)
from services.provider_connections.vault import (  # noqa: E402
    SEAL_AAD,
    SEAL_LEGACY,
    open_secret,
    seal_secret,
)

EXIT_OK = 0
EXIT_ROW_FAILED = 1
EXIT_NOTHING_EXAMINED = 2
EXIT_CANNOT_RUN = 3
BATCH_SIZE = 200

logger = logging.getLogger("reseal_connections")


@dataclass(slots=True)
class Counts:
    """Running totals; ``total_rows`` is how many rows the tables hold for this job at all."""

    examined: int = 0
    resealed: int = 0
    failed: int = 0
    total_rows: int = 0


@dataclass(slots=True, frozen=True)
class _Table:
    """One table's scan: its selector SQL and how a row maps to its AAD."""

    name: str
    count_sql: str
    select_sql: str
    update_sql: str
    aad_for: Callable[[Any], bytes]


_CONNECTIONS = _Table(
    name="platform_connections",
    count_sql="SELECT count(*) FROM platform_connections",
    select_sql=(
        "SELECT id, access_token, refresh_token, seal_version, public_id::text, platform "
        "FROM platform_connections WHERE id > %s ORDER BY id LIMIT %s FOR UPDATE"
    ),
    update_sql=(
        "UPDATE platform_connections SET access_token = %s, refresh_token = %s, seal_version = %s "
        "WHERE id = %s"
    ),
    aad_for=lambda row: platform_connection_aad(public_id=UUID(row[4]), platform=row[5]),
)

_INTEGRATIONS = _Table(
    name="platform_integrations",
    count_sql=(
        "SELECT count(*) FROM platform_integrations "
        "WHERE integration_type = 'community_oauth' AND is_encrypted AND access_token IS NOT NULL"
    ),
    select_sql=(
        "SELECT id, access_token, refresh_token, seal_version, community_id, platform "
        "FROM platform_integrations WHERE id > %s AND integration_type = 'community_oauth' "
        "AND is_encrypted AND access_token IS NOT NULL ORDER BY id LIMIT %s FOR UPDATE"
    ),
    update_sql=(
        "UPDATE platform_integrations SET access_token = %s, refresh_token = %s, seal_version = %s "
        "WHERE id = %s"
    ),
    aad_for=lambda row: connection_aad(community_id=int(row[4]), provider=row[5]),
)


def _process_row(cur: Any, table: _Table, row: Any, counts: Counts) -> None:
    """Verify (v1) or upgrade (v0) one locked row; a bad row is counted, never fatal."""
    row_id, access, refresh, seal_version = row[0], row[1], row[2], int(row[3])
    counts.examined += 1
    try:
        aad = table.aad_for(row)
        if seal_version == SEAL_LEGACY:
            new_access = seal_secret(open_secret(access, seal_version=SEAL_LEGACY), aad=aad)
            new_refresh = (
                seal_secret(open_secret(refresh, seal_version=SEAL_LEGACY), aad=aad)
                if refresh
                else None
            )
            cur.execute(table.update_sql, (new_access, new_refresh, SEAL_AAD, row_id))
            counts.resealed += 1
        else:
            open_secret(access, aad=aad, seal_version=seal_version)
            if refresh:
                open_secret(refresh, aad=aad, seal_version=seal_version)
    except PlatformCredentialKeyError:
        raise  # every row would fail the same way: abort instead of reporting a thousand failures
    except (PlatformCredentialCryptoError, ValueError) as exc:
        counts.failed += 1
        log_exc_safe(
            logger,
            logging.ERROR,
            "reseal_connections.row_failed",
            exc,
            table=table.name,
            row_id=int(row_id),
        )


def _reseal_table(conn: Any, table: _Table, counts: Counts, batch_size: int) -> None:
    """Walk one table in id order, one transaction (and one row-lock set) per batch."""
    with conn.cursor() as cur:
        cur.execute(table.count_sql)
        counts.total_rows += int(cur.fetchone()[0])
    conn.commit()
    last_id = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(table.select_sql, (last_id, batch_size))
            rows = cur.fetchall()
            for row in rows:
                _process_row(cur, table, row, counts)
        conn.commit()
        if not rows:
            return
        last_id = int(rows[-1][0])


def reseal_all(conn: Any, *, batch_size: int = BATCH_SIZE) -> Counts:
    """Run the job over both tables on an open psycopg2 connection (autocommit off)."""
    counts = Counts()
    for table in (_CONNECTIONS, _INTEGRATIONS):
        _reseal_table(conn, table, counts, batch_size)
    return counts


def exit_code(counts: Counts, *, expect_rows: bool) -> int:
    """Map the totals onto the documented exit codes (failures outrank an empty scan)."""
    if counts.failed > 0:
        return EXIT_ROW_FAILED
    if counts.examined == 0 and (counts.total_rows > 0 or expect_rows):
        return EXIT_NOTHING_EXAMINED
    return EXIT_OK


def _connect() -> Any:
    """Open a psycopg2 connection from DATABASE_URL or the DB_* variables (never from argv)."""
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "infra-postgres"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "waddlebot"),
        user=os.getenv("DB_USER", "hub-api-rw"),
        password=os.getenv("DB_PASS", ""),
    )


def main(argv: Sequence[str] | None = None, *, connect: Callable[[], Any] = _connect) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description="Re-seal legacy connection secrets (AAD, M4).")
    parser.add_argument(
        "--expect-rows",
        action="store_true",
        help="exit 2 if no row was examined even when the tables are empty",
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    try:
        conn = connect()
    except psycopg2.Error as exc:
        log_exc_safe(logger, logging.ERROR, "reseal_connections.cannot_connect", exc)
        return EXIT_CANNOT_RUN
    try:
        counts = reseal_all(conn, batch_size=args.batch_size)
    except PlatformCredentialKeyError as exc:
        conn.rollback()
        log_exc_safe(logger, logging.ERROR, "reseal_connections.no_usable_key", exc)
        return EXIT_CANNOT_RUN
    except psycopg2.Error as exc:
        conn.rollback()
        log_exc_safe(logger, logging.ERROR, "reseal_connections.database_error", exc)
        return EXIT_CANNOT_RUN
    finally:
        conn.close()
    print(f"examined={counts.examined} resealed={counts.resealed} failed={counts.failed}")
    return exit_code(counts, expect_rows=args.expect_rows)


if __name__ == "__main__":
    sys.exit(main())
