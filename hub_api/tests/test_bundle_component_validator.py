"""Tests for `validate_component()` -- the WIT `stage`-world conformance gate.

`_run_wasm_tools_wit_json` (the subprocess boundary) is mocked for every
test except `test_real_conformant_component_validates_ok`, which runs the
real `wasm-tools` CLI against a real compiled component fixture
(`core/bundle_executor/tests/fixtures/hostile_fixture.wasm`) to prove the
allowlist actually matches what a real Rust-built component targeting
this WIT world produces -- not just a hand-shaped JSON double.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services import bundle_component_validator as validator

_HOSTILE_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "core"
    / "bundle_executor"
    / "tests"
    / "fixtures"
    / "hostile_fixture.wasm"
)


def _doc(
    *,
    packages: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    imports: dict[str, Any],
    exports: dict[str, Any],
) -> dict[str, Any]:
    return {
        "worlds": [{"name": "root", "imports": imports, "exports": exports, "package": 0}],
        "interfaces": interfaces,
        "types": [],
        "packages": packages,
    }


def _iface_ref(idx: int) -> dict[str, Any]:
    return {"interface": {"id": idx}}


_CONFORMANT_PACKAGES = [{"name": "waddle:bundle@1.0.0"}, {"name": "wasi:cli@0.2.3"}]
_CONFORMANT_INTERFACES = [
    {"name": "context", "package": 0},
    {"name": "process-stage", "package": 0},
    {"name": "action-stage", "package": 0},
    {"name": "environment", "package": 1},
]


def _conformant_doc() -> dict[str, Any]:
    return _doc(
        packages=_CONFORMANT_PACKAGES,
        interfaces=_CONFORMANT_INTERFACES,
        imports={"interface-0": _iface_ref(0), "interface-1": _iface_ref(3)},
        exports={"interface-0": _iface_ref(1), "interface-1": _iface_ref(2)},
    )


async def test_conformant_component_validates_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: _conformant_doc())
    result = await validator.validate_component(b"fake")
    assert result.ok is True
    assert result.reason is None
    assert set(result.exports) == {"waddle:bundle/process-stage", "waddle:bundle/action-stage"}


async def test_disallowed_import_namespace_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    packages = [*_CONFORMANT_PACKAGES, {"name": "wasi:http@0.2.3"}]
    interfaces = [*_CONFORMANT_INTERFACES, {"name": "outgoing-handler", "package": 2}]
    doc = _doc(
        packages=packages,
        interfaces=interfaces,
        imports={"interface-0": _iface_ref(0), "interface-1": _iface_ref(4)},
        exports={"interface-0": _iface_ref(1), "interface-1": _iface_ref(2)},
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "wasi:http/outgoing-handler" in (result.reason or "")


async def test_allowed_wasi_socket_import_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wasi:sockets` is import-allowed -- denied at RUNTIME, not import time (engine.rs)."""
    packages = [*_CONFORMANT_PACKAGES, {"name": "wasi:sockets@0.2.3"}]
    interfaces = [*_CONFORMANT_INTERFACES, {"name": "tcp-create-socket", "package": 2}]
    doc = _doc(
        packages=packages,
        interfaces=interfaces,
        imports={"interface-0": _iface_ref(0), "interface-1": _iface_ref(4)},
        exports={"interface-0": _iface_ref(1), "interface-1": _iface_ref(2)},
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is True


async def test_missing_stage_export_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc(
        packages=_CONFORMANT_PACKAGES,
        interfaces=_CONFORMANT_INTERFACES,
        imports={"interface-0": _iface_ref(0)},
        exports={"interface-0": _iface_ref(1)},  # only process-stage, missing action-stage
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "action-stage" in (result.reason or "")


async def test_extra_export_beyond_the_stage_world_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = [*_CONFORMANT_PACKAGES]
    interfaces = [*_CONFORMANT_INTERFACES, {"name": "rogue", "package": 0}]
    doc = _doc(
        packages=packages,
        interfaces=interfaces,
        imports={"interface-0": _iface_ref(0)},
        exports={
            "interface-0": _iface_ref(1),
            "interface-1": _iface_ref(2),
            "interface-2": _iface_ref(4),
        },
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False


async def test_non_interface_import_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc(
        packages=_CONFORMANT_PACKAGES,
        interfaces=_CONFORMANT_INTERFACES,
        imports={"func-0": {"function": {"name": "mystery"}}},
        exports={"interface-0": _iface_ref(1), "interface-1": _iface_ref(2)},
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False


async def test_not_a_valid_component_is_rejected_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validator, "_run_wasm_tools_wit_json", lambda data: {"_decode_error": "garbage"}
    )
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert result.reason is not None and result.reason.startswith("not_a_valid_component")


async def test_missing_wasm_tools_binary_raises_unavailable_not_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validator.shutil, "which", lambda name: None)
    with pytest.raises(validator.ComponentValidatorUnavailableError):
        await validator.validate_component(b"fake")


@pytest.mark.skipif(not _HOSTILE_FIXTURE.exists(), reason="fixture not present in this checkout")
async def test_real_conformant_component_validates_ok() -> None:
    """End-to-end against a real compiled component, no mocking of the subprocess boundary."""
    data = _HOSTILE_FIXTURE.read_bytes()
    result = await validator.validate_component(data)
    assert result.ok is True, result.reason
    assert set(result.exports) == {"waddle:bundle/process-stage", "waddle:bundle/action-stage"}
