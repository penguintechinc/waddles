# ruff: noqa: S404, S603, S607
"""Real-binding tests for ``waddle_sdk._component_entry.ToolProvider`` (``world stage-tools``).

Unlike the fake-``wit_world`` unit tests, these run ``componentize-py`` (a
``build`` extra, so a missing toolchain FAILS here, never skips) against the
committed ``wit/waddle-bundle`` WIT:

* ``bindings`` -> the REAL generated ``wit_world`` (exports ``ToolProvider``
  Protocol, ``imports.tool.ToolError_*`` variants, real ``componentize_py_types.Err``)
  is put on ``sys.path`` and the entry's ``call`` is driven against it.
* ``componentize`` -> the ``tool_echo_provider`` fixture builds into a real component
  that exports ``waddle:bundle/tool-provider``.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

SDK_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SDK_ROOT.parents[1]
WIT_DIR = REPO_ROOT / "wit" / "waddle-bundle"
FIXTURE = Path(__file__).parent / "fixtures" / "tool_echo_provider"
_STALE = ("wit_world", "componentize_py_types", "componentize_py_async_support", "poll_loop")


def _cp() -> str:
    exe = shutil.which("componentize-py") or str(Path(sys.executable).parent / "componentize-py")
    assert Path(exe).exists(), "componentize-py missing: install waddle-sdk[build]"
    return exe


@pytest.fixture(scope="module")
def real_bindings(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("stage-tools-bindings")
    subprocess.run(
        [_cp(), "-d", str(WIT_DIR), "-w", "stage-tools", "bindings", str(out)],
        check=True,
        capture_output=True,
    )
    assert (out / "wit_world" / "exports" / "__init__.py").is_file()
    return out


@pytest.fixture
def entry(real_bindings: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Import ``_component_entry`` against the real generated bindings + echo fixture."""
    for name in list(sys.modules):
        if name in _STALE or name.startswith(("wit_world.", "waddle_sdk.tool", "app")):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "waddle_sdk._component_entry", raising=False)
    monkeypatch.delitem(sys.modules, "_entry_wiring", raising=False)
    monkeypatch.syspath_prepend(str(real_bindings))
    monkeypatch.syspath_prepend(str(FIXTURE))
    import wit_world.imports.context_v2 as ctx_mod

    monkeypatch.setattr(
        ctx_mod,
        "get_context",
        lambda: ctx_mod.BundleContext(
            tenant_id=7,
            community_id=9,
            tenant="acme",
            community=None,
            app_id="fixture.echo",
            feature="echo",
            version="1.0.0",
            message_id="m1",
            config_json="{}",
        ),
    )
    mod = importlib.import_module("waddle_sdk._component_entry")
    yield mod
    for name in list(sys.modules):
        if name in _STALE or name.startswith(("wit_world.", "waddle_sdk.tool", "app")):
            sys.modules.pop(name, None)
    sys.modules.pop("waddle_sdk._component_entry", None)
    sys.modules.pop("_entry_wiring", None)


def test_real_export_protocol_matches_entry(entry: Any) -> None:
    import wit_world.exports as exports

    assert hasattr(exports, "ToolProvider")
    assert callable(entry.ToolProvider().call)


def test_call_routes_to_registered_handler(entry: Any) -> None:
    args = json.dumps({"a": 1, "__idempotency_key": "k-1"}).encode()
    out = entry.ToolProvider().call("echo", args)
    assert isinstance(out, bytes)
    assert json.loads(out) == {"echo": {"a": 1}, "idempotency_key": "k-1"}


def test_unknown_tool_maps_to_real_wit_invalid_args(entry: Any) -> None:
    import wit_world.imports.tool as tool_mod
    from componentize_py_types import Err

    with pytest.raises(Err) as ei:
        entry.ToolProvider().call("nope", b"{}")
    assert isinstance(ei.value.value, tool_mod.ToolError_InvalidArgs)
    assert "nope" in ei.value.value.value


def test_malformed_args_map_to_invalid_args(entry: Any) -> None:
    import wit_world.imports.tool as tool_mod
    from componentize_py_types import Err

    with pytest.raises(Err) as ei:
        entry.ToolProvider().call("echo", b"not json")
    assert isinstance(ei.value.value, tool_mod.ToolError_InvalidArgs)


def test_every_code_maps_to_a_real_variant(entry: Any) -> None:
    import wit_world.imports.tool as tool_mod

    from waddle_sdk.tool import CODES

    assert set(entry._TOOL_VARIANTS) == set(CODES)
    for variant in entry._TOOL_VARIANTS.values():
        assert hasattr(tool_mod, variant)


def test_fixture_componentizes_and_exports_tool_provider(tmp_path: Path) -> None:
    out = tmp_path / "echo.wasm"
    proc = subprocess.run(
        [
            _cp(),
            "-d",
            str(WIT_DIR),
            "-w",
            "stage-tools",
            "componentize",
            "-p",
            str(SDK_ROOT / "src"),
            "-p",
            str(FIXTURE),
            "waddle_sdk._component_entry",
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert out.stat().st_size > 0
    wasm_tools = shutil.which("wasm-tools")
    if wasm_tools:
        wit = subprocess.run(
            [wasm_tools, "component", "wit", str(out)], capture_output=True, text=True, check=True
        ).stdout
        assert "export waddle:bundle/tool-provider@1.0.0" in wit
