"""The license client must resolve tiers against the WADDLES product, never penguin-licensing's default.

# regression: LICENSE PRODUCT. `penguin_licensing.get_license_client()` (0.1.0) builds
# `LicenseClient()` whose default is `product="elder"`, so every tier Waddles resolved came
# from Elder's licences on license.penguintech.io -- wrong tier -> wrong feature gating
# (flagged by the tier-enforcement + docs audits). `entitlement.py` now builds its own
# client pinned to `LICENSE_PRODUCT` and never imports `get_license_client`.

Layers, each able to fail on its own:
  1. the built client carries `waddles` (and the library default is NOT what we get);
  2. the HTTP request body sent to the license server names `waddles` (end to end);
  3. a static audit of every Python source file: no `get_license_client()` /
     `init_license_client()` call, no `LicenseClient(...)` without an explicit non-elder
     product -- with built-in mutation snippets so a scanner that stopped matching fails;
  4. the Python constant equals the Rust data plane's `LICENSE_PRODUCT` constants.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

import pytest

import flask_core.entitlement as entitlement_module
from flask_core.entitlement import (
    LICENSE_PRODUCT,
    PenguinLicenseGate,
    build_waddles_license_client,
    get_waddles_license_client,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
LIBRARY_DEFAULT_PRODUCT = "elder"


@pytest.fixture(autouse=True)
def _fresh_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test builds its own client: no shared singleton, no ambient license env."""
    monkeypatch.setattr(entitlement_module, "_license_client", None)
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    monkeypatch.delenv("LICENSE_SERVER_URL", raising=False)


# ---------------------------------------------------------------------------
# 1. the constructed client
# ---------------------------------------------------------------------------
def test_product_constant_is_waddles() -> None:
    assert LICENSE_PRODUCT == "waddles"
    assert LICENSE_PRODUCT != LIBRARY_DEFAULT_PRODUCT


def test_built_client_is_pinned_to_waddles_not_the_library_default() -> None:
    client = build_waddles_license_client()
    assert client.product == "waddles"
    assert client.product != LIBRARY_DEFAULT_PRODUCT


def test_library_helper_is_the_trap_this_fix_avoids() -> None:
    """Canary for the root cause: the library's own shared client is the elder one."""
    from penguin_licensing import LicenseClient

    assert LicenseClient().product == LIBRARY_DEFAULT_PRODUCT  # the default we must never rely on
    assert build_waddles_license_client().product == "waddles"


def test_entitlement_module_no_longer_exposes_the_elder_singleton() -> None:
    assert not hasattr(entitlement_module, "get_license_client")


def test_gate_from_env_wraps_a_waddles_client() -> None:
    gate = PenguinLicenseGate.from_env()
    assert isinstance(gate, PenguinLicenseGate)
    assert gate._client.product == "waddles"  # type: ignore[attr-defined]


def test_shared_client_is_a_singleton_so_the_validation_cache_is_shared() -> None:
    first = get_waddles_license_client()
    assert get_waddles_license_client() is first
    assert first.product == "waddles"


def test_license_key_and_server_url_still_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LICENSE_KEY", "PENG-TEST-1111-2222-3333-4444")
    monkeypatch.setenv("LICENSE_SERVER_URL", "https://license.example.test")
    client = build_waddles_license_client()
    assert client.license_key == "PENG-TEST-1111-2222-3333-4444"
    assert client.base_url == "https://license.example.test"
    assert client.product == "waddles"


def test_default_server_url_when_unset() -> None:
    assert build_waddles_license_client().base_url == "https://license.penguintech.io"


# ---------------------------------------------------------------------------
# 2. the wire request
# ---------------------------------------------------------------------------
class _FakeResponse:
    status_code = 200
    text = "fake"

    def json(self) -> dict[str, Any]:
        return {
            "customer": "acme",
            "product": "waddles",
            "license_version": "2.0",
            "license_key": "PENG-TEST-0000",
            "expires_at": "2099-01-01T00:00:00Z",
            "issued_at": "2026-01-01T00:00:00Z",
            "tier": "enterprise",
            "features": [],
        }


def test_validate_request_body_names_the_waddles_product(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LICENSE_KEY", "PENG-TEST-0000-0000-0000-0000")
    sent: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: float) -> _FakeResponse:
        sent.append({"url": url, "json": json})
        return _FakeResponse()

    client = get_waddles_license_client()
    monkeypatch.setattr(client.session, "post", fake_post)

    assert PenguinLicenseGate.from_env().resolve_tier() == "enterprise"
    assert len(sent) == 1  # denominator: the request really went through the gate
    assert sent[0]["json"] == {"product": "waddles"}
    assert sent[0]["url"].endswith("/api/v2/validate")


# ---------------------------------------------------------------------------
# fail-loud paths
# ---------------------------------------------------------------------------
def test_product_mismatch_raises_instead_of_resolving_the_wrong_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _IgnoresProduct:
        product = LIBRARY_DEFAULT_PRODUCT

        def __init__(self, **_: Any) -> None:
            return None

    monkeypatch.setattr(entitlement_module, "LicenseClient", _IgnoresProduct)
    with pytest.raises(RuntimeError, match="product mismatch"):
        build_waddles_license_client()


def test_missing_library_raises_instead_of_defaulting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entitlement_module, "LicenseClient", None)
    with pytest.raises(RuntimeError, match="not installed"):
        build_waddles_license_client()


def test_build_logs_product_and_never_the_license_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "PENG-SECRET-9999-8888-7777-6666"
    monkeypatch.setenv("LICENSE_KEY", secret)
    with caplog.at_level(logging.INFO, logger="flask_core.entitlement"):
        build_waddles_license_client()
    records = [r for r in caplog.records if r.getMessage() == "entitlement.license_client_built"]
    assert len(records) == 1
    assert records[0].product == "waddles"  # type: ignore[attr-defined]
    assert records[0].license_key_configured is True  # type: ignore[attr-defined]
    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# 3. static audit: nobody else constructs an elder-defaulted client
# ---------------------------------------------------------------------------
_SKIP_DIRS = {".venv", "venv", "node_modules", ".git", ".worktrees", "target", "__pycache__", "site-packages"}
_BANNED_HELPERS = {"get_license_client", "init_license_client"}


def audit_license_client_source(source: str) -> list[str]:
    """Return one finding per call that could resolve a tier against penguin-licensing's default product."""
    findings: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in _BANNED_HELPERS:
            findings.append(f"line {node.lineno}: {name}() hardcodes product='elder'")
        elif name == "LicenseClient":
            product: ast.expr | None = next((k.value for k in node.keywords if k.arg == "product"), None)
            if product is None and len(node.args) >= 2:
                product = node.args[1]
            if product is None:
                findings.append(f"line {node.lineno}: LicenseClient() without product= defaults to 'elder'")
            elif isinstance(product, ast.Constant) and product.value == LIBRARY_DEFAULT_PRODUCT:
                findings.append(f"line {node.lineno}: LicenseClient(product='elder')")
    return findings


#: Pre-fix / elder-defaulted shapes: each MUST be flagged (a scanner that goes quiet fails here).
UNSAFE = {
    "library-helper": "from penguin_licensing import get_license_client\nc = get_license_client()\n",
    "attr-helper": "import penguin_licensing\nc = penguin_licensing.get_license_client()\n",
    "init-helper": "from penguin_licensing.client import init_license_client\ninit_license_client(app)\n",
    "no-product": "c = LicenseClient(license_key=k)\n",
    "explicit-elder": "c = LicenseClient(license_key=k, product='elder')\n",
    "positional-elder": "c = LicenseClient(k, 'elder')\n",
}
#: Post-fix shapes: each MUST pass.
SAFE = {
    "kw-waddles": "c = LicenseClient(license_key=k, product='waddles')\n",
    "const": "c = LicenseClient(license_key=k, product=LICENSE_PRODUCT)\n",
    "positional-waddles": "c = LicenseClient(k, 'waddles')\n",
}


@pytest.mark.parametrize("case", sorted(UNSAFE))
def test_audit_flags_every_elder_defaulted_shape(case: str) -> None:
    assert audit_license_client_source(UNSAFE[case]), case


@pytest.mark.parametrize("case", sorted(SAFE))
def test_audit_passes_every_pinned_shape(case: str) -> None:
    assert audit_license_client_source(SAFE[case]) == [], case


def _python_sources() -> list[Path]:
    return [
        p
        for p in REPO_ROOT.rglob("*.py")
        if not (_SKIP_DIRS & set(p.relative_to(REPO_ROOT).parts))
        and p.name != Path(__file__).name  # this file holds the UNSAFE mutation snippets
        and "tests" not in p.relative_to(REPO_ROOT).parts
        and not p.name.startswith("test_")
    ]


def test_no_python_source_constructs_an_elder_defaulted_license_client() -> None:
    sources = _python_sources()
    assert len(sources) > 200, f"scanned {len(sources)} files -- scanner pointed at the wrong root"
    findings: list[str] = []
    constructions = 0
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "LicenseClient" not in text and "license_client" not in text:
            continue
        try:
            tree_findings = audit_license_client_source(text)
        except SyntaxError:
            continue
        constructions += len(re.findall(r"\bLicenseClient\(", text))
        findings.extend(f"{path.relative_to(REPO_ROOT)}: {f}" for f in tree_findings)
    assert constructions >= 1, "found no LicenseClient construction at all -- the audit proved nothing"
    assert findings == []


# ---------------------------------------------------------------------------
# 4. one product id across the Python and Rust licence clients
# ---------------------------------------------------------------------------
_RUST_SOURCES = ("core/svc_action/src/lib.rs", "core/svc_presentation/src/flags.rs")
_RUST_CONST = re.compile(r'const\s+LICENSE_PRODUCT\s*:\s*&str\s*=\s*"([^"]+)"\s*;')


def test_python_product_matches_the_rust_data_plane_constants() -> None:
    found: dict[str, str] = {}
    for rel in _RUST_SOURCES:
        match = _RUST_CONST.search((REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert match is not None, f"no LICENSE_PRODUCT constant in {rel}"
        found[rel] = match.group(1)
    assert len(found) == len(_RUST_SOURCES)  # denominator
    assert set(found.values()) == {LICENSE_PRODUCT}, found
