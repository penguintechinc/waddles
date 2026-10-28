"""Manifest regression: `blackjack` declares the bounds and permissions the host enforces.

The hub-api manifest parser rejects `economy.wager` without an integer `params.max_bet` in
1..=1000 (`invalid_economy_bound`). The declared bound is also the hard ceiling for every stake
this bundle accepts (`MANIFEST_MAX_BET`), so both manifest files must agree with the code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import app

BUNDLE_DIR = Path(__file__).resolve().parent.parent
MANIFESTS = ("bundle.yaml", "hub-manifest.yaml")
EXPECTED_PERMISSIONS = {
    "storage.kv",
    "economy.read",
    "economy.wager",
    "identity.resolve",
    "chat.send:discord",
    "chat.send:twitch",
}


def _load(manifest: str) -> dict[str, object]:
    raw = yaml.safe_load((BUNDLE_DIR / manifest).read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _permissions(manifest: str) -> list[dict[str, object]]:
    perms = _load(manifest)["permissions"]
    assert isinstance(perms, list)
    return perms


def _max_bet(manifest: str) -> object:
    matches = [p for p in _permissions(manifest) if p["id"] == "economy.wager"]
    assert len(matches) == 1, f"{manifest}: economy.wager declared {len(matches)} times"
    params = matches[0].get("params")
    assert isinstance(params, dict), f"{manifest}: economy.wager declares no params"
    return params["max_bet"]


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_declared_max_bet_is_valid_and_is_the_code_ceiling(manifest: str) -> None:
    bound = _max_bet(manifest)
    assert isinstance(bound, int) and not isinstance(bound, bool)
    assert 1 <= bound <= 1000  # the hub-api parser rule
    assert bound == app.MANIFEST_MAX_BET
    assert bound >= app.DEFAULT_MAX_BET >= app.DEFAULT_MIN_BET


def test_both_manifest_files_declare_the_same_max_bet() -> None:
    assert len({_max_bet(m) for m in MANIFESTS}) == 1


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_permissions_are_exactly_the_least_privilege_set(manifest: str) -> None:
    ids = [str(p["id"]) for p in _permissions(manifest)]
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_PERMISSIONS
    # No transfer (the game never pays a third party) and no moderation capability.
    assert not any(i.startswith(("economy.transfer", "chat.delete", "dm.send")) for i in ids)


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_justifications_fit_the_parser_limit(manifest: str) -> None:
    for perm in _permissions(manifest):
        assert 0 < len(str(perm["justification"])) <= 280


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_limits_are_inside_the_parser_ranges(manifest: str) -> None:
    limits = _load(manifest)["limits"]
    assert isinstance(limits, dict)
    assert 50 <= limits["timeout_ms"] <= 10_000
    assert 8 <= limits["memory_mb"] <= 256
    assert 1 <= limits["egress_rps"] <= 10


def test_both_manifest_files_agree_on_identity_and_version() -> None:
    a, b = (_load(m) for m in MANIFESTS)
    for field in ("app_id", "version", "name", "feature", "module", "provider"):
        assert a[field] == b[field], field
    assert a["app_id"] == "waddles.core.example.blackjack"


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_no_feature_flag_permission_activation_gated_only(manifest: str) -> None:
    assert "flags.read" not in {str(p["id"]) for p in _permissions(manifest)}
    source = (BUNDLE_DIR / "src" / "app.py").read_text(encoding="utf-8")
    assert "feature_enabled" not in source and "FLAG_KEY" not in source


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_consumes_every_command_on_both_platforms(manifest: str) -> None:
    stages = _load(manifest)["stages"]
    assert isinstance(stages, dict)
    consumes = stages["process"]["consumes"]
    assert {c["platform"] for c in consumes} == {"twitch", "discord"}
    for c in consumes:
        assert c["event_types"] == ["chat.message"]
        assert set(c["filters"]["command_prefix"]) == {"!blackjack", "!hit", "!stand"}
