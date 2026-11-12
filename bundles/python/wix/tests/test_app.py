"""Host-native tests for the event-only Wix bundle: no chat command, manifests, stage behaviour."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from waddle_sdk.flask_core import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, StageGuard, install_fake_kv_host

from app import dispatch as _app_dispatch
from app import transform as _app_transform

ALICE = "11111111-2222-3333-4444-555555555555"
WIX_B = "01234567-89ab-cdef-0123-456789abcdef"

BUNDLE_DIR = Path(__file__).resolve().parent.parent
REPO_BUNDLES = BUNDLE_DIR.parent.parent
APP_ID = "waddles.core.integration.wix"
VERSION = "1.1.1"

#: Stage-faithful fakes: a host call from the wrong stage fails the test (fixture teardown).
GUARD = StageGuard()
transform = GUARD.process_stage(_app_transform)
dispatch = GUARD.action_stage(_app_dispatch)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    """Fake host: kv and recorded relay/log."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.relay_calls: list[tuple[str, dict[str, Any]]] = []
        self.log_calls: list[tuple[int, str, str]] = []

    def logs_text(self) -> str:
        return " ".join(f"{m} {f}" for _l, m, f in self.log_calls)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Host]:
    GUARD.reset()
    state = _Host(install_fake_kv_host(monkeypatch))
    wit = sys.modules["wit_world"]
    wit.imports.relay = types.SimpleNamespace(
        push=GUARD.action_only(
            "relay.push", lambda p, m: state.relay_calls.append((p, json.loads(m)))
        )
    )
    wit.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: state.log_calls.append((lvl, msg, fields)),
    )
    yield state
    assert not GUARD.violations, GUARD.violations


def _manifests() -> list[dict[str, Any]]:
    return [
        yaml.safe_load((BUNDLE_DIR / name).read_text(encoding="utf-8"))
        for name in ("bundle.yaml", "hub-manifest.yaml")
    ]


@pytest.mark.parametrize("manifest", _manifests(), ids=["bundle", "hub-manifest"])
def test_manifest_declares_no_chat_command(manifest: dict[str, Any]) -> None:
    consumes = [c for s in manifest["stages"].values() for c in s.get("consumes") or []]
    assert consumes, "no consumes scanned"
    assert {c["platform"] for c in consumes} == {"wix"}
    for c in consumes:
        assert "chat.message" not in c["event_types"]
        assert not (c.get("filters") or {}).get("command_prefix")
    perms = {p["id"] for p in manifest["permissions"]}
    assert not any(p.startswith("chat.send") for p in perms)
    assert "identity.resolve" not in perms
    assert not manifest.get("egress")
    assert manifest["version"] == VERSION


def test_core_bundles_entry_version_matches() -> None:
    data = yaml.safe_load(
        (REPO_BUNDLES / "core-bundles.yaml").read_text(encoding="utf-8")
    )
    entries = [b for b in data["bundles"] if b["app_id"] == APP_ID]
    assert len(entries) == 1
    assert entries[0]["version"] == VERSION


def test_source_has_no_command_head() -> None:
    assert "!wix" not in (BUNDLE_DIR / "src" / "app.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("text", ["!wix", "!wix status", "!wix enable roles", "hello"])
def test_chat_lines_are_ignored(host: _Host, text: str) -> None:
    ev = PlatformEvent(
        "discord", "chat.message", ALICE, {"text": text, "channel_id": "1"}, "t"
    )  # type: ignore[arg-type]
    with bundle_context(tenant="t", community="c1", app_id=APP_ID):
        assert _run(transform(ev)) is None
    assert host.relay_calls == []
    assert host.kv.store == {}
    assert "wix.unexpected_platform" in host.logs_text()


def test_dispatch_refuses_loudly(host: _Host) -> None:
    ev = PlatformEvent(
        "discord", "chat.message", ALICE, {"text": "x", "channel_id": "1"}, "t"
    )  # type: ignore[arg-type]
    env = StageEnvelope(
        event=ev, tenant="t", community="c1", app_id=APP_ID, stage="action", ts="t"
    )  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="event-only"):
        _run(dispatch(env, {}, http_client=None))
    assert host.relay_calls == []
    assert "wix.dispatch_unexpected" in host.logs_text()
