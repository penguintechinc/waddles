"""Drift guard: every hub-api port reference matches the chart's `pipeline.hubApi.port`.

# regression: stale Node-era hub port (8060) left in beta routes, CI smoke, the vite dev proxy and test scripts

hub-api (Python/Quart, hub_api/Dockerfile) has one canonical HTTP port: `pipeline.hubApi.port` in
values.yaml, which renders its Service port and containerPort. This test asserts that (a) the image,
config defaults and every known tool/script reference agree with it, (b) every rendered Ingress /
HTTPRoute backend aimed at hub-api-v3 uses it for every env values file, and (c) the retired port
appears nowhere outside docs/. Zero items examined is a failure, and a planted stale port must fail.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_REPO = _CHART_DIR.parents[2]
_STALE_PORT = "80" + "60"  # split so this file never matches its own scan
_STALE_RE = re.compile(r"(?<![0-9A-Za-z])" + _STALE_PORT + r"(?![0-9A-Za-z])")

# file -> regex whose group(1) is a hub-api port reference (>=1 match required per file)
_ANCHORS: dict[str, str] = {
    "hub_api/Dockerfile": r"--bind\",\s*\"0\.0\.0\.0:(\d+)",
    "hub_api/config.py": r"\"MODULE_PORT\",\s*\"(\d+)\"",
    "hub_api/healthcheck.py": r"localhost:(\d+)/healthz",
    "admin/hub_module/frontend/vite.config.js": r"HUB_API_URL \|\| 'http://localhost:(\d+)'",
    "admin/hub_module/webui-server/server.js": r"HUB_API_URL \?\? 'http://localhost:(\d+)'",
    ".github/workflows/containers.yml": r"\"hub-api\|[^\"]*\|(\d+)\"",
    "scripts/ci/container-smoke.sh": r"HUB_API_URL=http://127\.0\.0\.1:(\d+)",
    "tests/e2e/local-proxy.js": r"API_PORT\s+\|\| '(\d+)'",
    "tests/api/hub-backend/setup.js": r"API_BASE_URL \|\| 'http://localhost:(\d+)'",
    "tests/smoke/run-all.sh": r"smoke-api-comprehensive\.sh http://localhost:(\d+)",
    "tests/smoke/smoke-api-comprehensive.sh": r"BASE_URL=\"\$\{1:-http://localhost:(\d+)\}\"",
    "tests/smoke/smoke-pages.js": r"replace\(':3000', ':(\d+)'\)",
    "test-all.sh": r"\"(\d+):Hub API\"",
}

_ENV_VALUES = sorted(_CHART_DIR.glob("values-*.yaml"))
# Overlays render on top of a base env file, exactly as their workflow installs them.
_OVERLAY_BASE = {"values-ci.yaml": "values-alpha.yaml"}


def _canonical_port() -> int:
    """The single source: values.yaml `pipeline.hubApi.port`."""
    values = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())
    port = values["pipeline"]["hubApi"]["port"]
    assert isinstance(port, int) and port > 0, f"pipeline.hubApi.port invalid: {port!r}"
    return int(port)


def _anchor_ports(text: str, pattern: str) -> list[int]:
    """All ports captured by `pattern` (any non-empty group) in `text`."""
    found: list[int] = []
    for m in re.finditer(pattern, text):
        found += [int(g) for g in m.groups() if g]
    return found


def _stale_hits(path: str, text: str) -> list[str]:
    """`path:line` for every standalone stale-port token in `text`."""
    return [f"{path}:{n}" for n, line in enumerate(text.splitlines(), 1) if _STALE_RE.search(line)]


def _tracked_text_files() -> list[tuple[str, str]]:
    """git-tracked text files outside docs/ (binaries skipped)."""
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(_REPO), "ls-files", "-z"],  # noqa: S607
        capture_output=True, check=True,
    ).stdout.decode()
    files: list[tuple[str, str]] = []
    for rel in filter(None, out.split("\0")):
        if rel.startswith("docs/"):
            continue
        p = _REPO / rel
        if not p.is_file():
            continue
        raw = p.read_bytes()
        if b"\0" in raw[:8192]:
            continue
        files.append((rel, raw.decode("utf-8", errors="replace")))
    return files


def test_anchor_references_match_chart_port() -> None:
    """Image, config defaults and tool/script references all equal pipeline.hubApi.port."""
    canon = _canonical_port()
    examined = 0
    for rel, pattern in _ANCHORS.items():
        ports = _anchor_ports((_REPO / rel).read_text(), pattern)
        assert ports, f"{rel}: no hub-api port reference matched {pattern!r} (anchor drifted?)"
        bad = [p for p in ports if p != canon]
        assert not bad, f"{rel}: hub-api port(s) {bad} != pipeline.hubApi.port {canon}"
        examined += len(ports)
    assert examined >= len(_ANCHORS), f"examined {examined} references"


def test_stale_port_absent_outside_docs() -> None:
    """The retired Node-era hub port is gone from every tracked non-doc file."""
    files = _tracked_text_files()
    assert len(files) > 100, f"only {len(files)} tracked files scanned -- wrong repo root?"
    hits = [h for rel, text in files for h in _stale_hits(rel, text)]
    assert not hits, f"stale hub port {_STALE_PORT} found: {hits}"


def test_planted_stale_port_is_caught() -> None:
    """The scanners themselves fail on a planted stale port (a gate that cannot fail is no gate)."""
    assert _stale_hits("x.js", f"target: 'http://localhost:{_STALE_PORT}',") == ["x.js:1"]
    assert _stale_hits("x.txt", f"sha256:5837b58e9f6e{_STALE_PORT}eecce") == []
    planted = f"target: process.env.HUB_API_URL || 'http://localhost:{_STALE_PORT}',"
    ports = _anchor_ports(planted, _ANCHORS["admin/hub_module/frontend/vite.config.js"])
    assert ports == [int(_STALE_PORT)] and ports[0] != _canonical_port()


helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not available")

_ROUTE_SETS = ("ingress.enabled=true", "httproute.enabled=true")


def _render(values: Path, *sets: str) -> subprocess.CompletedProcess[str]:
    """helm template with one env values file plus --set overrides."""
    argv = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.31.0"]
    if values.name in _OVERLAY_BASE:
        argv += ["-f", str(_CHART_DIR / _OVERLAY_BASE[values.name])]
    argv += ["-f", str(values)]
    for item in sets:
        argv += ["--set", item]
    return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603


def _hub_api_backend_ports(docs: list[dict[str, Any]]) -> list[int]:
    """Ports of every Ingress/HTTPRoute backend naming hub-api-v3."""
    ports: list[int] = []
    for d in docs:
        if d.get("kind") == "Ingress":
            for rule in d["spec"].get("rules") or []:
                for path in rule["http"]["paths"]:
                    svc = path["backend"]["service"]
                    if svc["name"].endswith("hub-api-v3"):
                        ports.append(int(svc["port"]["number"]))
        elif d.get("kind") == "HTTPRoute":
            for rule in d["spec"].get("rules") or []:
                for ref in rule.get("backendRefs") or []:
                    if str(ref["name"]).endswith("hub-api-v3"):
                        ports.append(int(ref["port"]))
    return ports


@helm
@pytest.mark.parametrize("values", _ENV_VALUES, ids=lambda p: p.name)
def test_rendered_hub_api_ports_match(values: Path) -> None:
    """Service, containerPort and every route backend to hub-api-v3 render pipeline.hubApi.port."""
    assert _ENV_VALUES, "no values-*.yaml files found"
    res = _render(values, *_ROUTE_SETS)
    assert res.returncode == 0, res.stderr
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "helm rendered zero documents"
    vals = yaml.safe_load(values.read_text()) or {}
    canon = ((vals.get("pipeline") or {}).get("hubApi") or {}).get("port") or _canonical_port()
    svc = next(d for d in docs if d.get("kind") == "Service" and d["metadata"]["name"].endswith("hub-api-v3"))
    assert {p["name"]: p["port"] for p in svc["spec"]["ports"]}["http"] == canon
    dep = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("hub-api-v3"))
    ctr = next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "hub-api")
    assert {p["name"]: p["containerPort"] for p in ctr["ports"]}["http"] == canon
    bad = [p for p in _hub_api_backend_ports(docs) if p != canon]
    assert not bad, f"{values.name}: hub-api route backend port(s) {bad} != {canon}"


@helm
def test_beta_routes_reach_hub_api() -> None:
    """values-beta's public routes actually target hub-api (non-zero denominator)."""
    res = _render(_CHART_DIR / "values-beta.yaml", *_ROUTE_SETS)
    assert res.returncode == 0, res.stderr
    ports = _hub_api_backend_ports([d for d in yaml.safe_load_all(res.stdout) if d])
    assert len(ports) >= 10, f"expected the beta hub-api routes, got {len(ports)}"
    assert set(ports) == {_canonical_port()}


@helm
@pytest.mark.parametrize(
    "prefix",
    ["ingress.hosts[0].paths[0]", "httproute.rules[0]"],
)
def test_planted_stale_route_port_fails_render(prefix: str) -> None:
    """A route pinning hub-api to the stale port fails the render instead of shipping."""
    res = _render(
        _CHART_DIR / "values-beta.yaml", *_ROUTE_SETS,
        f"{prefix}.port={_STALE_PORT}",
    )
    assert res.returncode != 0
    assert "hub-api serves on pipeline.hubApi.port" in res.stderr
