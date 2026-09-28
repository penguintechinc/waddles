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


_INIT_FUNCTION = {
    "name": "init",
    "kind": "freestanding",
    "params": [
        {"name": "app-name", "type": "string"},
        {"name": "symbols", "type": 999},
        {"name": "stub-wasi", "type": "bool"},
    ],
    "result": 1000,
}

#: `OWNER` is the interface id the componentize-py reflection interface
#: itself is placed at in `_componentize_py_runtime_doc` (index 4, right
#: after `_CONFORMANT_INTERFACES`) -- every named local type below must
#: report exactly this as its `owner` to pass `_check_type`'s ownership
#: check, matching a real capture verbatim.
_OWNER = 4


def _real_symbols_and_result_types(*, owner: int = _OWNER) -> tuple[list[dict[str, Any]], int, int]:
    """The exact `symbols`/`init`-result type table.

    Captured verbatim from a real `bundles/python/pyping` build
    (`wasm-tools component wit --json`, componentize-py 0.25.1), renumbered
    to a compact local 0..27 range. Returns
    `(types, symbols_type_id, result_type_id)`.
    """
    types: list[dict[str, Any]] = [
        {  # 0: function
            "name": "function",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "protocol", "type": "string"},
                        {"name": "name", "type": "string"},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 1: constructor
            "name": "constructor",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "module", "type": "string"},
                        {"name": "protocol", "type": "string"},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 2: static
            "name": "static",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "module", "type": "string"},
                        {"name": "protocol", "type": "string"},
                        {"name": "name", "type": "string"},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 3: function-export-kind
            "name": "function-export-kind",
            "kind": {
                "variant": {
                    "cases": [
                        {"name": "freestanding", "type": 0},
                        {"name": "constructor", "type": 1},
                        {"name": "method", "type": "string"},
                        {"name": "static", "type": 2},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 4: return-style
            "name": "return-style",
            "kind": {
                "variant": {
                    "cases": [
                        {"name": "none", "type": None},
                        {"name": "normal", "type": None},
                        {"name": "result", "type": None},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 5: function-export
            "name": "function-export",
            "kind": {
                "record": {
                    "fields": [{"name": "kind", "type": 3}, {"name": "return-style", "type": 4}]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 6: resource
            "name": "resource",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "package", "type": "string"},
                        {"name": "name", "type": "string"},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {"name": None, "kind": {"list": "string"}, "owner": None},  # 7: list<string>
        {  # 8: record
            "name": "record",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "package", "type": "string"},
                        {"name": "name", "type": "string"},
                        {"name": "fields", "type": 7},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 9: flags
            "name": "flags",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "package", "type": "string"},
                        {"name": "name", "type": "string"},
                        {"name": "u32-count", "type": "u32"},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 10: tuple
            "name": "tuple",
            "kind": {"record": {"fields": [{"name": "count", "type": "u32"}]}},
            "owner": {"interface": owner},
        },
        {  # 11: case
            "name": "case",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "name", "type": "string"},
                        {"name": "has-payload", "type": "bool"},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {"name": None, "kind": {"list": 11}, "owner": None},  # 12: list<case>
        {  # 13: variant
            "name": "variant",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "package", "type": "string"},
                        {"name": "name", "type": "string"},
                        {"name": "cases", "type": 12},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 14: enum
            "name": "enum",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "package", "type": "string"},
                        {"name": "name", "type": "string"},
                        {"name": "count", "type": "u32"},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {  # 15: option-kind
            "name": "option-kind",
            "kind": {"enum": {"cases": [{"name": "non-nesting"}, {"name": "nesting"}]}},
            "owner": {"interface": owner},
        },
        {  # 16: result-record
            "name": "result-record",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "has-ok", "type": "bool"},
                        {"name": "has-err", "type": "bool"},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {"name": None, "kind": {"list": 5}, "owner": None},  # 17: list<function-export>
        {"name": None, "kind": {"list": 6}, "owner": None},  # 18: list<resource>
        {"name": None, "kind": {"list": 8}, "owner": None},  # 19: list<record>
        {"name": None, "kind": {"list": 9}, "owner": None},  # 20: list<flags>
        {"name": None, "kind": {"list": 10}, "owner": None},  # 21: list<tuple>
        {"name": None, "kind": {"list": 13}, "owner": None},  # 22: list<variant>
        {"name": None, "kind": {"list": 14}, "owner": None},  # 23: list<enum>
        {"name": None, "kind": {"list": 15}, "owner": None},  # 24: list<option-kind>
        {"name": None, "kind": {"list": 16}, "owner": None},  # 25: list<result-record>
        {  # 26: symbols
            "name": "symbols",
            "kind": {
                "record": {
                    "fields": [
                        {"name": "exports", "type": 17},
                        {"name": "resources", "type": 18},
                        {"name": "records", "type": 19},
                        {"name": "flags", "type": 20},
                        {"name": "tuples", "type": 21},
                        {"name": "variants", "type": 22},
                        {"name": "enums", "type": 23},
                        {"name": "options", "type": 24},
                        {"name": "results", "type": 25},
                    ]
                }
            },
            "owner": {"interface": owner},
        },
        {"name": None, "kind": {"result": {"ok": None, "err": "string"}}, "owner": None},  # 27
    ]
    return types, 26, 27


def _real_init_function() -> dict[str, Any]:
    """`init`'s real signature, referencing the real `symbols`/result type ids above."""
    _, symbols_id, result_id = _real_symbols_and_result_types()
    return {
        "name": "init",
        "kind": "freestanding",
        "params": [
            {"name": "app-name", "type": "string"},
            {"name": "symbols", "type": symbols_id},
            {"name": "stub-wasi", "type": "bool"},
        ],
        "result": result_id,
    }


def _componentize_py_runtime_doc(
    *,
    functions: dict[str, Any],
    types: list[dict[str, Any]] | None = None,
    package_name: str = "root:component",
    interface_name: str | None = None,
) -> dict[str, Any]:
    """A conformant doc plus componentize-py's synthetic `root:component` export.

    Matches the exact shape captured from a real `bundles/python/pyping`
    build (`wasm-tools component wit --json`, componentize-py 0.25.1) --
    see `bundle_component_validator`'s module docstring. `types`/
    `package_name`/`interface_name` are overridable so hostile-lookalike
    tests can mutate exactly one dimension of the disguise at a time.
    """
    interfaces = [
        *_CONFORMANT_INTERFACES,
        {"name": interface_name, "package": 2, "functions": functions},
    ]
    packages = [*_CONFORMANT_PACKAGES, {"name": package_name}]
    return _doc(
        packages=packages,
        interfaces=interfaces,
        imports={"interface-0": _iface_ref(0)},
        exports={
            "interface-0": _iface_ref(4),
            "interface-1": _iface_ref(1),
            "interface-2": _iface_ref(2),
        },
    ) | {"types": types if types is not None else []}


async def test_componentize_py_runtime_reflection_export_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real, unavoidable componentize-py 0.25.1 build artifact passes.

    Regression: fix/pyping-wit-conformance -- pyping's real build was
    rejected with `disallowed_export:['root:component/None']` before this
    fix.
    """
    types, _, _ = _real_symbols_and_result_types()
    doc = _componentize_py_runtime_doc(functions={"init": _real_init_function()}, types=types)
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is True, result.reason
    assert set(result.exports) == {
        "root:component/<anonymous>",
        "waddle:bundle/process-stage",
        "waddle:bundle/action-stage",
    }


async def test_reflection_export_lookalike_with_extra_function_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile component adding a second function to the disguised interface is still caught."""
    doc = _componentize_py_runtime_doc(
        functions={
            "init": _INIT_FUNCTION,
            "steal-secrets": {**_INIT_FUNCTION, "name": "steal-secrets"},
        }
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "root:component/<anonymous>" in (result.reason or "")


async def test_reflection_export_lookalike_with_renamed_function_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile component renaming the single function away from `init` is still caught."""
    doc = _componentize_py_runtime_doc(
        functions={"steal-secrets": {**_INIT_FUNCTION, "name": "steal-secrets"}}
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "root:component/<anonymous>" in (result.reason or "")


async def test_reflection_export_lookalike_with_wrong_params_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile `init` with a different parameter shape is still caught."""
    forged_init = {**_INIT_FUNCTION, "params": [{"name": "app-name", "type": "string"}]}
    doc = _componentize_py_runtime_doc(functions={"init": forged_init})
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "root:component/<anonymous>" in (result.reason or "")


async def test_reflection_export_lookalike_with_different_package_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identical shape under a non-`root:component` package is rejected."""
    types, _, _ = _real_symbols_and_result_types()
    doc = _componentize_py_runtime_doc(
        functions={"init": _real_init_function()}, types=types, package_name="evil:component"
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "evil:component/<anonymous>" in (result.reason or "")


async def test_reflection_export_lookalike_with_named_interface_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identical function under a NAMED (non-anonymous) interface is rejected."""
    types, _, _ = _real_symbols_and_result_types()
    doc = _componentize_py_runtime_doc(
        functions={"init": _real_init_function()}, types=types, interface_name="sneaky"
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "root:component/sneaky" in (result.reason or "")


async def test_reflection_export_lookalike_with_altered_result_type_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `init` whose `result.err` has been widened to `u32` instead of `string` is rejected."""
    types, symbols_id, result_id = _real_symbols_and_result_types()
    types[result_id] = {
        "name": None,
        "kind": {"result": {"ok": None, "err": "u32"}},
        "owner": None,
    }
    init_fn = {
        "name": "init",
        "kind": "freestanding",
        "params": [
            {"name": "app-name", "type": "string"},
            {"name": "symbols", "type": symbols_id},
            {"name": "stub-wasi", "type": "bool"},
        ],
        "result": result_id,
    }
    doc = _componentize_py_runtime_doc(functions={"init": init_fn}, types=types)
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "root:component/<anonymous>" in (result.reason or "")


async def test_reflection_export_lookalike_with_altered_symbols_record_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `symbols` record whose nested `resource` record gained an extra field is rejected.

    Simulates a hostile component smuggling attacker-controlled data through
    an otherwise-identical-looking nested type, several levels deep.
    """
    types, symbols_id, result_id = _real_symbols_and_result_types()
    types[6] = {  # resource: package/name -> package/name/exfil
        "name": "resource",
        "kind": {
            "record": {
                "fields": [
                    {"name": "package", "type": "string"},
                    {"name": "name", "type": "string"},
                    {"name": "exfil", "type": "string"},
                ]
            }
        },
        "owner": {"interface": _OWNER},
    }
    init_fn = {
        "name": "init",
        "kind": "freestanding",
        "params": [
            {"name": "app-name", "type": "string"},
            {"name": "symbols", "type": symbols_id},
            {"name": "stub-wasi", "type": "bool"},
        ],
        "result": result_id,
    }
    doc = _componentize_py_runtime_doc(functions={"init": init_fn}, types=types)
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "root:component/<anonymous>" in (result.reason or "")


async def test_componentize_py_runtime_export_is_rejected_on_the_import_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact same benign shape is never allowed as an IMPORT -- export-only exception."""
    interfaces = [
        *_CONFORMANT_INTERFACES,
        {"name": None, "package": 2, "functions": {"init": _INIT_FUNCTION}},
    ]
    packages = [*_CONFORMANT_PACKAGES, {"name": "root:component"}]
    doc = _doc(
        packages=packages,
        interfaces=interfaces,
        imports={"interface-0": _iface_ref(0), "interface-1": _iface_ref(4)},
        exports={"interface-0": _iface_ref(1), "interface-1": _iface_ref(2)},
    )
    monkeypatch.setattr(validator, "_run_wasm_tools_wit_json", lambda data: doc)
    result = await validator.validate_component(b"fake")
    assert result.ok is False
    assert "root:component/<anonymous>" in (result.reason or "")


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
