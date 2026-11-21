#!/usr/bin/env python3
"""hub-api endpoint sweep: every OpenAPI route, authenticated as admin.

Why: after the app_id hard cut-over we need proof that every route still
answers without a 5xx and that no response leaks an old hard-coded bundle id.
GET routes run with real ids discovered from listing endpoints; mutating
routes are NEVER sent real ids -- they get bogus ids and an empty body and
must fail with a 4xx (unless --allow-mutations). Stdlib + PyYAML only.

Env: BASE_URL, HOST_HEADER (optional), HUB_TEST_EMAIL, HUB_TEST_PASS
(or INITIAL_ADMIN_PASSWORD), COMMUNITY_ID, INSECURE=1 (self-signed port-forward).
The password is only ever read from the environment; it is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import yaml

OLD_PREFIXES = (
    "waddles.core.example",
    "waddles.core.app",
    "waddles.core.service",
    "waddles.core.integration",
    "waddles.core.community",
    "waddles.integrations.superpenguin",
)
# No-param mutating routes that could do real damage with an empty body.
DESTRUCTIVE = re.compile(r"logout|revoke|delete|reset|rotate|purge|wipe|clear|disable|deactivate|shutdown|restart", re.I)
PARAM = re.compile(r"\{([^}]+)\}")
# 402 = licence gate (Professional+/Enterprise feature), an expected refusal.
VALIDATION_OK = {400, 401, 402, 403, 404, 405, 409, 413, 415, 422, 429}


class Client:
    """Throttled JSON client with 429 backoff and bearer auth."""

    def __init__(self, base: str, host: str | None, rate: float, insecure: bool) -> None:
        self.base = base.rstrip("/")
        self.host = host
        self.gap = 1.0 / rate
        self.token: str | None = None
        self.last = 0.0
        self.dead = 0  # consecutive connect failures
        # Unverified TLS is honoured ONLY for loopback (kubectl port-forward to a self-signed pod cert).
        loopback = re.match(r"https?://(localhost|127\.0\.0\.1)(:|/|$)", self.base) is not None
        self.ctx = ssl._create_unverified_context() if (insecure and loopback) else None

    def call(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None):
        """Return (status, latency_ms, body_text); retries 429 up to 3 times."""
        for attempt in range(6):
            wait = self.gap - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            req = urllib.request.Request(self.base + path, data=body, method=method)
            req.add_header("Accept", "application/json")
            if ctype:
                req.add_header("Content-Type", ctype)
            if self.host:
                req.add_header("Host", self.host)
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=30, context=self.ctx) as r:
                    status, text, hdrs = r.status, r.read(2_000_000).decode("utf-8", "replace"), r.headers
            except urllib.error.HTTPError as e:
                status, text, hdrs = e.code, e.read(2_000_000).decode("utf-8", "replace"), e.headers
            except Exception as e:  # network/timeout -> status 0, reported as FAIL
                status, text, hdrs = 0, f"{type(e).__name__}: {e}", {}
            self.dead = self.dead + 1 if status == 0 else 0
            if self.dead >= 12:
                sys.exit('FAIL: target unreachable (12 consecutive connection errors); aborting instead of reporting noise')
            self.last = time.monotonic()
            ms = (self.last - t0) * 1000
            if status == 0 and attempt < 3:  # transient connect error (port-forward respawn): retry
                time.sleep(3)
                continue
            if status == 429:
                time.sleep(min(int(hdrs.get("Retry-After", "20") or 20), 65))
                continue
            return status, ms, text
        return 429, 0.0, "rate-limited after retries"


def norm(p: str) -> str:
    """Normalise a path template so param names do not matter for diffing."""
    return PARAM.sub("{}", p)


def bogus(schema: dict) -> str:
    """A syntactically valid id that cannot exist."""
    if (schema or {}).get("format") == "uuid":
        return str(uuid.uuid4())
    if (schema or {}).get("type") == "integer":
        return "999999999"
    return "sweep-nonexistent"


def harvest(obj, path: str, ids: dict[str, list[str]]) -> None:
    """Collect ids from a JSON body, keyed by the param names they could fill."""
    seg = [s for s in path.split("/") if s and not s.startswith("{")]
    coll = seg[-1] if seg else ""
    singular = coll[:-3] + "y" if coll.endswith("ies") else coll.rstrip("s")

    def add(name: str, val) -> None:
        if isinstance(val, (str, int)) and not isinstance(val, bool) and str(val):
            bucket = ids[name]
            if str(val) not in bucket and len(bucket) < 5:
                bucket.append(str(val))

    def walk(o, depth=0) -> None:
        if depth > 4:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                lk = re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower()
                if lk.endswith(("_id", "_slug", "_uuid")) or lk in ("slug", "app_id"):
                    add(lk, v)
                if lk in ("id", "public_id", "uuid", "slug", "name") and singular:
                    add(f"{singular}_{'id' if lk != 'slug' else 'slug'}", v)
                walk(v, depth + 1)
        elif isinstance(o, list):
            for i in o[:10]:
                walk(i, depth + 1)

    walk(obj)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--spec", default=str(Path(__file__).resolve().parents[3] / "openapi" / "v1.yaml"))
    ap.add_argument("--url-map", help="JSON from dump_url_map.py (optional cross-check)")
    ap.add_argument("--report-dir", default=os.environ.get("REPORT_DIR", "/tmp/api-sweep-report"))
    ap.add_argument("--rate", type=float, default=float(os.environ.get("SWEEP_RATE", "1.4")), help="req/s (hub-api limits 100/min per user)")
    ap.add_argument("--allow-mutations", action="store_true", help="send REAL mutating requests (off by default)")
    ap.add_argument("--limit", type=int, default=0, help="debug: only first N operations")
    ap.add_argument("--allow-old-ids", action="store_true", help="old-prefix hits do not fail the run (pre-cutover baseline)")
    a = ap.parse_args()

    base = os.environ.get("BASE_URL", "https://waddles-alpha.penguintech.cloud:8443")
    email = os.environ.get("HUB_TEST_EMAIL", "")
    pw = os.environ.get("HUB_TEST_PASS") or os.environ.get("INITIAL_ADMIN_PASSWORD", "")
    community = os.environ.get("COMMUNITY_ID", "")  # empty -> discovered after login (never a stale hard-coded id)
    if not (email and pw):
        print("FAIL: HUB_TEST_EMAIL and HUB_TEST_PASS (or INITIAL_ADMIN_PASSWORD) must be set", file=sys.stderr)
        return 2

    spec = yaml.safe_load(open(a.spec, encoding="utf-8"))
    ops = []
    for p, item in spec["paths"].items():
        shared = item.get("parameters", [])
        for m, op in item.items():
            if m in ("get", "post", "put", "patch", "delete"):
                params = {x["name"]: x.get("schema", {}) for x in shared + op.get("parameters", []) if x.get("in") == "path"}
                for n in PARAM.findall(p):
                    params.setdefault(n, {})
                ops.append({"method": m.upper(), "path": p, "params": params, "op": op})
    if a.limit:
        ops = ops[: a.limit]
    if not ops:
        print("FAIL: spec yielded zero operations", file=sys.stderr)
        return 2

    cli = Client(base, os.environ.get("HOST_HEADER"), a.rate, os.environ.get("INSECURE") == "1")
    st, _, txt = cli.call("POST", "/api/v1/auth/login", json.dumps({"email": email, "password": pw}).encode(), "application/json")
    try:
        cli.token = json.loads(txt)["token"] if st == 200 else None
    except Exception:
        cli.token = None
    if not cli.token:
        print(f"FAIL: admin login returned HTTP {st}", file=sys.stderr)
        return 2

    if not community:
        # A hard-coded id goes stale when alpha is wiped, which turns every community route into
        # a bogus 404 "contract" failure; use a community the API itself lists.
        st_c, _, txt_c = cli.call("GET", "/api/v1/public/communities")
        try:
            community = str(json.loads(txt_c)["communities"][0]["id"]) if st_c == 200 else ""
        except (KeyError, IndexError, ValueError, TypeError):
            community = ""
        if not community:
            print(f"FAIL: no COMMUNITY_ID set and none discoverable (HTTP {st_c})", file=sys.stderr)
            return 2
    ids: dict[str, list[str]] = defaultdict(list)
    ids["community_id"].append(community)
    results: list[dict] = []
    old_hits: Counter = Counter()

    def scan(row: dict, text: str) -> None:
        hit = [p for p in OLD_PREFIXES if p in text]
        if hit:
            row["old_prefix"] = hit
            for h in hit:
                old_hits[h] += 1

    def run_get(o: dict, vals: dict[str, str]) -> None:
        path = PARAM.sub(lambda m: vals[m.group(1)], o["path"])
        st, ms, text = cli.call("GET", path)
        row = {"method": "GET", "route": o["path"], "status": st, "ms": round(ms)}
        if st >= 500 or st == 0:
            row["class"], row["why"] = "FAIL", f"HTTP {st}: {text[:120]}"
        elif 200 <= st < 300:
            row["class"] = "PASS"
            try:
                harvest(json.loads(text), o["path"], ids)
            except Exception:
                pass
        elif st in (401, 403) and o["path"].startswith("/api/v1/internal/"):
            row["class"], row["why"] = "PASS", f"{st} internal route (service JWT only)"
        elif st in (400, 422) and any(x.get("in") == "query" and x.get("required") for x in o["op"].get("parameters", [])):
            row["class"], row["why"] = "PASS", f"{st} required query param not supplied"
        elif st in (401,):
            row["class"], row["why"] = "FAIL", "401 with valid admin token"
        elif st == 403:
            row["class"], row["why"] = "PASS", "403 scope"
        elif st in (400, 422, 405) or (st == 404 and o["params"]):
            row["class"], row["why"] = "FAIL", f"{st} on a request built from discovered ids (contradicts spec)"
        elif st == 404:
            row["class"], row["why"] = "FAIL", "404 on parameterless route"
        else:
            row["class"], row["why"] = "PASS", f"{st}"
        scan(row, text)
        results.append(row)

    def run_mut(o: dict) -> None:
        vals = {n: bogus(s) for n, s in o["params"].items()}
        path = PARAM.sub(lambda m: vals[m.group(1)], o["path"])
        has_body = "requestBody" in o["op"]
        body = b"{}" if has_body else None
        st, ms, text = cli.call(o["method"], path, body, "application/json" if has_body else None)
        row = {"method": o["method"], "route": o["path"], "status": st, "ms": round(ms), "probe": "bogus-ids+empty-body"}
        if st >= 500 or st == 0:
            row["class"], row["why"] = "FAIL", f"HTTP {st}: {text[:120]}"
        elif st in VALIDATION_OK:
            row["class"] = "PASS"
        else:
            row["class"], row["why"] = "FAIL", f"{st}: must-fail request was accepted"
        scan(row, text)
        results.append(row)

    skipped: list[dict] = []

    def skip(o: dict, why: str) -> None:
        results.append({"method": o["method"], "route": o["path"], "class": "SKIPPED", "why": why})
        skipped.append(results[-1])

    gets = [o for o in ops if o["method"] == "GET"]
    muts = [o for o in ops if o["method"] != "GET"]
    flat = [o for o in gets if not o["params"]]
    deep = [o for o in gets if o["params"]]
    for o in flat:
        run_get(o, {})
    for o in deep:
        vals, miss = {}, []
        for n in o["params"]:
            c = ids.get(n) or (ids.get("app_id") if n.endswith("app_id") else None)
            if c:
                vals[n] = c[0]
            else:
                miss.append(n)
        if miss:
            # Still probe with bogus ids: must not 5xx.
            bv = {n: (vals.get(n) or bogus(o["params"][n])) for n in o["params"]}
            path = PARAM.sub(lambda m: bv[m.group(1)], o["path"])
            st, ms, text = cli.call("GET", path)
            row = {"method": "GET", "route": o["path"], "class": "SKIPPED", "why": f"no ids for {','.join(miss)}", "probe_status": st}
            if st >= 500 or st == 0:
                row["class"], row["why"] = "FAIL", f"bogus-id probe HTTP {st}: {text[:100]}"
            scan(row, text)
            results.append(row)
            if row["class"] == "SKIPPED":
                skipped.append(row)
        else:
            run_get(o, vals)

    for o in muts:
        if a.allow_mutations:
            vals = {n: (ids.get(n) or [bogus(s)])[0] for n, s in o["params"].items()}
            path = PARAM.sub(lambda m: vals[m.group(1)], o["path"])
            body = b"{}" if "requestBody" in o["op"] else None
            st, ms, text = cli.call(o["method"], path, body, "application/json" if body else None)
            row = {"method": o["method"], "route": o["path"], "status": st, "ms": round(ms), "class": "FAIL" if st >= 500 or st == 0 else "PASS", "probe": "REAL"}
            scan(row, text)
            results.append(row)
        elif not o["params"] and (DESTRUCTIVE.search(o["path"]) or "requestBody" not in o["op"] or o["method"] == "DELETE"):
            skip(o, "no path ids and (no body schema or destructive name): an empty request could really execute")
        else:
            run_mut(o)

    # url_map cross-check
    missing_spec: list[str] = []
    missing_code: list[str] = []
    if a.url_map:
        um = json.load(open(a.url_map))
        code = {(m, norm(r["rule"])) for r in um for m in r["methods"]}
        specset = {(o["method"], norm(o["path"])) for o in ops}
        missing_spec = sorted(f"{m} {p}" for m, p in code - specset)
        missing_code = sorted(f"{m} {p}" for m, p in specset - code)

    cnt = Counter(r["class"] for r in results)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        if r["class"] == "FAIL":
            groups[(r.get("why", "")[:40] or str(r.get("status")))].append(r)
    lat = sorted(r["ms"] for r in results if "ms" in r)
    summary = {
        "target": base, "examined": len(results), "counts": dict(cnt),
        "mutations_enabled": a.allow_mutations,
        "old_prefix_hits": dict(old_hits), "old_prefix_routes": sum(1 for r in results if "old_prefix" in r),
        "p50_ms": lat[len(lat) // 2] if lat else 0, "p95_ms": lat[int(len(lat) * 0.95)] if lat else 0,
        "routes_missing_from_spec": missing_spec, "spec_routes_missing_from_code": missing_code,
        "skipped_reasons": dict(Counter(s["why"] for s in skipped)),
    }
    out = Path(a.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "api-sweep.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    md = [f"# hub-api sweep ({base})", "", f"examined={len(results)} " + " ".join(f"{k}={v}" for k, v in sorted(cnt.items())),
          f"old-prefix routes={summary['old_prefix_routes']} hits={dict(old_hits)}",
          f"p50={summary['p50_ms']}ms p95={summary['p95_ms']}ms; missing from spec={len(missing_spec)}", "", "## FAIL", "", "| method | route | status | why |", "|---|---|---|---|"]
    md += [f"| {r['method']} | {r['route']} | {r.get('status', r.get('probe_status', ''))} | {r.get('why', '')[:100]} |" for r in results if r["class"] == "FAIL"]
    md += ["", "## SKIPPED reasons", ""] + [f"- {k}: {v}" for k, v in summary["skipped_reasons"].items()]
    md += ["", "## Routes missing from spec", ""] + [f"- {x}" for x in missing_spec]
    md += ["", "## Old-prefix responses", ""] + [f"- {r['method']} {r['route']}: {r['old_prefix']}" for r in results if "old_prefix" in r]
    (out / "api-sweep.md").write_text("\n".join(md) + "\n")
    print(json.dumps({k: summary[k] for k in ("examined", "counts", "old_prefix_hits", "old_prefix_routes", "p95_ms")}))
    print(f"report: {out}/api-sweep.json , api-sweep.md")
    if cnt.get("FAIL"):
        return 1
    if old_hits and not a.allow_old_ids:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
