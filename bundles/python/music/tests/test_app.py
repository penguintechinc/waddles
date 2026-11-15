"""Host-native tests for the music chat front end over the music-requests service.

A fake WIT seam stands in for `tool`, `kv`, `flags`, `relay`, `overlay`, `log` and `clock`; the
service is faked with a tiny in-memory model so replies are checked end to end.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from waddle_sdk.flask_core.bundle_runtime import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import StageGuard
from waddle_sdk.tool import ToolError

import app

GUARD = StageGuard()
transform = GUARD.process_stage(app.transform)
dispatch = GUARD.action_stage(app.dispatch)
COMMUNITY = "comm-1"
REQUESTER = "99999999-9999-4999-8999-999999999999"


class Host:
    """Records every wire call and models the service's observable behavior."""

    def __init__(self) -> None:
        """Start with an empty service, empty kv and flags OFF."""
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.errors: dict[str, ToolError] = {}
        self.results: dict[str, Any] = {}
        self.kv: dict[str, bytes] = {}
        self.relayed: list[dict[str, Any]] = []
        self.surfaces: list[tuple[str, dict[str, Any]]] = []
        self.logs: list[str] = []
        self.flag = False
        self.queue: list[dict[str, Any]] = []
        self.now: dict[str, Any] | None = None

    def item(self, i: int, title: str, artist: str = "Band") -> dict[str, Any]:
        """A service item (never carries a requester)."""
        return {
            "id": i,
            "title": title,
            "artist": artist,
            "source_url": "",
            "duration_ms": 61000,
            "kind": "viewer",
            "queued_at": "t",
        }

    def invoke(self, target: str, name: str, args: bytes, key: str | None) -> bytes:
        """Fake `tool.invoke`: scripted errors/results first, then a default model."""
        assert target == app.SERVICE
        decoded = json.loads(args)
        self.calls.append((name, decoded, key))
        if name in self.errors:
            raise self.errors[name]
        if name in self.results:
            return json.dumps(self.results[name]).encode()
        if name == "requests.list":
            out: Any = {
                "now_playing": self.now,
                "queue": self.queue,
                "total_queued": len(self.queue),
                "ai_dj_on": False,
            }
        elif name == "requests.add":
            it = self.item(len(self.queue) + 1, decoded["title"], decoded["artist"])
            self.queue.append(it)
            out = {"item": it, "position": len(self.queue)}
        else:
            out = {"removed": True}
        return json.dumps(out).encode()

    def names(self) -> list[str]:
        """Tool names called, in order."""
        return [c[0] for c in self.calls]


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    """Install the fake `wit_world` and reset the stage guard."""
    GUARD.reset()
    h = Host()

    def kv_set(key: str, value: bytes, ttl: int) -> None:
        assert ":" not in key
        h.kv[key] = bytes(value)

    imports = types.SimpleNamespace(
        tool=types.SimpleNamespace(invoke=h.invoke),
        kv=types.SimpleNamespace(
            get=lambda k: h.kv.get(k), set=kv_set, delete=lambda k: h.kv.pop(k, None)
        ),
        flags=types.SimpleNamespace(enabled=lambda k, d: h.flag if k == app.FLAG_QUEUE_PAGE else d),
        relay=types.SimpleNamespace(
            push=GUARD.action_only("relay.push", lambda p, m: h.relayed.append(json.loads(m)))
        ),
        overlay=types.SimpleNamespace(
            publish_surface=lambda s, p: h.surfaces.append((s, json.loads(p)))
        ),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: h.logs.append(f"{msg} {fields}"),
        ),
        clock=types.SimpleNamespace(now_millis=lambda: 1_700_000_000_000),
    )
    mod = types.ModuleType("wit_world")
    mod.imports = imports  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", mod)
    yield h
    GUARD.assert_clean()


def ev(text: str, mid: str | None = "m1") -> PlatformEvent:
    """A chat event; badges are deliberately forged mod to prove they are ignored."""
    payload: dict[str, Any] = {"text": text, "channel_id": "123", "is_mod": True}
    if mid:
        payload["message_id"] = mid
    return PlatformEvent(
        "twitch", "chat.message", "viewer-raw", payload, "2026-11-01T00:00:00.000Z"
    )


def run(text: str, mid: str | None = "m1") -> PlatformEvent | None:
    """Run the process stage inside a bundle context."""

    async def go() -> PlatformEvent | None:
        with bundle_context(tenant="t", community=COMMUNITY, app_id="music"):
            return await transform(ev(text, mid))

    return asyncio.run(go())


def reply(text: str, mid: str | None = "m1") -> str:
    """Reply text of a recognized command."""
    out = run(text, mid)
    assert out is not None
    return str(out.payload["reply"])


def add_calls(host: Host) -> list[tuple[str, dict[str, Any], str | None]]:
    """Only the requests.add calls."""
    return [c for c in host.calls if c[0] == "requests.add"]


def test_add_parses_title_artist_url_and_keys_idempotency(host: Host) -> None:
    """`!sr` splits title/artist, extracts the https link, and keys on the message id."""
    assert "added" in reply("!sr Song One - The Band https://example.com/x")
    _, args, key = add_calls(host)[0]
    assert args == {
        "title": "Song One",
        "artist": "The Band",
        "source_url": "https://example.com/x",
    }
    assert key and len(key) == 64
    reply("!songrequest Song One - The Band", "m2")
    assert add_calls(host)[1][2] != key


def test_add_without_message_id_is_refused_without_calling_add(host: Host) -> None:
    """No stable id -> friendly refusal, never an unguarded write."""
    assert "try again" in reply("!sr x - y", mid=None)
    assert "requests.add" not in host.names()


@pytest.mark.parametrize("key", ["queue_full", "per_user_limit", "duplicate", "invalid_url"])
def test_add_service_errors_are_friendly(host: Host, key: str) -> None:
    """Typed service errors map to chat lines, not exceptions."""
    host.errors["requests.add"] = ToolError("invalid_args", key)
    assert reply("!sr a - b") == app._SERVICE_ERRORS[key]


def test_add_not_dmca_safe_message(host: Host) -> None:
    """`not_dmca_safe` yields the clear DMCA-safe notice; no provider/tag is sent."""
    host.errors["requests.add"] = ToolError("invalid_args", "not_dmca_safe")
    assert reply("!sr a - b") == (
        "DMCA-safe mode is on: only royalty-free (RFM) and NCS tracks can be requested"
    )
    assert set(add_calls(host)[0][1]) <= {"title", "artist", "source_url"}


def test_list_runs_advance_due_first_and_shows_now_and_next(host: Host) -> None:
    """`!music` advances due items, then lists now playing + next items."""
    host.now = host.item(1, "Playing")
    host.queue = [host.item(2, "Next A"), host.item(3, "Next B")]
    text = reply("!music")
    assert host.names()[:2] == ["requests.advance_due", "requests.list"]
    assert "Now playing: Playing - Band" in text and "#2 Next A - Band" in text
    assert reply("!queue") == text
    assert "Now playing: Playing" in reply("!music now")


def test_history_last_five(host: Host) -> None:
    """`!music history` asks for 5 and formats titles."""
    host.results["history.list"] = {"items": [{"title": "Old", "artist": "A"}]}
    assert reply("!music history") == "Recently played: Old - A"
    assert host.calls[-1] == ("history.list", {"limit": 5}, None)


def test_remove_viewer_falls_back_to_cancel_own(host: Host) -> None:
    """Forbidden on remove -> cancel_own; privilege comes from scopes, not the forged badge."""
    host.errors["requests.remove"] = ToolError("forbidden")
    assert reply("!music remove 3") == "removed request #3"
    assert host.names()[:2] == ["requests.remove", "requests.cancel_own"]


def test_remove_viewer_not_owner_and_moderator_paths(host: Host) -> None:
    """Non-owner viewer is told so; a moderator's remove never touches cancel_own."""
    host.errors["requests.remove"] = ToolError("forbidden")
    host.results["requests.cancel_own"] = {"removed": False}
    assert reply("!music remove 3") == "you can only remove your own requests"
    host.errors.clear()
    host.calls.clear()
    assert reply("!music remove 4") == "removed request #4"
    assert "requests.cancel_own" not in host.names()
    assert "valid" in reply("!music remove abc")


@pytest.mark.parametrize(
    "cmd",
    ["!music next", "!music skip", "!music set max-per-user 4", "!music set dmca-safe on"],
)
def test_manage_ops_forbidden_mapped(host: Host, cmd: str) -> None:
    """A typed forbidden is a friendly reply even though the forged badge says mod."""
    for n in ("requests.advance", "config.set", "settings.set"):
        host.errors[n] = ToolError("forbidden")
    assert reply(cmd) == app._DENIED_MSG


def test_advance_config_and_dmca_success(host: Host) -> None:
    """Happy paths for advance, config.set and settings.set/get."""
    host.results["requests.advance"] = {"finished": None, "now_playing": host.item(2, "Two")}
    assert reply("!music next") == "now playing: Two - Band"
    host.results["config.set"] = {"max_per_user": 4}
    assert reply("!music set max-per-user 4") == "max-per-user set to 4"
    assert "between" in reply("!music set max-per-user 99")
    host.results["settings.set"] = {"dmca_safe": True}
    assert reply("!music set dmca-safe on") == "DMCA-safe mode is now on"
    assert ("settings.set", {"dmca_safe": True}) == host.calls[-3][:2]
    assert reply("!music set dmca-safe maybe") == app._USAGE
    host.results["config.get"] = {"dmca_safe": True}
    assert reply("!music dmca") == "DMCA-safe mode is on"


def test_top_output_formatting(host: Host) -> None:
    """`!music top` defaults to songs, accepts artists, and never shows requesters."""
    host.results["music.stats.top"] = {
        "items": [
            {"title": "S1", "artist": "A1", "plays": 9},
            {"title": "S2", "artist": "A2", "plays": 4},
        ]
    }
    assert reply("!music top") == "Top songs: 1. S1 - A1 (9); 2. S2 - A2 (4)"
    assert host.calls[-1][1] == {"kind": "song", "limit": 5}
    host.results["music.stats.top"] = {"items": [{"artist": "A1", "plays": 7}]}
    assert reply("!music top artists") == "Top artists: 1. A1 (7)"
    assert reply("!music top nonsense") == app._USAGE
    host.results["music.stats.top"] = {"items": []}
    assert reply("!music top") == "no plays recorded yet."


def test_songqueue_flag_off_link_and_not_configured(host: Host) -> None:
    """Flag OFF -> silent; ON -> link; link_not_configured -> plain notice."""
    assert run("!songqueue") is None and run("!sq") is None
    assert host.calls == []
    host.flag = True
    host.results["page.link"] = {"url": "https://w.example/community/7/music"}
    assert reply("!sq") == "Song queue: https://w.example/community/7/music"
    del host.results["page.link"]
    host.errors["page.link"] = ToolError("invalid_args", "link_not_configured")
    assert reply("!songqueue") == "song queue page is not configured"


def test_migration_one_shot(host: Host) -> None:
    """Legacy kv queue is imported once (no requester ids), cleared, and never re-run."""
    host.kv[f"c.{COMMUNITY}.music.queue"] = json.dumps(
        [{"id": 1, "requester_pseudonym": "abc", "text": "T - A https://e.com/z", "ts": 1}]
    ).encode()
    host.kv[f"c.{COMMUNITY}.music.seq"] = b"1"
    reply("!music")
    name, args, _ = host.calls[0]
    assert name == "requests.import"
    assert args == {
        "items": [{"title": "T", "artist": "A", "source_url": "https://e.com/z", "duration_ms": 0}]
    }
    assert "abc" not in json.dumps(args)
    assert f"c.{COMMUNITY}.music.queue" not in host.kv
    assert f"c.{COMMUNITY}.music.seq" not in host.kv
    assert host.kv[f"c.{COMMUNITY}.music.migrated"] == b"1"
    host.calls.clear()
    reply("!music")
    assert "requests.import" not in host.names()


def test_migration_retries_on_transient_failure_and_clears_on_refusal(host: Host) -> None:
    """Transient failure keeps legacy data; a service refusal clears it (cannot succeed)."""
    key = f"c.{COMMUNITY}.music.queue"
    host.kv[key] = json.dumps([{"text": "T - A"}]).encode()
    host.errors["requests.import"] = ToolError("target_unavailable")
    reply("!music")
    assert key in host.kv and f"c.{COMMUNITY}.music.migrated" not in host.kv
    host.errors["requests.import"] = ToolError("invalid_args", "already imported")
    reply("!music")
    assert key not in host.kv and f"c.{COMMUNITY}.music.migrated" in host.kv


def test_overlay_from_service_state_without_requester(host: Host) -> None:
    """Overlay frames come from requests.list; no requester or url leaks anywhere."""
    host.now = host.item(1, "Playing")
    host.queue = [dict(host.item(2, "Next"), requester=REQUESTER, source_url="https://e.com/q")]
    out = run("!music")
    assert out is not None

    async def go() -> None:
        env = StageEnvelope(
            tenant="t", community=COMMUNITY, app_id="music", stage="action", event=out, ts="x"
        )
        await dispatch(env, {}, http_client=None)

    asyncio.run(go())
    frames = dict(host.surfaces)
    assert frames[app._SURFACE_NOW_PLAYING]["now_playing"]["title"] == "Playing"
    assert frames[app._SURFACE_QUEUE]["queue"][0]["title"] == "Next"
    blob = json.dumps(host.surfaces) + json.dumps(host.relayed)
    assert REQUESTER not in blob and "e.com" not in blob and "requester" not in blob
    assert host.relayed[0]["channel"] == "123"


def test_logs_pii_free_and_unknown_error_is_loud(host: Host) -> None:
    """Failures log only code/command; chat gets an apology, never raw input."""
    host.errors["requests.add"] = ToolError("timeout")
    assert reply("!sr SecretTitle - SecretArtist") == app._UNAVAILABLE_MSG
    joined = " ".join(host.logs)
    assert "music.tool_failed" in joined
    assert "Secret" not in joined and "viewer-raw" not in joined


def test_works_with_dj_absent_and_unrelated_ignored(host: Host) -> None:
    """Never calls a dj.* tool or names the DJ bundle; unrelated chat is ignored."""
    reply("!music")
    reply("!sr a - b")
    assert not any(n.startswith("dj.") for n in host.names())
    with open(app.__file__, encoding="utf-8") as fh:
        assert "example.dj" not in fh.read()
    assert run("hello") is None
    assert reply("!sr") == app._USAGE
    assert reply("!music bogus") == app._USAGE
