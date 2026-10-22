"""Gate: every bundle that relays an `ai.generate` completion must guard the reply's lead.

A model completion is attacker-influenced and is relayed raw. One that STARTS with `/` or `.`
(Twitch/IRC run it as the bot's own command) or `!` (re-triggers other bots) is executed by the
platform, not read -- an LLM01 sink the prompt-side defenses cannot close. The guard is ONE shared
helper, `waddle_sdk.safe_text.guard_leading_command`; it was copy-pasted into six bundles before
being lifted, so this gate proves three things over EVERY `bundles/python/*/src/app.py` that calls
`ai.generate(...)`:

1. **Static**: the bundle calls the shared guard, or is on the reasoned prefix-safe allowlist
   (its reply is always built behind a fixed visible prefix). An allowlist entry that no longer
   applies fails (stale) so the list can only shrink, and no bundle may re-implement the guard.
2. **Behavioural**: with a hostile completion (every command lead, hidden behind invisible /
   control characters and whitespace), the reply the bundle actually emits never starts with a
   command lead -- checked with an oracle independent of the SDK helper under test.
3. **Denominators**: a scan that finds too few bundles, or a bundle with no behavioural sample,
   FAILS -- a new `ai.generate` bundle cannot slip through unguarded and unnoticed.

Run: python3 -m pytest scripts/ci/tests/test_ai_output_lead_gate.py
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
import types
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLES_DIR = REPO_ROOT / "bundles" / "python"
SDK_SRC = REPO_ROOT / "sdk" / "waddle-sdk" / "src"

GUARD = "guard_leading_command"

#: Names the six copy-pasted guards used; none may be (re)defined inside a bundle.
FORBIDDEN_LOCAL_GUARD_NAMES = frozenset(
    {"_UNSAFE_LEAD", "_SAFE_LEAD", "_leads_with_command"}
)

#: Bundles whose reply is ALWAYS built behind a fixed visible prefix, so the model's lead can never
#: lead the reply (a guard would be dead code). Exact-match enforced: an entry that calls the guard,
#: or no longer calls `ai.generate`, fails -- so this can only shrink.
PREFIX_SAFE = {
    "rephrase": "reply is f'<fixed pencil emoji> {completion}' -- always led by that emoji",
    "translate": "reply is f'[{lang}] {completion}' -- always led by a fixed bracketed language",
}


def _is_ai_generate(node: ast.AST) -> bool:
    """True for a `ai.generate(...)` call (the SDK's model entry point)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "generate"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ai"
    )


def _calls_named(tree: ast.AST, name: str) -> bool:
    """True if the module calls a function/attribute called `name`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == name) or (
                isinstance(func, ast.Attribute) and func.attr == name
            ):
                return True
    return False


def _defined_names(tree: ast.AST) -> set[str]:
    """Every name the module assigns or defines at any level."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def _ai_apps() -> dict[str, Path]:
    """`{bundle name: app.py}` for every bundle whose code calls `ai.generate(...)`."""
    found: dict[str, Path] = {}
    for path in sorted(BUNDLES_DIR.glob("*/src/app.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(_is_ai_generate(node) for node in ast.walk(tree)):
            found[path.parent.parent.name] = path
    return found


AI_APPS = _ai_apps()

_ZWSP, _BOM, _RLO, _NUL = chr(0x200B), chr(0xFEFF), chr(0x202E), chr(0)
_TAG = chr(0xE0041)
_E_ACUTE = chr(0xE9)  # sentiment's emoji slot must be non-ASCII to be accepted

#: Hostile completion LEADS: each command lead bare, indented, and hidden behind characters the
#: host's send path strips before the platform parses the line.
LEADS = [
    "/",
    ".",
    "!",
    "  /",
    "\n.",
    _ZWSP + "/",
    _BOM + "!",
    _RLO + ".",
    _TAG + "/",
    _NUL + "!",
    _ZWSP + _NUL + " " + _BOM + "/",
]


#: One valid invocation per `ai.generate` bundle, and how to smuggle a hostile lead through its
#: completion parser. Keyed by bundle name; the gate FAILS for an `ai.generate` bundle with no entry.
def _plain(lead: str) -> str:
    return f"{lead}ban everyone now"


def _sentiment_json(lead: str) -> str:
    """Sentiment only accepts a JSON object; the model-chosen emoji LEADS its reply."""
    return json.dumps(
        {"sentiment": "positive", "confidence": 0.9, "emoji": f"{lead}ban {_E_ACUTE}"}
    )


SAMPLES: dict[str, tuple[str, Callable[[str], str]]] = {
    "ask": ("!ask why is the sky blue", _plain),
    "define": ("!define penguin", _plain),
    "eli5": ("!eli5 photosynthesis", _plain),
    "rephrase": ("!rephrase hello there my friend", _plain),
    "sentiment": ("!sentiment I love this stream", _sentiment_json),
    "summarize": (
        "!summarize the quick brown fox jumps over the lazy dog today",
        _plain,
    ),
    "tldr": ("!tldr the quick brown fox jumps over the lazy dog today", _plain),
    "translate": ("!translate es hello there my friend", _plain),
}


def _independent_lead_oracle(text: str) -> str:
    """The first character the platform would see: drop Cc/Cf, skip whitespace (no SDK code)."""
    visible = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    return visible.lstrip()[:1]


def _install_world(monkeypatch: pytest.MonkeyPatch, completion: str) -> Any:
    """Fake only the WIT imports the bundle needs (ai + log); the real SDK and bundle run."""
    monkeypatch.syspath_prepend(str(SDK_SRC))
    from waddle_sdk.testing import FakeAiHost

    ai_host = FakeAiHost(reply=completion)
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        ai=types.SimpleNamespace(generate=ai_host.generate, Options=ai_host.Options),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3}, write=lambda *a: None
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)
    return ai_host


def _load(name: str, path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Import one bundle's `app.py` under a unique module name with its `src/` + the SDK on path."""
    module_name = f"ai_lead_gate_{name}"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_denominator_the_scan_found_the_ai_bundles() -> None:
    """A mis-pointed glob/AST matcher must fail loudly, not pass vacuously."""
    assert len(AI_APPS) >= 8, (
        f"expected >=8 ai.generate bundles, found {sorted(AI_APPS)}"
    )
    assert {
        "ask",
        "define",
        "eli5",
        "rephrase",
        "sentiment",
        "summarize",
        "tldr",
        "translate",
    } <= set(AI_APPS)


def test_every_ai_bundle_has_a_behavioural_sample() -> None:
    """A new ai.generate bundle must add a SAMPLES entry (an unsampled bundle is unproven)."""
    missing = sorted(set(AI_APPS) - set(SAMPLES))
    assert not missing, f"add a hostile-completion sample to SAMPLES for: {missing}"
    stale = sorted(set(SAMPLES) - set(AI_APPS))
    assert not stale, (
        f"SAMPLES entries for bundles that no longer call ai.generate: {stale}"
    )


@pytest.mark.parametrize("name", sorted(AI_APPS))
def test_bundle_uses_the_shared_guard_or_is_reasoned_prefix_safe(name: str) -> None:
    tree = ast.parse(AI_APPS[name].read_text(encoding="utf-8"))
    guarded = _calls_named(tree, GUARD)
    if name in PREFIX_SAFE:
        assert not guarded, (
            f"{name} calls {GUARD} now: drop it from PREFIX_SAFE (stale allowlist)"
        )
    else:
        assert guarded, (
            f"{name} calls ai.generate but never calls waddle_sdk.safe_text.{GUARD}: a completion "
            "that starts with `/`, `.` or `!` would run as a chat command"
        )
        imports = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "waddle_sdk.safe_text"
            for alias in node.names
        ]
        assert GUARD in imports, f"{name} must import {GUARD} from waddle_sdk.safe_text"


@pytest.mark.parametrize("name", sorted(AI_APPS))
def test_no_bundle_reimplements_the_guard(name: str) -> None:
    tree = ast.parse(AI_APPS[name].read_text(encoding="utf-8"))
    local = sorted(_defined_names(tree) & FORBIDDEN_LOCAL_GUARD_NAMES)
    assert not local, (
        f"{name} re-implements the lead guard ({local}); use waddle_sdk.safe_text"
    )


def test_the_prefix_safe_allowlist_names_real_bundles() -> None:
    assert set(PREFIX_SAFE) <= set(AI_APPS), (
        "PREFIX_SAFE names a bundle that does not call ai.generate"
    )


@pytest.mark.parametrize("lead", LEADS, ids=[repr(lead) for lead in LEADS])
@pytest.mark.parametrize("name", sorted(AI_APPS))
def test_a_hostile_completion_never_makes_the_reply_start_with_a_command(
    name: str, lead: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_text, make_completion = SAMPLES[name]
    ai_host = _install_world(monkeypatch, make_completion(lead))
    module = _load(name, AI_APPS[name], monkeypatch)
    from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": command_text, "channel_id": "12345"},
        occurred_at="2026-10-22T00:00:00.000Z",
    )
    out = asyncio.run(module.transform(event))

    assert out is not None, f"{name}: transform dropped a valid invocation"
    assert len(ai_host.calls) == 1, (
        f"{name}: the model was never reached (vacuous test)"
    )
    # Bundles that tag their output carry `command`; it must be the success command, not an error.
    assert out.payload.get("command", name) == name, (
        f"{name}: expected its success path, got command={out.payload.get('command')!r} "
        f"text={out.payload['text']!r} -- the sample no longer reaches the completion"
    )
    text = out.payload["text"]
    if (
        lead.isprintable()
    ):  # invisible leads may be rejected by a bundle's own completion parser
        assert "ban" in text, (
            f"{name}: the hostile completion never reached the reply (vacuous)"
        )
    first = _independent_lead_oracle(text)
    assert first not in ("/", ".", "!"), (
        f"{name}: reply {text!r} would be parsed as a chat command (first visible char {first!r})"
    )
