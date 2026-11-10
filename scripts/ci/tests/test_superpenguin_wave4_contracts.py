"""Contracts of the wave-4 SuperPenguinTV C# apps: attribution, generated-manifest freshness, overlay-asset provenance."""

from __future__ import annotations

import hashlib
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WAVE4 = ("giveaway", "bonustickets", "wheel", "audiocommand", "tts", "channelpoints")
#: Binary media of unclear upstream provenance must never ship inside a bundle (see each overlay/_ASSETS.md).
FORBIDDEN_SUFFIXES = {".mp3", ".ogg", ".wav", ".gif", ".mp4", ".webm", ".woff", ".woff2", ".ttf", ".otf", ".png", ".jpg", ".jpeg"}


def _generator():
    """Load scripts/gen-superpenguin-manifests.py (a hyphenated filename cannot be imported normally)."""
    spec = importlib.util.spec_from_file_location("gen_superpenguin_manifests", ROOT / "scripts" / "gen-superpenguin-manifests.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dir(slug: str) -> Path:
    return ROOT / "bundles" / "csharp" / f"superpenguin-{slug}"


def test_the_denominator_is_all_six_wave4_bundles():
    """A mis-pointed path must not pass vacuously: every wave-4 bundle directory and source must exist."""
    found = [slug for slug in WAVE4 if (_dir(slug) / "manifest.src.yaml").is_file()]
    assert found == list(WAVE4)


@pytest.mark.parametrize("slug", WAVE4)
def test_generated_manifests_are_current_and_carry_the_attribution_contract(slug):
    """bundle.yaml/hub-manifest.yaml equal a fresh generation; author, collection, MIT notice are fixed."""
    gen = _generator()
    directory = _dir(slug)
    src = yaml.safe_load((directory / "manifest.src.yaml").read_text())
    assets = gen.overlay_assets(directory, src.get("overlay_origins", {}))
    bundle = yaml.safe_load((directory / "bundle.yaml").read_text())
    install = yaml.safe_load((directory / "hub-manifest.yaml").read_text())
    assert bundle == gen.build(src, install=False, assets=assets), "bundle.yaml is stale: make gen-superpenguin-manifests"
    assert install == gen.build(src, install=True, assets=assets), "hub-manifest.yaml is stale: make gen-superpenguin-manifests"
    for manifest in (bundle, install):
        assert manifest["author"] == "github/psychoboy"
        assert manifest["collection"] == "superpenguintv"
        assert manifest["license"] == "MIT"
        assert manifest["provider"] == "thirdparty"
        assert manifest["language"] == "csharp"
        assert manifest["category"] == "alternatives"
        assert "Copyright (c) 2023 Psychoboy" in manifest["notice"]
        assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in manifest["notice"]
    assert bundle["permissions"] == install["permissions"]
    assert bundle["requested_tools"] == install["requested_tools"]
    assert bundle["app_id"] == f"waddles.integrations.superpenguin.{slug}"


@pytest.mark.parametrize("slug", WAVE4)
def test_apps_use_service_tools_for_money_and_never_request_wit_economy_grants(slug):
    """Apps own policy only: no economy.* WIT grant; every economy tool request has a matching write-flagged grant."""
    manifest = yaml.safe_load((_dir(slug) / "bundle.yaml").read_text())
    grants = {permission["id"]: permission for permission in manifest["permissions"]}
    assert not any(key.startswith("economy.") for key in grants)
    for request in manifest["requested_tools"]:
        grant = grants[f"tool.call:{request['target_app_id']}:{request['tool']}"]
        assert grant["params"]["write"] == (request["access"] == "write")
    assert "storage.kv" in grants
    assert manifest["limits"]["timeout_ms"] <= 2000


@pytest.mark.parametrize("slug", WAVE4)
def test_overlay_assets_are_declared_hashed_and_free_of_unclear_binary_media(slug):
    """Every shipped overlay file is in the manifest with a matching sha256 and an origin; no binary media ships."""
    directory = _dir(slug)
    manifest = yaml.safe_load((directory / "bundle.yaml").read_text())
    root = directory / "overlay"
    declared = {entry["path"]: entry for entry in manifest.get("overlay_assets", [])}
    shipped = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith("_") and p.suffix != ".md"
    ) if root.is_dir() else []
    assert sorted(declared) == shipped
    for path in shipped:
        entry = declared[path]
        assert entry["sha256"] == hashlib.sha256((root / path).read_bytes()).hexdigest()
        assert entry["origin"] and entry["license"] == "MIT"
        assert Path(path).suffix.lower() not in FORBIDDEN_SUFFIXES, f"{slug}: binary media {path} must not ship"
    if root.is_dir():
        assert (root / "_ASSETS.md").is_file(), f"{slug}: overlay/_ASSETS.md (shipped vs not shipped) is required"
        index = (root / "index.html").read_text()
        assert "innerHTML" not in index and "onclick=" not in index and "eval(" not in index


@pytest.mark.parametrize("slug", WAVE4)
def test_project_wiring(slug):
    """stage-next-tools world, shared sources linked and copied into the build image, flag key documented in source."""
    directory = _dir(slug)
    project = ET.parse(directory / f"superpenguin-{slug}.csproj")
    assert project.find(".//Wit").attrib["World"] == "stage-next-tools"
    assert "superpenguin-shared" in (directory / "Dockerfile").read_text()
    source = "\n".join(path.read_text() for path in directory.glob("*Logic.cs"))
    assert f"waddles.command-superpenguin-{slug}" in source
    assert (directory / "README.md").is_file() and (directory / "LICENSE").is_file()
    assert list((directory / "tests").glob("*LogicTests.cs"))
