"""Prepare an offline, reviewable loyalty-to-economy migration; never connect to a database.

Both exports must include explicit tenant/community scope. Pseudonyms are one-way:
an operator supplies a verified UUID mapping; unmapped or duplicate rows fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

SERVICE = "waddles.core.service.economy"
# Mirrors `bundle_host_economy::DEFAULT_MAX_SUPPLY` (the ceiling svc_process
# enforces at runtime). A backfill may never create a community total the live
# host would refuse to mint into.
RUNTIME_MAX_SUPPLY = 1_000_000


def validated_cap(value: object) -> int:
    """Require a supply cap within `[1, RUNTIME_MAX_SUPPLY]` before it is planned or rendered."""
    try:
        cap = integer(value, "max_supply")
    except ValueError:
        raise ValueError("max_supply exceeds the runtime supply ceiling") from None
    if cap > RUNTIME_MAX_SUPPLY:
        raise ValueError("max_supply exceeds the runtime supply ceiling")
    return cap


def integer(value: object, name: str, minimum: int = 1) -> int:
    """Require a bounded integer rather than coercing possibly corrupt export data."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 2**63 - 1
    ):
        raise ValueError(f"invalid {name}")
    return value


def scope_key(row: dict[str, Any]) -> tuple[int, int, str]:
    """Validate scope and the legacy SHA-256 pseudonym, never infer an identity."""
    actor_hash = row.get("actor_hash")
    if not isinstance(actor_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", actor_hash):
        raise ValueError("invalid actor_hash")
    return (
        integer(row.get("tenant_id"), "tenant_id"),
        integer(row.get("community_id"), "community_id"),
        actor_hash,
    )


def prepare(
    rows: list[dict[str, Any]],
    mapping: list[dict[str, Any]],
    max_supply: int = RUNTIME_MAX_SUPPLY,
) -> dict[str, Any]:
    """Build one all-or-nothing scoped migration plan from frozen exports and verified mappings."""
    max_supply = validated_cap(max_supply)
    if not rows:
        raise ValueError("empty legacy export")
    identities: dict[tuple[int, int, str], str] = {}
    for row in mapping:
        key = scope_key(row)
        if key in identities:
            raise ValueError("duplicate identity mapping")
        raw = row.get("user_uuid")
        if not isinstance(raw, str) or str(UUID(raw)) != raw:
            raise ValueError("mapping requires a canonical UUID")
        identities[key] = raw
    seen: set[tuple[int, int, str]] = set()
    targets: set[tuple[int, int, str]] = set()
    movements: list[dict[str, Any]] = []
    totals: dict[tuple[int, int], int] = {}
    for row in rows:
        key = scope_key(row)
        if key in seen:
            raise ValueError("duplicate legacy balance row")
        seen.add(key)
        if key not in identities:
            raise ValueError(
                "unmapped legacy balance; manual identity reconciliation required"
            )
        target = (*key[:2], identities[key])
        if target in targets:
            raise ValueError("multiple legacy accounts map to one economy balance")
        targets.add(target)
        amount = integer(row.get("balance"), "balance", 0)
        totals[key[:2]] = totals.get(key[:2], 0) + amount
        if totals[key[:2]] > max_supply:
            raise ValueError("legacy community total exceeds supply cap")
        movements.append(
            {
                "tenant_id": key[0],
                "community_id": key[1],
                "user_uuid": target[2],
                "amount": amount,
            }
        )
    if set(identities) != seen:
        raise ValueError("mapping contains identities outside this export")
    movements.sort(
        key=lambda row: (row["tenant_id"], row["community_id"], row["user_uuid"])
    )
    canonical = json.dumps(movements, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return {
        "status": "REQUIRES_EXPLICIT_APPROVAL",
        "plan_id": digest,
        "source_rows": len(rows),
        "total_amount": sum(row["amount"] for row in movements),
        "max_supply": max_supply,
        "movements": movements,
        "scope_totals": [
            {"tenant_id": t, "community_id": c, "amount": total}
            for (t, c), total in sorted(totals.items())
        ],
    }


def migration_sql(plan: dict[str, Any]) -> str:
    """Render review-only SQL with live membership, supply and preexisting-balance guards.

    Locks serialize this maintenance transaction with all balance writers. A replay
    aborts visibly instead of adding funds again; the legacy source remains intact.
    """
    values = ",\n".join(
        f"({row['tenant_id']},{row['community_id']},'{row['user_uuid']}'::uuid,{row['amount']})"
        for row in plan["movements"]
    )
    plan_id = plan["plan_id"]
    # Re-validated here: a plan loaded from a report file is untrusted input.
    cap = validated_cap(plan["max_supply"])
    return f"""-- REVIEW ONLY: explicit approval and a frozen legacy export are required.
-- Plan {plan_id}; source rows are preserved. Run with ON_ERROR_STOP=1.
BEGIN;
LOCK TABLE economy_balances IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE community_members IN SHARE MODE;
CREATE TEMP TABLE loyalty_backfill_plan(tenant_id INTEGER,community_id INTEGER,user_uuid UUID,amount BIGINT) ON COMMIT DROP;
INSERT INTO loyalty_backfill_plan VALUES {values};
DO $backfill$
BEGIN
  IF EXISTS (SELECT 1 FROM loyalty_backfill_plan p WHERE NOT EXISTS (
      SELECT 1 FROM communities c JOIN community_members m ON m.community_id=c.id
      WHERE c.id=p.community_id AND c.tenant_id=p.tenant_id AND m.user_uuid=p.user_uuid
        AND m.is_active IS TRUE AND m.left_at IS NULL AND m.removed_at IS NULL)) THEN
    RAISE EXCEPTION 'backfill target is not an active member in the exact tenant/community';
  END IF;
  IF EXISTS (SELECT 1 FROM economy_ledger WHERE app_id='{SERVICE}'
      AND idempotency_key LIKE 'loyalty-backfill:{plan_id}:%') THEN
    RAISE EXCEPTION 'backfill plan already applied';
  END IF;
  -- Account-identity drain marker: a legacy account is migrated at most once,
  -- whichever plan (revision) carried it and whatever its balance is now.
  IF EXISTS (SELECT 1 FROM loyalty_backfill_plan p JOIN economy_ledger l
      ON l.tenant_id=p.tenant_id AND l.community_id=p.community_id AND l.user_uuid=p.user_uuid
      WHERE l.app_id='{SERVICE}' AND l.idempotency_key LIKE 'loyalty-backfill:%') THEN
    RAISE EXCEPTION 'backfill account already migrated by an earlier plan';
  END IF;
  IF EXISTS (SELECT 1 FROM loyalty_backfill_plan p JOIN economy_balances b USING(tenant_id,community_id,user_uuid)
      WHERE b.balance<>0) THEN
    RAISE EXCEPTION 'target has existing funds; reconcile manually rather than overwrite';
  END IF;
  IF EXISTS (SELECT 1 FROM (SELECT DISTINCT tenant_id,community_id FROM loyalty_backfill_plan) s
      WHERE COALESCE((SELECT SUM(balance) FROM economy_balances b WHERE b.tenant_id=s.tenant_id AND b.community_id=s.community_id),0)
      +(SELECT SUM(amount) FROM loyalty_backfill_plan p WHERE p.tenant_id=s.tenant_id AND p.community_id=s.community_id)>{cap}) THEN
    RAISE EXCEPTION 'community supply ceiling exceeded';
  END IF;
END
$backfill$;
WITH changed AS (
  INSERT INTO economy_balances(tenant_id,community_id,user_uuid,balance)
    SELECT tenant_id,community_id,user_uuid,amount FROM loyalty_backfill_plan
    ON CONFLICT(tenant_id,community_id,user_uuid) DO UPDATE SET balance=EXCLUDED.balance,updated_at=NOW()
    RETURNING tenant_id,community_id,user_uuid,balance
)
INSERT INTO economy_ledger(tenant_id,community_id,app_id,user_uuid,kind,delta,balance_after,idempotency_key)
 SELECT tenant_id,community_id,'{SERVICE}',user_uuid,'admin_adjust',balance,balance,
   'loyalty-backfill:{plan_id}:' || user_uuid::text FROM changed;
COMMIT;
"""


def main() -> None:
    """Write the dry-run report and reviewable SQL; this program has no apply mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-export", required=True, type=Path)
    parser.add_argument("--identity-map", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--sql", required=True, type=Path)
    parser.add_argument("--max-supply", type=int, default=RUNTIME_MAX_SUPPLY)
    args = parser.parse_args()
    rows = json.loads(args.legacy_export.read_text())
    mapping = json.loads(args.identity_map.read_text())
    if not isinstance(rows, list) or not isinstance(mapping, list):
        raise TypeError("export and mapping must be JSON arrays")
    plan = prepare(rows, mapping, args.max_supply)
    args.report.write_text(json.dumps(plan, indent=2) + "\n")
    args.sql.write_text(migration_sql(plan))


if __name__ == "__main__":
    main()
