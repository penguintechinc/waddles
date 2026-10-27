"""Operator tooling for the ban-evasion suppression list (lift / purge / stats).

Run as ``python3 -m cli.suppression <command>`` from hub-api's ``/app`` WORKDIR with the usual
``DATABASE_URL`` / ``DB_*`` environment, plus ``TENANT_KEK_HEX`` for ``lift`` (the master key the
codes were derived from). In-cluster only -- never an HTTP route, like ``cli.verify_audit_chain``.

Commands (every output line carries counts and enums only -- never a platform id or a code):

* ``lift --tenant-id N --platform P --platform-user-id-stdin`` -- a moderator lifted the
  restriction, or the person objected (GDPR Art. 21) and a human review decided to remove it:
  deletes that identity's row in THAT tenant at once. The platform id is read from stdin, never
  argv (shell history). Exit ``0`` = at least one row deleted, ``1`` = nothing matched (so a typo
  cannot look like success), ``3`` = could not run (key missing / database unreachable).
* ``check --tenant-id N --platform P --platform-user-id-stdin`` -- does a live row exist for this
  identity in this tenant? The answer to a data subject's access request (Art. 15) or to an
  objection before a human decides (Art. 21): prints ``matched=true|false``, the restriction type
  and the expiry, never the code. Exit ``0`` = matched, ``1`` = no live row.
* ``purge`` -- delete every expired row now (the hub-api process also does this hourly).
* ``stats`` -- live / expired row counts per tenant, restriction type and key version. The
  ``key_ver`` breakdown is what tells an operator when ``TENANT_KEK_HEX_PREVIOUS`` can be dropped
  after a platform-KEK rotation: once no live row carries the old version, dropping it loses no
  match. ``0`` = counted (zero rows is still a valid, printed answer).

Exit codes shared by all commands: ``2`` = invalid arguments, ``3`` = the command itself could
not run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, TextIO

from flask_core.database import init_database
from flask_core.db_errors import describe_db_error, format_sanitized_traceback

from config import HubAPIConfig
from services import ban_suppression
from services.schema import bind_erasure_reputation_tables, bind_suppression_tables
from services.suppression_keys import (
    PlatformKekKeySource,
    SuppressionInputError,
    SuppressionKeyError,
)

EXIT_OK = 0
EXIT_NOTHING_MATCHED = 1
EXIT_BAD_ARGS = 2
EXIT_CANNOT_RUN = 3


async def lift(
    async_dal: Any, dal: Any, *, tenant_id: int, platform: str, platform_user_id: str, out: TextIO
) -> int:
    """Delete one identity's suppression row in one tenant; returns the process exit code."""
    deleted = await ban_suppression.lift_suppression_async(
        async_dal,
        dal,
        tenant_id=tenant_id,
        platform=platform,
        platform_user_id=platform_user_id,
        keys=PlatformKekKeySource(),
    )
    out.write(f"lift tenant_id={tenant_id} platform={platform} rows_deleted={deleted}\n")
    return EXIT_OK if deleted else EXIT_NOTHING_MATCHED


async def check(
    async_dal: Any, dal: Any, *, tenant_id: int, platform: str, platform_user_id: str, out: TextIO
) -> int:
    """Report whether a live suppression row matches one identity in one tenant."""
    source = PlatformKekKeySource()

    def _find() -> ban_suppression.SuppressionMatch | None:
        return ban_suppression.find_suppression(
            dal,
            tenant_id=tenant_id,
            platform=platform,
            platform_user_id=platform_user_id,
            keys=source,
            now=datetime.now(UTC),
        )

    match = await ban_suppression.run_in_transaction(async_dal, dal, _find)
    if match is None:
        out.write(f"check tenant_id={tenant_id} platform={platform} matched=false\n")
        return EXIT_NOTHING_MATCHED
    expires = "until_lifted" if match.expires_at is None else match.expires_at.isoformat()
    out.write(
        f"check tenant_id={tenant_id} platform={platform} matched=true "
        f"reason={match.reason.value} expires_at={expires}\n"
    )
    return EXIT_OK


async def purge(async_dal: Any, dal: Any, *, out: TextIO) -> int:
    """Delete every expired row; returns the process exit code."""
    deleted = await ban_suppression.purge_expired_async(async_dal, dal)
    out.write(f"purge rows_deleted={deleted}\n")
    return EXIT_OK


async def stats(async_dal: Any, dal: Any, *, out: TextIO) -> int:
    """Print live/expired counts per tenant, restriction type and key version."""
    table = dal.suppression_records
    rows = await async_dal.select_async(
        dal(table.id > 0), table.tenant_id, table.reason_enum, table.key_ver, table.expires_at
    )
    now = datetime.now(UTC)
    buckets: Counter[tuple[int, str, str, str]] = Counter()
    for row in rows:
        expires = row.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        state = "expired" if expires is not None and expires <= now else "live"
        buckets[(int(row.tenant_id), row.reason_enum, row.key_ver, state)] += 1
    for (tenant_id, reason, key_ver, state), count in sorted(buckets.items()):
        out.write(
            f"tenant_id={tenant_id} reason={reason} key_ver={key_ver} state={state} rows={count}\n"
        )
    out.write(f"TOTAL rows={sum(buckets.values())} groups={len(buckets)}\n")
    return EXIT_OK


async def _run(args: argparse.Namespace, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    """Open the database, dispatch the command, map failures to exit codes."""
    platform_user_id = ""
    if args.command in ("lift", "check"):
        if not args.platform_user_id_stdin:
            stderr.write(
                f"error: {args.command} needs --platform-user-id-stdin (ids never go in argv)\n"
            )
            return EXIT_BAD_ARGS
        platform_user_id = stdin.readline().rstrip("\r\n")
    async_dal = None
    try:
        config = HubAPIConfig.from_env()
        async_dal = init_database(config.database_url, pool_size=2, migrate=False)
        dal = async_dal.dal
        bind_suppression_tables(dal)
        bind_erasure_reputation_tables(dal)
        if args.command == "lift":
            return await lift(
                async_dal,
                dal,
                tenant_id=args.tenant_id,
                platform=args.platform,
                platform_user_id=platform_user_id,
                out=stdout,
            )
        if args.command == "check":
            return await check(
                async_dal,
                dal,
                tenant_id=args.tenant_id,
                platform=args.platform,
                platform_user_id=platform_user_id,
                out=stdout,
            )
        if args.command == "purge":
            return await purge(async_dal, dal, out=stdout)
        return await stats(async_dal, dal, out=stdout)
    except SuppressionInputError as exc:
        stderr.write(f"error: {exc}\n")
        return EXIT_BAD_ARGS
    except SuppressionKeyError as exc:
        stderr.write(f"error: suppression key unavailable: {exc}\n")
        return EXIT_CANNOT_RUN
    except Exception as exc:
        # Value-free: a driver message can embed bound values, so only the type + the frames.
        stderr.write(f"error: suppression {args.command} could not run: {describe_db_error(exc)}\n")
        stderr.write(format_sanitized_traceback(exc) + "\n")
        return EXIT_CANNOT_RUN
    finally:
        if async_dal is not None:
            # Not `close_async()`: pydal's `DAL.close()` raises when called from the executor
            # thread that did not create it (the same reason hub-api's own shutdown only logs
            # that failure), and this process exits next anyway -- the connections go with it.
            async_dal.executor.shutdown(wait=True)


def build_parser() -> argparse.ArgumentParser:
    """The argument parser (separate so tests can exercise it without a database)."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    lift_p = sub.add_parser("lift", help="delete one identity's row in one tenant")
    lift_p.add_argument("--tenant-id", type=int, required=True)
    lift_p.add_argument("--platform", required=True)
    lift_p.add_argument(
        "--platform-user-id-stdin",
        action="store_true",
        help="read the platform user id from the first line of stdin (never argv)",
    )
    check_p = sub.add_parser("check", help="is there a live row for this identity in this tenant?")
    check_p.add_argument("--tenant-id", type=int, required=True)
    check_p.add_argument("--platform", required=True)
    check_p.add_argument(
        "--platform-user-id-stdin",
        action="store_true",
        help="read the platform user id from the first line of stdin (never argv)",
    )
    sub.add_parser("purge", help="delete every expired row now")
    sub.add_parser("stats", help="live/expired counts per tenant, type and key version")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args, stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr))


if __name__ == "__main__":  # pragma: no cover - script-execution guard, never hit under pytest
    sys.exit(main())
