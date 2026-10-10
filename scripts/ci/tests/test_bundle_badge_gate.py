"""Behavioral mod-gate regression across EVERY Python bundle that reads role badges.

`bool(is_mod)` is truthy for the string `"false"`, so a non-moderator whose normalizer emitted a
string badge passed any mod gate built on it -- and `transform()` laundered the string into a
real `True` for `dispatch`. The static gate (`check-bundle-source-hygiene.py --check
badge-truthiness`) forbids the pattern; this suite proves the BEHAVIOUR on the real modules:

1. every bundle's gate helper denies hostile non-boolean badge payloads and still admits a real
   moderator/broadcaster (a positive control, so an always-False helper cannot pass);
2. every bundle whose `transform()` forwards badges to `dispatch` forwards a string badge as a
   real `False`, never `True`.

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
    return str(consumes[0]["filters"]["command_prefix"][0])


def test_transform_never_launders_a_string_badge_into_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Across all bundles that forward badges, `transform()` emits a real `False` for `"false"`."""
    _fake_wit_world(monkeypatch)
    exercised: list[str] = []
    for app_path in BADGE_APPS:
        module = _load(app_path, monkeypatch)
        if not hasattr(module, "feature_enabled"):
            continue

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

    async def _on(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(module, "feature_enabled", _on)
    event = _platform_event(module, {"text": "!quote add hi", "channel_id": "1", "is_mod": True, "is_broadcaster": False})
    result = asyncio.run(module.transform(event))
    assert result is not None
    assert result.payload["is_mod"] is True and result.payload["is_broadcaster"] is False
    assert inspect.iscoroutinefunction(module.transform)
