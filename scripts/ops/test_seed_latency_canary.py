"""Tests for scripts/ops/seed_latency_canary.py (a fake in-memory hub-api; no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "seed_latency_canary", Path(__file__).with_name("seed_latency_canary.py")
)
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
sys.modules["seed_latency_canary"] = (
    mod  # dataclasses resolves its module via sys.modules
)
_SPEC.loader.exec_module(mod)

GUILD = "1234567890123456789"


class FakeHub:
    """In-memory community + activation store speaking the hub-api JSON shapes."""

    def __init__(
        self, active: list[str] | None = None, community: bool = False
    ) -> None:
        self.communities: list[dict[str, object]] = []
        self.active: list[str] = list(active or [])
        self.calls: list[tuple[str, str]] = []
        self.ack_required = False
        # Accrual bundle community config: app_id -> {"version": int, "config": dict}.
        self.configs: dict[str, dict[str, object]] = {
            a: {"version": 0, "config": {}} for a in mod.ACCRUAL_BUNDLES
        }
        self.inactive_cfg: set[str] = set()
        self.no_schema: set[str] = set()
        self.ignore_put = False
        if community:
            self.communities.append({"id": 11, "platformServerId": GUILD})

    def __call__(self, method: str, path: str, body):
        self.calls.append((method, path))
        if path.startswith("/api/v1/superadmin/communities") and method == "GET":
            return 200, {"communities": self.communities}
        if path == "/api/v1/superadmin/communities" and method == "POST":
            assert body["platformServerId"] == GUILD and body["isPublic"] is False
            self.communities.append({"id": 42, "platformServerId": GUILD})
            return 201, {"community": {"id": 42}}
        if "/bundles/" in path and path.endswith("/config"):
            app_id = path.split("/bundles/")[1].rsplit("/", 1)[0]
            if app_id in self.inactive_cfg:
                return 404, {"error": {"code": "bundle_not_active"}}
            if app_id in self.no_schema:
                return 404, {"error": {"code": "bundle_has_no_config"}}
            cfg = self.configs[app_id]
            if method == "PUT":
                assert body["expectedVersion"] == cfg["version"]
                if not self.ignore_put:
                    cfg["version"] = int(cfg["version"]) + 1
                    cfg["config"] = dict(body["config"])
                return 200, {}
            return 200, {"version": cfg["version"], "config": cfg["config"]}
        if path.endswith("/activation") and method == "GET":
            return 200, {"bundles": [{"appId": a} for a in self.active]}
        if path.endswith("/activation") and method == "POST":
            if self.ack_required:
                return 422, {"error": "access_grant_ack_required"}
            self.active.append(body["appId"])
            return 201, {}
        if method == "DELETE":
            self.active.remove(path.rsplit("/", 1)[1])
            return 200, {}
        raise AssertionError((method, path))


def test_fresh_seed_creates_activates_ping_and_verifies() -> None:
    hub = FakeHub()
    res = mod.seed(hub, GUILD, dry_run=False)
    assert (res.community_id, res.created, res.ping_activated) == (42, True, True)
    assert hub.active == [mod.PING_APP_ID]
    assert res.examined == 1
    # Accrual is switched off on all three reputation/economy bundles, verified by read-back.
    assert set(res.accrual_off) == set(mod.ACCRUAL_BUNDLES)
    assert set(res.accrual_changed) == set(mod.ACCRUAL_BUNDLES)
    for cfg in hub.configs.values():
        assert cfg["config"] == {"accrual_enabled": False}


def test_rerun_is_idempotent_and_deactivates_denied_bundles() -> None:
    hub = FakeHub(
        active=[
            mod.PING_APP_ID,
            "waddles.fun.loyalty",
            "waddles.fun.jackpot",
            "waddles.core.service.economy",
            "waddles.fun.roll",
        ],
        community=True,
    )
    res = mod.seed(hub, GUILD, dry_run=False)
    assert res.community_id == 11 and not res.created and not res.ping_activated
    assert set(res.deactivated) == {"waddles.fun.loyalty", "waddles.fun.jackpot"}
    assert sorted(hub.active) == sorted(
        [mod.PING_APP_ID, "waddles.core.service.economy", "waddles.fun.roll"]
    )
    again = mod.seed(hub, GUILD, dry_run=False)
    assert again.deactivated == () and not any(m == "POST" for m, _ in hub.calls[-4:])


def test_dry_run_never_writes() -> None:
    hub = FakeHub()
    with pytest.raises(RuntimeError, match="would create"):
        mod.seed(hub, GUILD, dry_run=True)
    assert all(m == "GET" for m, _ in hub.calls)


def test_ack_required_for_ping_fails_loud() -> None:
    hub = FakeHub(community=True)
    hub.ack_required = True
    with pytest.raises(RuntimeError, match="acknowledgement"):
        mod.seed(hub, GUILD, dry_run=False)


def test_zero_activations_after_seed_is_a_failure() -> None:
    class Empty(FakeHub):
        def __call__(self, method, path, body):
            if method == "POST" and path.endswith("/activation"):
                return 201, {}  # accepted but never recorded
            return super().__call__(method, path, body)

    with pytest.raises(RuntimeError, match="0 activations"):
        mod.seed(Empty(community=True), GUILD, dry_run=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://waddles.penguintech.cloud",
        "https://app.waddles.app",
        "https://beta.example.com",
    ],
)
def test_non_alpha_targets_are_refused(url: str) -> None:
    with pytest.raises(mod.Refused):
        mod.check_target(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18080",
        "http://localhost:1",
        "https://waddles-alpha.penguintech.cloud:8443",
    ],
)
def test_loopback_and_alpha_hosts_are_allowed(url: str) -> None:
    mod.check_target(url)


def test_main_requires_confirmation_token_and_snowflake(monkeypatch, capsys) -> None:
    base = ["--hub-api-url", "http://127.0.0.1:1", "--guild-id", GUILD]
    monkeypatch.delenv(mod.TOKEN_ENV, raising=False)
    assert mod.main(base) == mod.EXIT_REFUSED
    assert mod.main([*base, "--confirm-alpha"]) == mod.EXIT_REFUSED  # no token
    monkeypatch.setenv(mod.TOKEN_ENV, "s3cret-token-value")
    assert (
        mod.main(
            [
                "--hub-api-url",
                "http://127.0.0.1:1",
                "--guild-id",
                "abc",
                "--confirm-alpha",
            ]
        )
        == mod.EXIT_REFUSED
    )
    assert (
        mod.main(
            [
                "--hub-api-url",
                "https://example.com",
                "--guild-id",
                GUILD,
                "--confirm-alpha",
            ]
        )
        == mod.EXIT_REFUSED
    )
    hub = FakeHub()
    assert (
        mod.main([*base, "--confirm-alpha"], http_factory=lambda _u, _t: hub)
        == mod.EXIT_OK
    )
    out = capsys.readouterr()
    assert (
        "community_id=42" in out.out and "s3cret-token-value" not in out.out + out.err
    )


def test_service_bundles_are_never_deactivated_only_their_accrual_is_switched_off() -> (
    None
):
    hub = FakeHub(
        active=[
            mod.PING_APP_ID,
            "waddles.core.service.economy",
            "waddles.core.service.reputation",
            "waddles.core.app.reputation-accrual",
        ],
        community=True,
    )
    res = mod.seed(hub, GUILD, dry_run=False)
    # The accrual APP is a denied name and is deactivated; the SERVICE bundles stay activated.
    assert "waddles.core.app.reputation-accrual" in res.deactivated
    assert "waddles.core.service.economy" in hub.active
    assert "waddles.core.service.reputation" in hub.active
    assert not any(m == "DELETE" and "service" in p for m, p in hub.calls)
    assert len(res.accrual_off) == 3


def test_second_run_changes_nothing() -> None:
    hub = FakeHub(community=True)
    mod.seed(hub, GUILD, dry_run=False)
    before = len(hub.calls)
    res = mod.seed(hub, GUILD, dry_run=False)
    assert res.accrual_changed == () and len(res.accrual_off) == 3
    assert not [c for c in hub.calls[before:] if c[0] in {"PUT", "POST", "DELETE"}]


def test_old_bundle_versions_without_the_switch_fail_loud() -> None:
    hub = FakeHub(community=True)
    hub.no_schema.add("waddles.core.service.economy")
    with pytest.raises(RuntimeError, match="community_config_schema"):
        mod.seed(hub, GUILD, dry_run=False)


def test_unconfirmed_accrual_off_fails_verification() -> None:
    hub = FakeHub(community=True)
    hub.ignore_put = True  # the PUT is accepted but never persisted
    with pytest.raises(RuntimeError, match="not false after seeding"):
        mod.seed(hub, GUILD, dry_run=False)


def test_zero_accrual_bundles_examined_is_a_failure() -> None:
    hub = FakeHub(community=True)
    hub.inactive_cfg.update(mod.ACCRUAL_BUNDLES)
    with pytest.raises(RuntimeError, match="examined 0 accrual bundles"):
        mod.seed(hub, GUILD, dry_run=False)


def test_inactive_accrual_bundle_is_skipped_not_failed() -> None:
    hub = FakeHub(community=True)
    hub.inactive_cfg.add("waddles.core.app.reputation-accrual")
    res = mod.seed(hub, GUILD, dry_run=False)
    assert len(res.accrual_off) == 2


def test_dry_run_flags_pending_accrual_change_without_writing() -> None:
    hub = FakeHub(active=[mod.PING_APP_ID], community=True)
    with pytest.raises(RuntimeError, match="would set accrual_enabled=false"):
        mod.seed(hub, GUILD, dry_run=True)
    assert all(m == "GET" for m, _ in hub.calls)
