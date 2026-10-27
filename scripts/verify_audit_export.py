#!/usr/bin/env python3
"""Verify Waddles audit-log export files offline -- no server, database, or network needed.

Usage::

    python3 scripts/verify_audit_export.py export-1.json [export-2.json ...] \
        [--anchor-hash HEX] [--expect-head-seq N] [--expect-head-hash HEX]

Each file is one ``GET /api/v1/compliance/audit/export`` response; pass them in page order.
The records are recomputed with the exact hashing code hub-api uses
(``hub_api/services/audit_chain.py``, standard library only) and the chain is checked for
altered, removed, inserted and re-ordered records.

* A slice that does not start at ``seq`` 1 needs ``--anchor-hash``: the ``record_hash`` of the
  record before it, taken from an earlier export or a ``/head`` response you pinned yourself.
  The file's own ``first_prev_hash`` is deliberately NOT trusted for this -- it comes from the
  thing being verified.
* ``--expect-head-seq`` / ``--expect-head-hash`` compare the last record to a head you pinned
  elsewhere; that is the only way to notice that records were removed from the tail.

Exit codes: ``0`` intact (at least one record examined), ``1`` broken, ``2`` bad usage or
nothing examined (a verification that examined zero records proves nothing, so it fails).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHAIN_MODULE = _REPO_ROOT / "hub_api" / "services" / "audit_chain.py"


def load_chain_module() -> ModuleType:
    """Load ``audit_chain.py`` by path (stdlib-only, so no hub-api dependencies are needed)."""
    spec = importlib.util.spec_from_file_location("waddles_audit_chain", _CHAIN_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_CHAIN_MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = (
        module  # dataclasses resolve annotations through sys.modules
    )
    spec.loader.exec_module(module)
    return module


def load_records(paths: list[Path], chain: ModuleType) -> tuple[str, list[Any]]:
    """Read export files in order -> ``(chain_id, records)``; ``ValueError`` on bad input."""
    chain_id: str | None = None
    records: list[Any] = []
    for path in paths:
        body = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(body, dict)
            or "records" not in body
            or "chain_id" not in body
        ):
            raise ValueError(
                f"{path}: not a Waddles audit export (missing chain_id/records)"
            )
        if chain_id is None:
            chain_id = str(body["chain_id"])
        elif body["chain_id"] != chain_id:
            raise ValueError(
                f"{path}: chain_id {body['chain_id']!r} differs from {chain_id!r}"
            )
        for raw in body["records"]:
            records.append(
                chain.ChainRecord(
                    chain_id=chain_id,
                    seq=int(raw["seq"]),
                    event_id=raw["event_id"],
                    occurred_at=raw["occurred_at"],
                    actor_uuid=raw["actor_uuid"],
                    actor_kind=raw["actor_kind"],
                    category=raw["category"],
                    action=raw["action"],
                    outcome=raw["outcome"],
                    target_type=raw["target_type"],
                    target_id=raw["target_id"],
                    details=raw["details"],
                    prev_hash=raw["prev_hash"],
                    record_hash=raw["record_hash"],
                    hash_version=raw["hash_version"],
                )
            )
    if chain_id is None:
        raise ValueError("no export files given")
    return chain_id, records


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "files", nargs="+", type=Path, help="export JSON files, in page order"
    )
    parser.add_argument(
        "--anchor-hash", help="record_hash of the record before the first seq"
    )
    parser.add_argument("--expect-head-seq", type=int)
    parser.add_argument("--expect-head-hash")
    args = parser.parse_args(argv)

    chain = load_chain_module()
    try:
        chain_id, records = load_records(args.files, chain)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return 2
    if not records:
        sys.stderr.write(
            "error: the export contains no records; nothing was verified\n"
        )
        return 2

    first_seq = records[0].seq
    if first_seq != 1 and not args.anchor_hash:
        sys.stderr.write(
            f"error: the export starts at seq {first_seq}; pass --anchor-hash (the record_hash of "
            f"seq {first_seq - 1} from a head you pinned earlier)\n"
        )
        return 2
    try:
        result = chain.verify_records(
            records,
            chain_id=chain_id,
            start_seq=first_seq,
            start_prev_hash=args.anchor_hash if first_seq != 1 else chain.GENESIS_HASH,
            expected_head_seq=args.expect_head_seq,
            expected_head_hash=args.expect_head_hash,
        )
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    sys.stdout.write(
        f"chain={chain_id} status={result.status.value} records_examined={result.examined} "
        f"head_seq={result.head_seq} head_hash={result.head_hash}\n"
    )
    if result.break_ is not None:
        sys.stdout.write(
            f"TAMPER DETECTED at seq {result.break_.seq}: {result.break_.reason.value} "
            f"({result.break_.detail})\n"
        )
        return 1
    return 0 if result.examined > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
