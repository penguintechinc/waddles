#!/usr/bin/env python3
"""Bundle sweep: every seeded/installed bundle -> resolved / loaded / invoked.

Why: after the app_id hard cut-over we need per-bundle proof the registry
still resolves each id and the data plane still serves it.

Evidence per bundle (read-only; DB via tests/e2e/lib/psql-ro.sh, hub-api via
the same authenticated client as api-sweep):
  resolved  an app_active_versions row exists AND the latest app_versions row
            carries an artifact digest
  loaded    resolved AND no bundle_precompile_requests row for it holds a
            last_error (the executor's precompile/load queue is clean)
  invoked   svc-action's action_dispatch_log shows >=1 `success` dispatch for the
            app_id (command went ingest -> process -> action on the REAL path);
            `never-observed` is reported, not failed, unless --require-invoked
  described service bundles only: GET .../bundles/{app_id}/admin (404 = not
            activated in the test community, informational; 5xx = failure)
There is no external endpoint that injects a synthetic chat event for an arbitrary
bundle (the only in-cluster driver is svc-ingest's flag-gated canary), so live
per-command injection is a follow-up; --harness reuses the existing real-wasm
flag-on command e2e (make test-bundle-flag-on-command-e2e) as an extra gate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api-sweep"))
from api_sweep import Client  # noqa: E402  (reuse throttled/authenticated client)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
GOOD_ID = re.compile(r"^(penguintechinc\.waddles|psychoboy\.penguintwitchbot)\.[a-z0-9-]+$")
RUNTIME = {"python": "scripted", "rust": "compiled", "csharp": "C#"}

SQL = """
select coalesce(json_agg(t), '[]'::json) from (
 select c.app_id, c.kind, c.execution_model, c.status, v.version, v.language,
  (v.artifact_digest is not null) as has_artifact,
  exists(select 1 from app_active_versions av where av.app_id=c.app_id) as resolved,
  (select min(command) from app_commands ac where ac.app_id=c.app_id) as command,
  (select count(*) from bundle_precompile_requests p where p.app_id=c.app_id and p.last_error is not null) as precompile_errors,
  (select count(*) from action_dispatch_log d where d.app_id=c.app_id and d.status='success') as dispatch_ok,
  (select count(*) from action_dispatch_log d where d.app_id=c.app_id and d.status<>'success') as dispatch_bad,
  (select max(dispatched_at) from action_dispatch_log d where d.app_id=c.app_id) as last_dispatch
 from app_catalog c
 left join lateral (select * from app_versions v where v.app_id=c.app_id order by built_at desc nulls last, id desc limit 1) v on true
 order by c.app_id) t
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report-dir", default=os.environ.get("REPORT_DIR", "/tmp/bundle-sweep-report"))
    ap.add_argument("--rate", type=float, default=float(os.environ.get("SWEEP_RATE", "1.4")))
    ap.add_argument("--require-invoked", action="store_true", help="count never-observed command bundles as failed")
    ap.add_argument("--harness", action="store_true", help="also run make test-bundle-flag-on-command-e2e (needs docker + cargo)")
    ap.add_argument("--allow-old-ids", action="store_true", help="old ids do not fail the run (pre-cutover baseline)")
    a = ap.parse_args()

    base = os.environ.get("BASE_URL", "")
    email = os.environ.get("HUB_TEST_EMAIL", "")
    pw = os.environ.get("HUB_TEST_PASS") or os.environ.get("INITIAL_ADMIN_PASSWORD", "")
    community = os.environ.get("COMMUNITY_ID", "15959481-f9f1-47ce-be48-d975a6832d08")
    if not (base and email and pw):
        print("FAIL: BASE_URL, HUB_TEST_EMAIL, HUB_TEST_PASS must be set (use run.sh)", file=sys.stderr)
        return 2

    raw = subprocess.run([str(ROOT / "tests/e2e/lib/psql-ro.sh"), SQL], capture_output=True, text=True, timeout=120)
    if raw.returncode != 0:
        print(f"FAIL: db inventory: {raw.stderr.strip()[:200]}", file=sys.stderr)
        return 2
    bundles = json.loads(raw.stdout)
    if not bundles:
        print("FAIL: zero bundles listed (denominator 0)", file=sys.stderr)
        return 2

    cli = Client(base, os.environ.get("HOST_HEADER"), a.rate, os.environ.get("INSECURE") == "1")
    st, _, txt = cli.call("POST", "/api/v1/auth/login", json.dumps({"email": email, "password": pw}).encode(), "application/json")
    try:
        cli.token = json.loads(txt)["token"] if st == 200 else None
    except Exception:
        cli.token = None
    if not cli.token:
        print(f"FAIL: admin login HTTP {st}", file=sys.stderr)
        return 2

    ls, _, _ = cli.call("GET", "/api/v1/marketplace/bundles")
    rows = []
    for b in bundles:
        kind = b["kind"] or "app"
        is_service = kind == "service"
        resolved = bool(b["resolved"] and b["has_artifact"])
        loaded = resolved and b["precompile_errors"] == 0
        if b["dispatch_bad"] and not b["dispatch_ok"]:
            invoked = "failed"
        elif b["dispatch_ok"]:
            invoked = "observed-ok"
        else:
            invoked = "never-observed"
        # Service bundles: community admin descriptor (404 = not activated in the test community: informational).
        # There is no per-bundle GET in the marketplace API (DELETE only), so apps are not described individually.
        admin_s, ms = None, 0.0
        if is_service:
            admin_s, ms, _ = cli.call("GET", f"/api/v1/apps/community/{community}/bundles/{b['app_id']}/admin")
        s = admin_s or 0
        described = bool(admin_s and 200 <= admin_s < 300)
        err = []
        if not resolved:
            err.append("unresolved(no active version/artifact)")
        elif not loaded:
            err.append(f"precompile_errors={b['precompile_errors']}")
        if invoked == "failed":
            err.append("dispatch failures, no success")
        if (admin_s or 0) >= 500 or admin_s == 0:
            err.append(f"descriptor HTTP {admin_s}")
        if a.require_invoked and not is_service and invoked == "never-observed":
            err.append("command never observed on data plane")
        rows.append({
            "app_id": b["app_id"], "kind": kind, "runtime": RUNTIME.get(b["language"], b["language"] or "unknown"),
            "command": b["command"], "resolved": resolved, "loaded": loaded, "invoked": invoked,
            "described": described, "admin_describe": admin_s, "latency_ms": round(ms),
            "id_ok": bool(GOOD_ID.match(b["app_id"])), "error": "; ".join(err),
        })

    harness = None
    if a.harness:
        r = subprocess.run(["make", "-C", str(ROOT), "test-bundle-flag-on-command-e2e"], capture_output=True, text=True)
        harness = {"exit": r.returncode, "tail": r.stdout.strip().splitlines()[-3:]}

    n = len(rows)
    failed = [r for r in rows if r["error"]]
    old = [r for r in rows if not r["id_ok"]]
    summary = {
        "bundles": n, "ok": n - len(failed), "failed": len(failed),
        "resolved": sum(r["resolved"] for r in rows), "loaded": sum(r["loaded"] for r in rows),
        "invoked_observed_ok": sum(r["invoked"] == "observed-ok" for r in rows),
        "invoked_never_observed": sum(r["invoked"] == "never-observed" for r in rows),
        "service_descriptor_ok": sum(r["described"] for r in rows),
        "service_descriptor_404_not_activated": sum(r["admin_describe"] == 404 for r in rows),
        "marketplace_list_status": ls,
        "non_conforming_app_ids": len(old), "conforming_app_ids": n - len(old),
        "by_kind": {k: sum(r["kind"] == k for r in rows) for k in sorted({r["kind"] for r in rows})},
        "by_runtime": {k: sum(r["runtime"] == k for r in rows) for k in sorted({r["runtime"] for r in rows})},
        "harness": harness,
    }
    out = Path(a.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bundle-sweep.json").write_text(json.dumps({"summary": summary, "bundles": rows}, indent=1))
    md = ["# bundle sweep", "", json.dumps({k: v for k, v in summary.items() if k != "harness"}), "",
          "## Failed", "", "| app_id | kind | runtime | error |", "|---|---|---|---|"]
    md += [f"| {r['app_id']} | {r['kind']} | {r['runtime']} | {r['error'][:90]} |" for r in failed]
    md += ["", "## Non-conforming app_ids", ""] + [f"- {r['app_id']}" for r in old]
    (out / "bundle-sweep.md").write_text("\n".join(md) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("harness", "by_runtime")}))
    print(f"report: {out}/bundle-sweep.json , bundle-sweep.md")
    if failed or (harness and harness["exit"]) or (old and not a.allow_old_ids):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
