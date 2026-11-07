"""Tests for scripts/backfill_bundle_author.py and the state it left the repo's bundles in."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "backfill_bundle_author", _ROOT / "scripts" / "backfill_bundle_author.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_bump_patch() -> None:
    assert _mod.bump("1.0.9") == "1.0.10"
    assert _mod.bump("0.1.1") == "0.1.2"


def test_set_fields_replaces_existing_author_and_bumps_version() -> None:
    text = 'version: 1.0.0\nprovider: builtin\nauthor: "old (x)"\nlicense: MIT\n'
    out = _mod.set_fields(text, "github:penguintechinc", None, "1.0.1")
    assert (
        out
        == "version: 1.0.1\nprovider: builtin\nauthor: github:penguintechinc\nlicense: MIT\n"
    )


def test_set_fields_inserts_author_after_provider_and_adds_collection() -> None:
    text = "version: 2.3.4\nprovider: thirdparty\nlanguage: csharp\n"
    out = _mod.set_fields(text, "github:Psychoboy", "superpenguintv", "2.3.5")
    assert (
        "provider: thirdparty\nauthor: github:Psychoboy\ncollection: superpenguintv\n"
        in out
    )
    assert out.startswith("version: 2.3.5\n")


def test_set_fields_requires_a_provider_anchor() -> None:
    with pytest.raises(SystemExit):
        _mod.set_fields("version: 1.0.0\n", "github:x", None, "1.0.1")


def test_every_bundle_has_the_target_author_and_catalog_versions_match() -> None:
    catalog = yaml.safe_load((_ROOT / "bundles" / "core-bundles.yaml").read_text())
    catalog_versions = {e["app_id"]: e["version"] for e in catalog["bundles"]}
    examined = rows = 0
    for hub in sorted((_ROOT / "bundles").glob("*/*/hub-manifest.yaml")):
        raw = yaml.safe_load(hub.read_text())
        rel = f"{hub.parent.parent.name}/{hub.parent.name}"
        psycho = rel == "csharp/superpenguin-roll"
        assert raw["author"] == (
            "github:Psychoboy" if psycho else "github:penguintechinc"
        ), rel
        examined += 1
        if raw["app_id"] in catalog_versions:
            assert str(raw["version"]) == catalog_versions[raw["app_id"]], rel
            rows += 1
    assert examined >= 80 and rows >= 80, f"examined={examined} catalog-rows={rows}"


def test_python_bundle_pyproject_versions_match_manifests() -> None:
    examined = 0
    for pp in sorted((_ROOT / "bundles" / "python").glob("*/pyproject.toml")):
        m = re.search(r'^version = "([^"]+)"', pp.read_text(), re.MULTILINE)
        hub = pp.parent / "hub-manifest.yaml"
        if m and hub.exists():
            assert m.group(1) == str(yaml.safe_load(hub.read_text())["version"]), (
                pp.parent.name
            )
            examined += 1
    assert examined >= 50, f"examined only {examined}"
