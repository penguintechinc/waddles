"""Python half of the data-plane golden-vector parity harness (gh #840 PR4).

Loads the shared corpus (`tests/data_plane_parity/corpus/**.json`), runs each
raw platform frame through the REAL legacy Python ingest path, and diffs the
resulting `PlatformEvent` against the vector's committed `golden`:

* Twitch IRC  -- a real local TCP server speaks the IRC wire protocol to the
  real `TwitchIrcReceiver` (-> `waddle_transports.IrcTransport` regex parse ->
  tag/badge decoding -> self-nick filter) whose raw dict feeds the real
  `builtin_handlers.twitch_ingest.normalize`.
* Discord     -- the real `DiscordGatewayReceiver._is_self` / `_build_raw_event`
  over a duck-typed `discord.Message` (py-cord's own `Message` needs live HTTP
  state to construct; that third-party layer is out of scope, exactly as in
  `core/svc_ingest/tests/test_discord_gateway.py`), then the real
  `discord_ingest.normalize`.
* EventSub    -- the real `TwitchEventSubHandler.handle_webhook` (HMAC
  verification, known-type filter, `build_raw_event`); only the Valkey fan-out
  boundary is captured, then the real `twitch_eventsub_ingest.normalize`.
* Slack/Kick/YouTube/Spectrum -- Python-only today: the receiver-raw dict is fed
  to the real `normalize()`; the golden IS the Python output until a Rust/bundle
  path lands.

`golden` for dual-path vectors is the Rust oracle (`core/svc_ingest/src/
normalize.rs`) -- asserted by `core/svc_ingest/tests/parity_golden.rs`.
Declared divergences (`python_divergences`) must match the observed diff
EXACTLY: a new, unexplained difference fails, and a declared one that no longer
occurs (stale) fails too.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
INGEST_DIR = REPO_ROOT / "core" / "svc_ingest"

#: svc-ingest's own top-level packages (`receivers`, `builtin_handlers`,
#: `eventsub`, `fanout`) resolve relative to the service directory.
if str(INGEST_DIR) not in sys.path:
    sys.path.insert(0, str(INGEST_DIR))

ABSENT = "<absent>"
OUTCOME_PATH = "<outcome>"
SOURCE_PATH = "source"

#: Python `PlatformEvent` has no `source` field at all (it is stamped by the
#: Rust spine). Declared once here instead of on all 38 vectors.
GLOBAL_DIVERGENCE_SOURCE = (
    "Python flask_core.PlatformEvent has no `source` field; the Rust PlatformEvent carries "
    "Source{platform, account_id, channel_id}"
)

#: Dual-path platform slugs where both a Python and a Rust path exist today.
DUAL_PATHS = frozenset({"twitch_irc", "discord_gateway", "twitch_eventsub"})

#: The corpus must cover every platform slot, populated or stubbed.
REQUIRED_PLATFORMS = frozenset({"twitch", "discord", "slack", "kick", "youtube", "spectrum"})


@dataclass(slots=True)
class PyResult:
    """What the legacy Python path did with one frame."""

    outcome: str  # "event" | "dropped" | "raises"
    event: dict[str, Any] | None = None
    occurred_at: str | None = None
    error: str | None = None


@dataclass(slots=True, frozen=True)
class Diff:
    """One observed difference between the golden and the Python event."""

    path: str
    golden: Any
    python: Any


@dataclass(slots=True)
class Vector:
    """One corpus vector (parsed JSON + its on-disk path)."""

    path: Path
    data: dict[str, Any]
    id: str = field(init=False)

    def __post_init__(self) -> None:
        self.id = str(self.data["id"])


def load_corpus() -> list[Vector]:
    """Load every vector, sorted, failing loudly on an empty corpus."""
    vectors = [
        Vector(path=p, data=json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(CORPUS_DIR.rglob("*.json"))
    ]
    if not vectors:
        raise RuntimeError(f"no vectors found under {CORPUS_DIR} -- zero examined is a FAILURE")
    return vectors


def write_vector(vector: Vector) -> None:
    """Write a vector in the canonical form shared with the Rust bless path."""
    text = json.dumps(vector.data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    vector.path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Python path runners -- one per corpus `path`
# --------------------------------------------------------------------------


async def _run_twitch_irc(vector: Vector) -> dict[str, Any] | None:
    """Drive the real `TwitchIrcReceiver` over a real local TCP IRC server."""
    from receivers.twitch_irc import TwitchIrcReceiver

    frame = str(vector.data["frame"])
    nick = str(vector.data["context"]["self_nick"])
    joined = asyncio.Event()

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Registration: wait for the client's NICK/USER burst, welcome it (001).
            await reader.readuntil(b"USER ")
            await reader.readline()
            writer.write(f":tmi.twitch.tv 001 {nick} :Welcome, GLHF!\r\n".encode())
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line or line.startswith(b"JOIN "):
                    break
            joined.set()
            writer.write(f"{frame}\r\n".encode())
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = {
        "host": "127.0.0.1",
        "port": port,
        "use_tls": False,
        "nick": nick,
        "channel": "somechannel",
        "timeout_seconds": 5.0,
    }
    try:
        async with asyncio.timeout(10):
            raws = [dict(raw) async for raw in TwitchIrcReceiver().receive(config)]
    finally:
        server.close()
        await server.wait_closed()
    assert joined.is_set(), f"{vector.id}: client never sent JOIN to the test IRC server"
    if not raws:
        return None
    assert len(raws) == 1, f"{vector.id}: expected 1 raw event, got {len(raws)}"
    return raws[0]


def _discord_message(frame: dict[str, Any]) -> SimpleNamespace:
    """Duck-type a `discord.Message` from a MESSAGE_CREATE `d` payload."""
    author = frame["author"]
    guild = SimpleNamespace(id=int(frame["guild_id"])) if frame.get("guild_id") else None
    return SimpleNamespace(
        id=int(frame["id"]),
        content=frame.get("content", ""),
        author=SimpleNamespace(
            id=int(author["id"]), name=author.get("username", ""), bot=bool(author.get("bot"))
        ),
        channel=SimpleNamespace(id=int(frame["channel_id"])),
        guild=guild,
    )


def _run_discord(vector: Vector) -> dict[str, Any] | None:
    """Real `_is_self` + `_build_raw_event` over a duck-typed message."""
    from receivers.discord_gateway import DiscordGatewayReceiver

    message = _discord_message(vector.data["frame"])
    self_id = vector.data["context"].get("self_user_id")
    bot = SimpleNamespace(user=SimpleNamespace(id=int(self_id)) if self_id else None)
    if DiscordGatewayReceiver._is_self(message, bot):  # type: ignore[arg-type]  # noqa: SLF001
        return None
    return DiscordGatewayReceiver._build_raw_event(message)  # type: ignore[arg-type]  # noqa: SLF001


async def _run_eventsub(vector: Vector) -> dict[str, Any] | None:
    """Real `TwitchEventSubHandler.handle_webhook`; capture only the Valkey fan-out."""
    import eventsub

    secret = "parity-harness-secret"  # noqa: S105 - test literal, not a credential
    body_json = vector.data["frame"]
    body = json.dumps(body_json).encode()
    message_id, timestamp = "msg-parity-1", "2026-09-28T00:00:00Z"
    signature = (
        "sha256="
        + hmac.new(
            secret.encode(), message_id.encode() + timestamp.encode() + body, hashlib.sha256
        ).hexdigest()
    )
    headers = {
        eventsub.EVENTSUB_MESSAGE_ID: message_id,
        eventsub.EVENTSUB_TIMESTAMP: timestamp,
        eventsub.EVENTSUB_SIGNATURE: signature,
        eventsub.EVENTSUB_MESSAGE_TYPE: str(vector.data["context"]["message_type"]),
    }
    captured: list[dict[str, Any]] = []

    async def _capture(raw_event: dict[str, Any], **_: Any) -> int:
        captured.append(raw_event)
        return 1

    original = eventsub.fan_out_event
    eventsub.fan_out_event = _capture  # type: ignore[assignment]
    try:
        handler = eventsub.TwitchEventSubHandler(
            secret=secret, redis_client=None, registry=None, tenant_slug="parity"
        )  # type: ignore[arg-type]
        _response, status = await handler.handle_webhook(
            headers=headers, body=body, body_json=body_json
        )
    finally:
        eventsub.fan_out_event = original  # type: ignore[assignment]
    assert status == 200, f"{vector.id}: handle_webhook returned {status}"
    return captured[0] if captured else None


_NORMALIZERS: dict[str, tuple[str, str]] = {
    "twitch_irc": ("builtin_handlers.twitch_ingest", "normalize"),
    "discord_gateway": ("builtin_handlers.discord_ingest", "normalize"),
    "twitch_eventsub": ("builtin_handlers.twitch_eventsub_ingest", "normalize"),
    "slack_socket": ("builtin_handlers.slack_ingest", "normalize"),
    "kick_pusher": ("builtin_handlers.kick_ingest", "normalize"),
    "youtube_live_poll": ("builtin_handlers.youtube_live_ingest", "normalize"),
    "spectrum_poll": ("builtin_handlers.spectrum_ingest", "normalize"),
    "spectrum_org": ("builtin_handlers.spectrum_ingest", "normalize"),
}


async def run_python_path(vector: Vector) -> PyResult:
    """Run one frame through the legacy Python receiver + normalizer."""
    import importlib

    path = str(vector.data["path"])
    if path == "twitch_irc":
        raw = await _run_twitch_irc(vector)
    elif path == "discord_gateway":
        raw = _run_discord(vector)
    elif path == "twitch_eventsub":
        raw = await _run_eventsub(vector)
    elif path in _NORMALIZERS:
        raw = dict(vector.data["frame"])  # receiver-raw dict, fed straight to normalize()
    else:
        raise AssertionError(f"{vector.id}: no Python path registered for {path!r}")

    if raw is None:
        return PyResult(outcome="dropped")

    module_name, fn_name = _NORMALIZERS[path]
    normalize: Callable[[dict[str, Any]], Any] = getattr(
        importlib.import_module(module_name), fn_name
    )
    try:
        event = await normalize(raw)
    except ValueError as exc:
        return PyResult(outcome="raises", error=f"ValueError: {exc}")
    as_dict = event.to_dict()
    occurred_at = as_dict.pop("occurred_at")
    return PyResult(outcome="event", event=as_dict, occurred_at=occurred_at)


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts to dotted paths; lists/scalars are leaves."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, child in value.items():
            out.update(flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: value}


def diff_events(golden: dict[str, Any], python: dict[str, Any]) -> list[Diff]:
    """Path-by-path diff of golden vs Python (Python's missing `source` is global)."""
    golden_flat = flatten({k: v for k, v in golden.items() if k != SOURCE_PATH})
    python_flat = flatten({k: v for k, v in python.items() if k != SOURCE_PATH})
    diffs: list[Diff] = []
    for path in sorted(set(golden_flat) | set(python_flat)):
        g = golden_flat.get(path, ABSENT)
        p = python_flat.get(path, ABSENT)
        if g != p:
            diffs.append(Diff(path=path, golden=g, python=p))
    return diffs


def observed_divergences(vector: Vector, result: PyResult) -> list[Diff]:
    """All differences between the golden outcome/event and the Python result."""
    expected_outcome = vector.data["outcome"]
    if result.outcome == "raises":
        if expected_outcome == "event":
            return [
                Diff(
                    OUTCOME_PATH,
                    "event",
                    f"raises {result.error.split(':')[0] if result.error else ''}",
                )
            ]
        return [Diff(OUTCOME_PATH, expected_outcome, "raises")]
    if result.outcome != expected_outcome:
        return [Diff(OUTCOME_PATH, expected_outcome, result.outcome)]
    if result.outcome == "dropped":
        return []
    assert result.event is not None and vector.data["golden"] is not None
    return diff_events(vector.data["golden"], result.event)


def declared_divergences(vector: Vector) -> dict[str, Any]:
    """`{path: python_value}` the vector declares as known/expected."""
    return {d["path"]: d["python"] for d in vector.data.get("python_divergences", [])}
