#!/usr/bin/env python3
"""Report hub-webui <-> hub-api REST coverage gaps (report-only by default).

Categories:
  A        hub-api route (openapi/v1.yaml) the webui never calls
  B        webui call whose method+path matches no hub-api route (spec or code)
  C        spec <-> code drift: route in openapi but not registered in Quart, or vice versa
  B-proxy  path hub-api's calls proxy forwards that svc-streaming does not serve

Sources:
  spec   openapi/v1.yaml `paths:` (source of truth)
  code   Quart decorators under hub_api/blueprints (Blueprint url_prefix + @bp.route/get/...)
  webui  admin/hub_module/frontend/src (api/apiClient/client/axios/http .get/.post/... calls)
  proxy  hub_api/blueprints/v1/calls.py `_proxy_client.request("M", "path")` vs
         core/svc_streaming/src axum `.route("path", get(..).post(..))`
         (src/api/mod.rs is nested at /api/v1 by src/http/mod.rs)

Exclusion rules (apply to A and C only; B always reports):
  - /internal/ anywhere in the path (service-only)
  - health / healthz / readyz / livez / metrics endpoints
  - platform webhooks and callbacks (/webhook, /webhooks, /callback, /oauth/.../callback)
  - gRPC-only surfaces (paths containing /grpc/)
  - OpenAPI / docs endpoints (/openapi, /docs)

Path params ({id}, <int:id>, :id, ${expr}) normalise to `{}`; a trailing `${qs}`
glued to a segment is treated as a query string and dropped. A webui `{}` segment
matches any route segment (e.g. `/auth/oauth/${platform}`).

Exit status: 0 unless --strict and any A/B/C/B-proxy item exists. stdlib only.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

METHODS = ("get", "post", "put", "patch", "delete")
EXCLUDE_RES = [
    re.compile(p)
    for p in (
        r"/internal(/|$)",
        r"/(health|healthz|ready|readyz|livez|metrics)(/|$)",
        r"/webhooks?(/|$)",
        r"/callback(/|$)",
        r"/oauth/.*callback",
        r"/grpc(/|$)",
        r"/(openapi|docs)(/|\.|$)",
    )
]
DEMO_KEYWORDS = ("communit", "connection", "role-sync", "role-mirror", "guild", "pairing", "bundle", "apps")
SKIP_AREA = {"api", "v1", "v2", "admin", "community", "communities", "{}", ""}


@dataclass(frozen=True)
class Endpoint:
    """One method+path pair with its source location."""

    method: str
    path: str
    where: str

    @property
    def key(self) -> tuple[str, str]:
        """Normalised (METHOD, path) match key."""
        return (self.method.upper(), normalise(self.path))


def normalise(path: str) -> str:
    """Collapse every path-param style to `{}`, drop query strings and trailing slashes."""
    path = path.split("?", 1)[0]
    path = re.sub(r"(?<=[^/])\$\{[^}]*\}$", "", path)  # glued trailing ${qs}
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"<[^>]*>", "{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    path = re.sub(r"(?<=/):[A-Za-z_]\w*", "{}", path)
    path = re.sub(r"/+", "/", path)
    return path.rstrip("/") or "/"


def excluded(path: str) -> bool:
    """True when a route is internal/service-only per the header rules."""
    return any(r.search(path) for r in EXCLUDE_RES)


def feature_area(path: str) -> str:
    """Guess a feature area: first non-generic, non-param segment."""
    for seg in normalise(path).split("/"):
        if seg not in SKIP_AREA:
            return seg
    return "root"


def is_demo(path: str) -> bool:
    """True when the normalised path touches a demo-relevant feature."""
    return any(k in normalise(path) for k in DEMO_KEYWORDS)


def segments_match(call: str, route: str) -> bool:
    """Webui call path matches route path; a `{}` on either side matches any segment."""
    a, b = call.split("/"), route.split("/")
    return len(a) == len(b) and all(x == y or x == "{}" or y == "{}" for x, y in zip(a, b))


def find_match(call: Endpoint, routes: list[Endpoint]) -> tuple[bool, bool]:
    """Return (path_exists, method_matches) for a call against routes."""
    m, p = call.key
    path_hits = [r for r in routes if segments_match(p, r.key[1])]
    return bool(path_hits), any(r.key[0] == m for r in path_hits)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def parse_openapi(path: Path, rel: Path) -> list[Endpoint]:
    """Parse `paths:` from an OpenAPI YAML without a YAML library (2-space indent)."""
    out: list[Endpoint] = []
    in_paths, cur = False, None
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if re.match(r"^\S", line):
            in_paths = line.startswith("paths:")
            cur = None
            continue
        if not in_paths:
            continue
        m = re.match(r"^  (/[^:]*|'[^']*'|\"[^\"]*\"):\s*$", line)
        if m:
            cur = m.group(1).strip("'\"")
            continue
        m = re.match(r"^    (get|post|put|patch|delete):\s*$", line)
        if m and cur:
            out.append(Endpoint(m.group(1).upper(), cur, f"{rel}:{n}"))
    return out


_BP_RE = re.compile(r"(\w+)\s*=\s*Blueprint\((.*?)\)", re.DOTALL)
_DECO_RE = re.compile(r"@(\w+)\.(route|get|post|put|patch|delete)\(\s*([rf]?)([\"'])(.*?)\4(.*?)\)\s*\n", re.DOTALL)


def parse_quart(root: Path, base: Path) -> list[Endpoint]:
    """Parse Blueprint url_prefix + route decorators from every .py under root."""
    out: list[Endpoint] = []
    for f in sorted(root.rglob("*.py")):
        if "test" in f.parts or f.name.startswith("test_"):
            continue
        text = f.read_text()
        prefixes = {}
        for m in _BP_RE.finditer(text):
            pm = re.search(r"url_prefix\s*=\s*[\"']([^\"']*)", m.group(2))
            prefixes[m.group(1)] = pm.group(1) if pm else ""
        for m in _DECO_RE.finditer(text):
            var, kind, rest = m.group(1), m.group(2), m.group(6)
            if var not in prefixes:
                continue
            if kind == "route":
                mm = re.search(r"methods\s*=\s*[\[(]([^\])]*)", rest)
                methods = re.findall(r"[\"'](\w+)[\"']", mm.group(1)) if mm else ["GET"]
            else:
                methods = [kind]
            full = prefixes[var] + m.group(5)
            for meth in methods:
                if meth.upper() in ("HEAD", "OPTIONS"):
                    continue
                out.append(Endpoint(meth.upper(), full, f"{f.relative_to(base)}:{_line(text, m.start())}"))
    return out


_CALL_RE = re.compile(
    r"\b(?:api|apiClient|client|axios|http)\s*\.\s*(get|post|put|patch|delete)\s*(?:<(?:[^<>()]|<[^<>]*>)*>)?\s*\(\s*([`'\"])(.*?)\2",
    re.DOTALL,
)


def parse_webui(root: Path, base: Path) -> tuple[list[Endpoint], int]:
    """Scan frontend source (non-test) for literal-path HTTP calls; returns (calls, files scanned)."""
    out: list[Endpoint] = []
    files = 0
    for f in sorted(root.rglob("*")):
        if f.suffix not in (".ts", ".tsx", ".js", ".jsx") or "__tests__" in f.parts or ".test." in f.name or "test" in f.parts:
            continue
        files += 1
        text = f.read_text()
        for m in _CALL_RE.finditer(text):
            p = m.group(3)
            if not p.startswith("/api/"):
                continue
            out.append(Endpoint(m.group(1).upper(), p, f"{f.relative_to(base)}:{_line(text, m.start())}"))
    return out, files


_PROXY_RE = re.compile(r"_proxy_client\.request\(\s*[\"'](\w+)[\"']\s*,\s*f?[\"']([^\"']+)[\"']", re.DOTALL)


def parse_proxy(files: list[Path], base: Path) -> list[Endpoint]:
    """Extract (method, path) pairs the calls proxy forwards to svc-streaming."""
    out = []
    for f in files:
        text = f.read_text()
        for m in _PROXY_RE.finditer(text):
            out.append(Endpoint(m.group(1).upper(), m.group(2), f"{f.relative_to(base)}:{_line(text, m.start())}"))
    return out


RUST_PREFIX = {"src/api/mod.rs": "/api/v1"}


def parse_axum(root: Path, base: Path) -> list[Endpoint]:
    """Parse axum `.route("path", get(..).post(..))` with known nest prefixes."""
    out: list[Endpoint] = []
    for f in sorted(root.rglob("*.rs")):
        text = f.read_text()
        prefix = RUST_PREFIX.get(f.relative_to(root.parent if root.name == "src" else root).as_posix(), "")
        for m in re.finditer(r"\.route\(\s*\"([^\"]+)\"\s*,", text):
            depth, i = 1, m.end()
            while i < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
            body = text[m.end() : i]
            for meth in re.findall(r"(?<![\w:])(get|post|put|patch|delete)\(", body):
                out.append(Endpoint(meth.upper(), prefix + m.group(1), f"{f.relative_to(base)}:{_line(text, m.start())}"))
    return out


def build_report(repo: Path) -> tuple[list[tuple[str, Endpoint, str]], dict[str, int]]:
    """Run every comparison; returns rows (category, endpoint, note) and examined counts."""
    spec = parse_openapi(repo / "openapi/v1.yaml", Path("openapi/v1.yaml"))
    code = parse_quart(repo / "hub_api/blueprints", repo)
    calls, files = parse_webui(repo / "admin/hub_module/frontend/src", repo)
    proxy = parse_proxy([repo / "hub_api/blueprints/v1/calls.py", repo / "hub_api/services/calls_proxy.py"], repo)
    rust = parse_axum(repo / "core/svc_streaming/src", repo)
    counts = {
        "spec_routes": len(spec), "code_routes": len(code), "webui_files": files,
        "webui_calls": len(calls), "proxy_paths": len(proxy), "streaming_routes": len(rust),
    }
    for k, v in counts.items():
        if v == 0 and k != "proxy_paths":
            raise SystemExit(f"FAIL: zero {k} examined -- scanner pointed at the wrong root?")
    rows: list[tuple[str, Endpoint, str]] = []
    spec_keys = {e.key for e in spec}
    code_keys = {e.key for e in code}
    all_routes = spec + code
    seen_a: set[tuple[str, str]] = set()
    for e in spec:
        if excluded(e.path) or e.key in seen_a:
            continue
        seen_a.add(e.key)
        if not any(c.key[0] == e.key[0] and segments_match(c.key[1], e.key[1]) for c in calls):
            rows.append(("A", e, "uncovered"))
    seen_b: set[tuple[str, str]] = set()
    for c in calls:
        if c.key in seen_b:
            continue
        seen_b.add(c.key)
        path_ok, meth_ok = find_match(c, all_routes)
        if not meth_ok:
            rows.append(("B", c, "method-mismatch" if path_ok else "no-such-path"))
    for e in spec:
        if not excluded(e.path) and e.key not in code_keys:
            rows.append(("C", e, "in-spec-not-in-code"))
    seen_c: set[tuple[str, str]] = set()
    for e in code:
        if not excluded(e.path) and e.key not in spec_keys and e.key not in seen_c:
            seen_c.add(e.key)
            rows.append(("C", e, "in-code-not-in-spec"))
    for p in proxy:
        path_ok, meth_ok = find_match(p, rust)
        if not meth_ok:
            rows.append(("B-proxy", p, "method-mismatch" if path_ok else "no-such-path"))
    return rows, counts


def render(rows: list[tuple[str, Endpoint, str]], counts: dict[str, int]) -> str:
    """Render a markdown report with counts, per-category tallies and the full table."""
    out = ["# webui <-> hub-api coverage", "", "## Examined", ""]
    out += [f"- {k}: {v}" for k, v in counts.items()]
    out += ["", "## Counts", ""]
    for cat in ("A", "B", "C", "B-proxy"):
        out.append(f"- {cat}: {sum(1 for r in rows if r[0] == cat)}")
    areas: dict[str, int] = {}
    for cat, e, _ in rows:
        if cat == "A":
            areas[feature_area(e.path)] = areas.get(feature_area(e.path), 0) + 1
    out += ["", "## A by feature area", "", "| area | count |", "|---|---|"]
    out += [f"| {a} | {n} |" for a, n in sorted(areas.items(), key=lambda x: -x[1])]
    out += ["", "## Items", "", "| category | method | path | file:line | area | note | demo |", "|---|---|---|---|---|---|---|"]
    for cat, e, note in sorted(rows, key=lambda r: (r[0], r[1].path, r[1].method)):
        demo = "yes" if is_demo(e.path) else ""
        out.append(f"| {cat} | {e.method} | `{e.path}` | {e.where} | {feature_area(e.path)} | {note} | {demo} |")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry: write the report, print counts, exit non-zero only under --strict."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--out", type=Path, help="write markdown report here (default stdout)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when any gap exists")
    args = ap.parse_args(argv)
    rows, counts = build_report(args.repo)
    report = render(rows, counts)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
    else:
        sys.stdout.write(report)
    tally = {c: sum(1 for r in rows if r[0] == c) for c in ("A", "B", "C", "B-proxy")}
    print("examined: " + " ".join(f"{k}={v}" for k, v in counts.items()), file=sys.stderr)
    print("gaps: " + " ".join(f"{k}={v}" for k, v in tally.items()), file=sys.stderr)
    return 1 if args.strict and rows else 0


if __name__ == "__main__":
    sys.exit(main())
