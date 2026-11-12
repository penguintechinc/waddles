"""Export/ack steps around `bundle-executor --precompile` (executor-precompile Job/CronJob).

Run as `python3 -m cli.precompile_queue <command>` from hub-api's `/app` WORKDIR (same
convention as `cli/sign_approved_bundles.py`). The Helm Job is three steps sharing an emptyDir:

    export (hub-api)  ->  bundle-executor --precompile (executor image)  ->  ack (hub-api)

Commands:
  export --out FILE [--all-active] [--limit N]
      Queue mode: lease up to N pending `bundle_precompile_requests` rows.
      `--all-active`: backfill -- every signed, currently-active version (no queue writes).
  ack --report FILE [--backfill]
      Queue mode: delete compiled/skipped rows, record `last_error` on failed ones.
      `--backfill`: no queue writes, just validate the report.
      Exit 1 if any item failed or the report examined zero items while the export had
      some (a gate that cannot fail is not a gate).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from config import HubAPIConfig
from services import bundle_precompile_service as svc
from services.bundle_install_dal import build_install_dal

logger = logging.getLogger("waddles.hub_api.precompile_queue")


async def _export(install_dal: Any, out: Path, *, all_active: bool, limit: int) -> int:
    if all_active:
        items = await svc.select_all_active(install_dal)
    else:
        items = await svc.claim_pending(install_dal, limit=limit)
    payload = json.dumps([i.as_json() for i in items])
    await asyncio.to_thread(out.write_text, payload)
    logger.info(
        "precompile-queue: exported",
        extra={"mode": "backfill" if all_active else "queue", "items": len(items)},
    )
    return 0


def _load_report(path: Path) -> dict[str, Any]:
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


async def _ack(install_dal: Any | None, report: dict[str, Any]) -> int:
    if install_dal is None:
        failed = int(report.get("failed", 0))
        logger.info(
            "precompile-queue: backfill report",
            extra={
                "examined": report.get("examined"),
                "compiled": report.get("compiled"),
                "skipped": report.get("skipped"),
                "failed": failed,
            },
        )
        return 1 if failed else 0
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
    parser = argparse.ArgumentParser(description="bundle precompile queue export/ack")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--out", type=Path, required=True)
    exp.add_argument("--all-active", action="store_true")
    exp.add_argument("--limit", type=int, default=50)
    ack = sub.add_parser("ack")
    ack.add_argument("--report", type=Path, required=True)
    ack.add_argument("--manifest", type=Path, default=None)
    ack.add_argument("--backfill", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "ack":
        report = _load_report(args.report)
        _check_denominator(report, args.manifest)
        if args.backfill:
            return await _ack(None, report)
    config = HubAPIConfig.from_env()
    install_dal = await build_install_dal(config.database_url, pool_size=2)
    try:
        if args.command == "export":
            return await _export(
                install_dal, args.out, all_active=args.all_active, limit=args.limit
            )
        return await _ack(install_dal, _load_report(args.report))
    finally:
        await install_dal.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- returns the process exit code."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    return asyncio.run(_run(_parser().parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - script-execution guard
    sys.exit(main())
