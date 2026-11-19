"""Queue steps around `bundle-executor --precompile` (executor-precompile CronJob + enqueue Job).

Run as `python3 -m cli.precompile_queue <command>` from hub-api's `/app` WORKDIR (same
convention as `cli/sign_approved_bundles.py`). The CronJob pod is four steps sharing an emptyDir:

    export (hub-api) -> compile (executor, bounded parallel) -> sign (executor) -> ack (hub-api)

Commands:
  export --out FILE [--limit N]
      Lease up to N pending `bundle_precompile_requests` rows into the manifest.
  ack --report FILE [--manifest FILE]
      Delete compiled/skipped rows, record `last_error` on failed ones. Exit 1 if any item
      failed or the report examined a different number of items than the manifest held
      (a gate that cannot fail is not a gate).
  enqueue-all-active [--revive-dead-letters]
      Queue every signed, currently-active version (the per-release enqueue Job). It never
      compiles, so it never gates `helm upgrade`. With --revive-dead-letters (the Helm Job
      passes it) active versions whose row hit the attempt cap get a fresh round of attempts.
  requeue-failed [--app-id ID] [--error-like TEXT] [--max N] [--dry-run] [--yes]
      Operator tool: reset dead-lettered rows (attempts at the cap). Prints the match count
      and changes nothing unless --yes is given; more than --max matches (default 500) exits 2
      before writing anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from services import bundle_precompile_service as svc
from services.bundle_install_dal import build_install_dal

logger = logging.getLogger("waddles.hub_api.precompile_queue")


class PrecompileConfigError(RuntimeError):
    """Raised when the precompile CLI's minimal environment is incomplete (fail closed)."""


def database_url_from_env(env: Mapping[str, str] | None = None) -> str:
    """Return the Postgres DSN the precompile CLI needs -- and nothing else.

    The queue steps only touch `bundle_precompile_requests` / bundle versions through the
    install DAL, so they must not demand the full `HubAPIConfig` (which also requires
    SECRET_KEY, a session/JWT secret this pod deliberately never receives -- M2).
    `DATABASE_URL` is rendered by the chart from the precompile DB role's own secretKeyRef;
    a missing/blank value raises rather than falling back to a guessed default DSN.
    """
    source = os.environ if env is None else env
    url = (source.get("DATABASE_URL") or "").strip()
    if not url:
        raise PrecompileConfigError(
            "DATABASE_URL is required for cli.precompile_queue (no default DSN)"
        )
    return url


async def _export(install_dal: Any, out: Path, *, limit: int) -> int:
    """Lease up to `limit` queue rows and write them as the precompile manifest."""
    items = await svc.claim_pending(install_dal, limit=limit)
    payload = json.dumps([i.as_json() for i in items])
    await asyncio.to_thread(out.write_text, payload)
    logger.info("precompile-queue: exported", extra={"items": len(items)})
    return 0


async def _enqueue_all_active(install_dal: Any, *, revive_dead_letters: bool = False) -> int:
    """Queue every signed, active version; counts are logged (examined is the denominator)."""
    summary = await svc.enqueue_all_active(install_dal, revive_dead_letters=revive_dead_letters)
    logger.info(
        "precompile-queue: enqueue-all-active examined=%d queued=%d already_pending=%d revived=%d",
        summary.examined,
        summary.queued,
        summary.already_pending,
        summary.revived,
    )
    return 0


async def _requeue_failed(
    install_dal: Any,
    *,
    app_id: str | None,
    error_like: str | None,
    cap: int,
    apply: bool,
) -> int:
    """Count (and with `apply`, reset) dead-lettered rows; exit 2 when the cap is exceeded."""
    try:
        summary = await svc.requeue_failed(
            install_dal, app_id=app_id, error_like=error_like, cap=cap, dry_run=not apply
        )
    except svc.RequeueCapExceededError as exc:
        logger.error("precompile-queue: requeue-failed refused: %s", exc)
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if summary.dry_run:
        print(
            f"dry-run: {summary.matched} dead-lettered row(s) match; re-run with --yes to requeue"
        )
    else:
        print(f"requeued {summary.requeued} of {summary.matched} matching dead-lettered row(s)")
    return 0


def _load_report(path: Path) -> dict[str, Any]:
    """Parse the sign stage's report; anything but a JSON object is an error."""
    report = json.loads(path.read_text())
    if not isinstance(report, dict):
        raise ValueError("precompile report is not a JSON object")
    return report


def _check_denominator(report: dict[str, Any], manifest: Path | None) -> None:
    """Fail when the report examined fewer items than the exported manifest held."""
    if manifest is None or not manifest.exists():
        return
    exported = len(json.loads(manifest.read_text()))
    examined = int(report.get("examined", -1))
    if examined != exported:
        raise ValueError(f"precompile report examined {examined} items, manifest had {exported}")


async def _ack(install_dal: Any, report: dict[str, Any]) -> int:
    """Apply the report to the queue; exit 1 when any item failed (fail loud)."""
    summary = await svc.ack_report(install_dal, report)
    logger.info(
        "precompile-queue: acked",
        extra={"examined": summary.examined, "acked": summary.acked, "failed": summary.failed},
    )
    if summary.failed:
        logger.error("precompile-queue: %d item(s) failed to precompile", summary.failed)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    """Argument parser for the queue commands."""
    parser = argparse.ArgumentParser(description="bundle precompile queue steps")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--out", type=Path, required=True)
    exp.add_argument("--limit", type=int, default=50)
    ack = sub.add_parser("ack")
    ack.add_argument("--report", type=Path, required=True)
    ack.add_argument("--manifest", type=Path, default=None)
    enq = sub.add_parser("enqueue-all-active")
    enq.add_argument("--revive-dead-letters", action="store_true")
    req = sub.add_parser("requeue-failed")
    req.add_argument("--app-id", default=None)
    req.add_argument("--error-like", default=None)
    req.add_argument("--max", dest="cap", type=int, default=svc.DEFAULT_REQUEUE_CAP)
    req.add_argument("--dry-run", action="store_true")
    req.add_argument("--yes", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    """Dispatch one command with a DAL built from DATABASE_URL alone."""
    report: dict[str, Any] = {}
    if args.command == "ack":
        report = _load_report(args.report)
        _check_denominator(report, args.manifest)
    install_dal = await build_install_dal(database_url_from_env(), pool_size=2)
    try:
        if args.command == "export":
            return await _export(install_dal, args.out, limit=args.limit)
        if args.command == "enqueue-all-active":
            return await _enqueue_all_active(
                install_dal, revive_dead_letters=args.revive_dead_letters
            )
        if args.command == "requeue-failed":
            return await _requeue_failed(
                install_dal,
                app_id=args.app_id,
                error_like=args.error_like,
                cap=args.cap,
                apply=args.yes and not args.dry_run,
            )
        return await _ack(install_dal, report)
    finally:
        await install_dal.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- returns the process exit code."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    return asyncio.run(_run(_parser().parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - script-execution guard
    sys.exit(main())
