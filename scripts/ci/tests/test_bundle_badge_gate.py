"""Behavioral mod-gate regression across EVERY Python bundle that reads role badges.

`bool(is_mod)` is truthy for the string `"false"`, so a non-moderator whose normalizer emitted a
string badge passed any mod gate built on it -- and `transform()` laundered the string into a
real `True` for `dispatch`. The static gate (`check-bundle-source-hygiene.py --check
badge-truthiness`) forbids the pattern; this suite proves the BEHAVIOUR on the real modules:

1. every bundle's gate helper denies hostile non-boolean badge payloads and still admits a real
   moderator/broadcaster (a positive control, so an always-False helper cannot pass);
2. every bundle whose `transform()` forwards badges to `dispatch` forwards a string badge as a
   real `False`, never `True`.

3. app bundles are activation-gated, NOT feature-flag-gated (Global:INSTALLED -> Tenant:ENABLED ->
   Community:ACTIVATED, DB-tracked): no `feature_enabled` / `FLAG_KEY` in app bundle source, with
   an explicit, reasoned allowlist for any bundle that still carries a flag guard.

Bundles are loaded straight from `bundles/python/*/src/app.py` under unique module names, with the
real `waddle_sdk` on `sys.path` and only the WIT `wit_world` import faked. Denominators are
asserted: a loader that found zero bundles/gates would otherwise pass vacuously.

Run: python3 -m pytest scripts/ci/tests/test_bundle_badge_gate.py
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLES_DIR = REPO_ROOT / "bundles" / "python"
SDK_SRC = REPO_ROOT / "sdk" / "waddle-sdk" / "src"

#: Hostile payloads: every one must be denied by a correct strict-boolean gate.
HOSTILE = {
    "string_false_mod": {"is_mod": "false"},
    "string_false_broadcaster": {"is_broadcaster": "false"},
    "string_false_both": {"is_mod": "false", "is_broadcaster": "false"},
    "string_false_mod_real_false_broadcaster": {"is_mod": "false", "is_broadcaster": False},
    "real_false_mod_string_false_broadcaster": {"is_mod": False, "is_broadcaster": "false"},
    "string_zero": {"is_mod": "0", "is_broadcaster": "0"},
    "string_zero_beside_real_false": {"is_mod": "0", "is_broadcaster": False},
    "string_no": {"is_mod": "no", "is_broadcaster": False},
    "empty_string_beside_real_false": {"is_mod": "", "is_broadcaster": False},
    "string_true_is_not_trusted": {"is_mod": "true", "is_broadcaster": False},
    "int_one_is_not_trusted": {"is_mod": 1, "is_broadcaster": False},
    "all_false": {"is_mod": False, "is_broadcaster": False},
}
ADMITTED = {
    "real_mod": {"is_mod": True, "is_broadcaster": False},
    "real_broadcaster": {"is_mod": False, "is_broadcaster": True},
    "real_mod_only_key": {"is_mod": True},
}


def _badge_bundles() -> list[Path]:
    """Every `bundles/python/<name>/src/app.py` that reads `is_mod`."""
    return sorted(
        p for p in BUNDLES_DIR.glob("*/src/app.py") if "is_mod" in p.read_text(encoding="utf-8")
    )


BADGE_APPS = _badge_bundles()

#: Identifiers whose presence in app bundle code means a feature-flag gate. App bundles must not use
#: them -- only SYSTEM bundles are flag-gated.
FLAG_IDENTIFIERS = frozenset({"feature_enabled", "FLAG_KEY", "FEATURE_FLAG_KEY"})

#: App bundles still carrying flag guards, with the reason. Exact-match enforced: an entry that is
#: no longer flagged fails the suite (stale allowlist), so this can only shrink.
FLAG_GUARD_ALLOWLIST = {
    "secret": "not yet converted to activation-only: also gates the hub-api secret-messaging "
    "endpoint behind waddles.secret-messaging; tracked as follow-up",
}


def _flag_identifiers_used(app_path: Path) -> set[str]:
    """Flag-gate identifiers a bundle's code (AST, so docstrings/comments don't count) references."""
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FLAG_IDENTIFIERS:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FLAG_IDENTIFIERS:
            found.add(node.attr)
        elif isinstance(node, ast.alias) and node.name in FLAG_IDENTIFIERS:
            found.add(node.name)
    return found


def _gate_functions(app_path: Path) -> list[tuple[str, bool]]:
    """`(name, takes_event)` for each sync single-arg function that reads BOTH badge keys.

    Found by AST so a renamed helper (`_is_privileged`, `_caller_role_signal`, ...) is still
    covered; `takes_event` is true when the parameter is annotated `PlatformEvent`.
    """
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    found: list[tuple[str, bool]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or len(node.args.args) != 1:
            continue
        keys = {
            n.value
            for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        if {"is_mod", "is_broadcaster"} <= keys:
            annotation = node.args.args[0].annotation
            found.append((node.name, annotation is not None and "PlatformEvent" in ast.unparse(annotation)))
    return found


def _fake_wit_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the minimum `wit_world` the SDK's lazy imports need (log sink + flags)."""
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        log=types.SimpleNamespace(Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3}, write=lambda *a: None),
        flags=types.SimpleNamespace(enabled=lambda key, default_value: True),
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)


def _load(app_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Import one bundle's `app.py` under a unique module name with its `src/` + the SDK on path."""
    name = f"bundle_badge_gate_{app_path.parent.parent.name}"
    monkeypatch.syspath_prepend(str(SDK_SRC))
    monkeypatch.syspath_prepend(str(app_path.parent))
    spec = importlib.util.spec_from_file_location(name, app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _platform_event(module: types.ModuleType, payload: dict[str, Any]) -> Any:
    from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload=dict(payload),
        occurred_at="2026-10-10T00:00:00.000Z",
    )


def _ids(paths: list[Path]) -> list[str]:
    return [p.parent.parent.name for p in paths]


def test_denominator_the_scan_found_the_badge_reading_bundles() -> None:
    """A mis-pointed glob must fail loudly, not pass vacuously."""
    assert len(BADGE_APPS) >= 30, f"expected >=30 badge-reading bundles, found {len(BADGE_APPS)}"


def test_app_bundles_are_flag_free_activation_gated_only() -> None:
    """No app bundle references `feature_enabled`/`FLAG_KEY` (beyond the exact reasoned allowlist).

    Denominator asserted: a glob that scanned nothing must FAIL, not pass vacuously.
    """
    apps = sorted(BUNDLES_DIR.glob("*/src/app.py"))
    assert len(apps) >= 20, f"expected >=20 app bundles scanned, found {len(apps)}"
    flagged = {p.parent.parent.name: sorted(ids) for p in apps if (ids := _flag_identifiers_used(p))}
    unexpected = {n: ids for n, ids in flagged.items() if n not in FLAG_GUARD_ALLOWLIST}
    assert not unexpected, (
        f"app bundles must be activation-gated only (no feature flags); flag guards found in "
        f"{unexpected} (scanned {len(apps)} bundles)"
    )
    stale = sorted(set(FLAG_GUARD_ALLOWLIST) - set(flagged))
    assert not stale, f"FLAG_GUARD_ALLOWLIST entries no longer flagged -- remove them: {stale}"


@pytest.mark.parametrize("app_path", BADGE_APPS, ids=_ids(BADGE_APPS))
def test_every_gate_helper_denies_hostile_badges_and_admits_real_ones(
    app_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_wit_world(monkeypatch)
    module = _load(app_path, monkeypatch)
    gates = _gate_functions(app_path)
    if not gates:
        pytest.skip(f"{app_path.parent.parent.name}: reads badges inline (covered by transform test)")
    for name, takes_event in gates:
        gate: Callable[[Any], Any] = getattr(module, name)
        for label, payload in HOSTILE.items():
            arg = _platform_event(module, payload) if takes_event else dict(payload)
            assert not gate(arg), f"{app_path.parent.parent.name}.{name} admitted hostile {label}"
        for label, payload in ADMITTED.items():
            arg = _platform_event(module, payload) if takes_event else dict(payload)
            assert gate(arg) is True, f"{app_path.parent.parent.name}.{name} denied genuine {label}"


def _command_text(app_path: Path) -> str:
    manifest = yaml.safe_load((app_path.parent.parent / "bundle.yaml").read_text(encoding="utf-8"))
    consumes = manifest["stages"]["process"]["consumes"]
    # A bundle that deliberately consumes ALL chat (e.g. `greeter`) declares no `filters` /
    # `command_prefix` (an absent filter key is "not a constraint"); probe it with its own command.
    prefixes = (consumes[0].get("filters") or {}).get("command_prefix")
    return str(prefixes[0]) if prefixes else f"!{app_path.parent.parent.name}"


def test_transform_never_launders_a_string_badge_into_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Across all bundles that forward badges, `transform()` emits a real `False` for `"false"`."""
    _fake_wit_world(monkeypatch)
    exercised: list[str] = []
    for app_path in BADGE_APPS:
        module = _load(app_path, monkeypatch)
        if hasattr(module, "feature_enabled"):  # allowlisted flag-guarded bundle: switch the guard on

            async def _on(*_a: Any, **_k: Any) -> bool:
                return True

            monkeypatch.setattr(module, "feature_enabled", _on)
        event = _platform_event(
            module, {"text": _command_text(app_path), "channel_id": "1", "is_mod": "false", "is_broadcaster": "false"}
        )
        try:
            result = asyncio.run(module.transform(event))
        except Exception:  # noqa: BLE001 -- a bundle that needs a bound context is skipped, not hidden
            continue
        if result is None or "is_mod" not in result.payload:
            continue
        exercised.append(app_path.parent.parent.name)
        assert result.payload["is_mod"] is False, f"{exercised[-1]} laundered a string is_mod"
        assert result.payload.get("is_broadcaster") is False, f"{exercised[-1]} laundered is_broadcaster"
    assert len(exercised) >= 20, f"only {len(exercised)} bundles exercised: {exercised}"


def test_transform_forwards_a_real_true_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: strictness must not break genuine moderators."""
    _fake_wit_world(monkeypatch)
    app_path = BUNDLES_DIR / "quote" / "src" / "app.py"
    module = _load(app_path, monkeypatch)
    event = _platform_event(module, {"text": "!quote add hi", "channel_id": "1", "is_mod": True, "is_broadcaster": False})
    result = asyncio.run(module.transform(event))
    assert result is not None
    assert result.payload["is_mod"] is True and result.payload["is_broadcaster"] is False
    assert inspect.iscoroutinefunction(module.transform)
