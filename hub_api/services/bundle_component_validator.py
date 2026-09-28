"""WIT `stage`-world conformance check for an uploaded pre-built component.

spec Sec9.1's INSPECTING state ("WIT/import/size check"; size is already
enforced by `bundle_version_service.BUNDLE_MAX_COMPONENT_BYTES` before this
module ever runs) -- this is hub-api's OWN trust-boundary gate on a vendor's
raw `.wasm` upload, independent of `core/bundle_compiler/src/validate/mod.rs`
(stubbed this wave, Rust-side, compile-time only).

**Mechanism**: `wasm-tools component wit --json` (the `wasm-tools` CLI,
upstream bytecodealliance tooling, already used by this repo's own WIT
tests -- see `wit/waddle-bundle/stage_wit_test.sh`) decodes the component
and reprints its OWN derived world -- the actual link-time import/export
surface -- as structured JSON. This is the lightest correct mechanism:
it reuses a real WIT/component-model parser instead of hand-rolling one
against the raw wasm binary format, and needs nothing beyond the CLI
already on this image's `PATH` (see `hub_api/Dockerfile`'s pinned,
checksum-verified install).

**The allowlist.** A conformant component's imports are NOT limited to
`stage.wit`'s eight named world imports (`context`/`http`/`kv`/`db`/
`relay`/`%flags`/`log`/`clock`) -- `componentize-py` (Python) and
`cargo-component` (Rust) both statically link the FULL WASI-p2 import
surface regardless of what the target world declares, and
`core/bundle_executor/src/engine.rs::build_linker` confirms this is
exactly what the Rust executor itself links at runtime
(`wasmtime_wasi::p2::add_to_linker_async`, "wasi:sockets is linked too --
`componentize-py` links the full WASI P2 import set regardless of the
declared world, so refusing to link it at all would fail Python-built
components at instantiation time"). This module's allowlist is therefore
the UNION of:

  - any interface in the `waddle:bundle` package (including `types` --
    a benign `wit-bindgen`/`componentize-py` artifact of `use
    types.{...}` inside the exported stage interfaces, confirmed against
    `core/bundle_executor/tests/fixtures/hostile_fixture.wasm`, a real
    compiled Rust component targeting this exact world), and
  - the standard WASI-p2 namespaces the executor actually links:
    `wasi:cli`, `wasi:clocks`, `wasi:filesystem`, `wasi:io`,
    `wasi:random`, `wasi:sockets`. `wasi:sockets` stays on this
    allowlist deliberately -- linking it is permitted, but every call
    through it is denied natively at RUNTIME by the executor's
    `WasiCtx` (`allow_tcp(false)`/`allow_udp(false)`, assumption A19),
    not refused at import time.

Anything outside this union -- `wasi:http/*`, a bare top-level function
import, an unrelated namespace -- is rejected. Narrowing this list to
only Rust's WASI subset (as a naive reading of `stage.wit` alone would
suggest) would reject every legitimate Python component; do not do that.

Exports are checked strictly: the component MUST export exactly
`waddle:bundle/process-stage` and `waddle:bundle/action-stage` (the
`stage` world's own export set) -- neither more nor fewer -- with one
additional, narrowly-matched exception: `componentize-py` 0.25.1
unconditionally links an anonymous, un-namespaced reflection export
(package `root:component`, no interface name, a single `init` function)
into EVERY component it builds -- its own embedded-CPython runtime's
dynamic-dispatch bootstrap, entirely orthogonal to the target WIT world
(confirmed against a real `bundles/python/pyping` build: `stage.wit`
never declares it, yet it is present regardless of world/entry/WIT path).
`core/bundle_executor/src/engine.rs`'s `wasmtime::component::bindgen!`-
generated `Stage` binding looks up exactly `waddle:bundle/process-stage`/
`action-stage` by name and never enumerates or calls anything else, so
allowing this export grants no capability -- it is never invoked by the
executor. It is also not a new instantiation hook: any WASI-p2 core-module
start section runs at instantiation for every component already,
independent of its exported interface surface -- this allowlist entry
neither grants nor widens that behavior, it only lets `wasm-tools`'s own
`wit --json` reflection of this one, already-inert, already-happening
artifact pass through the export check unmolested.

`_is_componentize_py_runtime_reflection_export` matches it exactly, not
loosely:

  - package `root:component`, interface name `None` (anonymous), exactly
    one function named `init`, with exactly the parameter names
    `app-name`/`symbols`/`stub-wasi` in that order;
  - `app-name: string` and `stub-wasi: bool` are checked as literal WIT
    primitives;
  - `symbols` is checked against `_COMPONENTIZE_PY_SYMBOLS_SHAPE` --
    the record's full field set, in order, resolved **recursively**
    through every nested list/record/variant/enum it contains (captured
    verbatim from a real build's `wasm-tools component wit --json`
    output), not merely "is some record" or "is an int";
  - `init`'s return type is checked against
    `_COMPONENTIZE_PY_INIT_RESULT_SHAPE` (`result<_, string>`), not left
    unchecked;
  - every named type in both shapes must additionally be OWNED by this
    same anonymous interface (`_check_type`'s `owner_interface_id`
    check) -- a hostile component cannot satisfy the shape by pointing
    `symbols`/the result type at a same-shaped type borrowed from
    elsewhere in the component.

This closes the gap a shallower check would leave open: matching only on
package/name/param-names would let a hostile component reuse that half
of the disguise while smuggling a *different* `symbols` record (e.g. one
with an extra field carrying attacker-controlled data) or a different
result type through the same `init` signature.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess  # nosec B404 -- fixed argv, no shell, trusted CLI (see module docstring)
import tempfile
from dataclasses import dataclass, field
from typing import Any

WADDLE_BUNDLE_NAMESPACE = "waddle:bundle"

#: WASI-p2 namespaces `core/bundle_executor/src/engine.rs::build_linker` links
#: for every component (Rust or Python built) -- see module docstring.
ALLOWED_WASI_NAMESPACES = frozenset(
    {"wasi:cli", "wasi:clocks", "wasi:filesystem", "wasi:io", "wasi:random", "wasi:sockets"}
)

#: The `stage` world's own export set (`wit/waddle-bundle/stage.wit`) -- a
#: conformant component exports exactly these two, never more or fewer.
REQUIRED_EXPORTS = frozenset({"process-stage", "action-stage"})

#: `componentize-py`'s synthetic reflection export always reports this exact
#: package name and no interface name (see module docstring).
_COMPONENTIZE_PY_RUNTIME_EXPORT_PACKAGE = "root:component"
#: `init(app-name: string, symbols: <record>, stub-wasi: bool) -> result<_, string>`
#: -- the fixed parameter order/names this export's single function must have.
_COMPONENTIZE_PY_RUNTIME_INIT_PARAM_NAMES = ("app-name", "symbols", "stub-wasi")


def _rec(name: str, fields: list[tuple[str, Any]]) -> dict[str, Any]:
    """A named WIT `record` shape node -- see `_check_type`."""
    return {"kind": "record", "name": name, "fields": fields}


def _lst(of: Any) -> dict[str, Any]:
    """An anonymous WIT `list<of>` shape node -- see `_check_type`."""
    return {"kind": "list", "of": of}


def _res(ok: Any, err: Any) -> dict[str, Any]:
    """An anonymous WIT `result<ok, err>` shape node -- see `_check_type`."""
    return {"kind": "result", "ok": ok, "err": err}


def _var(name: str, cases: list[tuple[str, Any]]) -> dict[str, Any]:
    """A named WIT `variant` shape node -- see `_check_type`."""
    return {"kind": "variant", "name": name, "cases": cases}


def _enum(name: str, cases: list[str]) -> dict[str, Any]:
    """A named WIT `enum` shape node -- see `_check_type`."""
    return {"kind": "enum", "name": name, "cases": cases}


_FUNCTION = _rec("function", [("protocol", "string"), ("name", "string")])
_CONSTRUCTOR = _rec("constructor", [("module", "string"), ("protocol", "string")])
_STATIC = _rec("static", [("module", "string"), ("protocol", "string"), ("name", "string")])
_FUNCTION_EXPORT_KIND = _var(
    "function-export-kind",
    [
        ("freestanding", _FUNCTION),
        ("constructor", _CONSTRUCTOR),
        ("method", "string"),
        ("static", _STATIC),
    ],
)
_RETURN_STYLE = _var("return-style", [("none", None), ("normal", None), ("result", None)])
_FUNCTION_EXPORT = _rec(
    "function-export", [("kind", _FUNCTION_EXPORT_KIND), ("return-style", _RETURN_STYLE)]
)
_RESOURCE = _rec("resource", [("package", "string"), ("name", "string")])
_RECORD_TYPE = _rec(
    "record", [("package", "string"), ("name", "string"), ("fields", _lst("string"))]
)
_FLAGS_TYPE = _rec("flags", [("package", "string"), ("name", "string"), ("u32-count", "u32")])
_TUPLE_TYPE = _rec("tuple", [("count", "u32")])
_CASE_TYPE = _rec("case", [("name", "string"), ("has-payload", "bool")])
_VARIANT_TYPE = _rec(
    "variant", [("package", "string"), ("name", "string"), ("cases", _lst(_CASE_TYPE))]
)
_ENUM_TYPE = _rec("enum", [("package", "string"), ("name", "string"), ("count", "u32")])
_OPTION_KIND_TYPE = _enum("option-kind", ["non-nesting", "nesting"])
_RESULT_RECORD_TYPE = _rec("result-record", [("has-ok", "bool"), ("has-err", "bool")])

#: The exact `symbols` record shape componentize-py 0.25.1's `init` export
#: takes as its second parameter -- captured verbatim (field names, order,
#: and every nested type, recursively) from a real `bundles/python/pyping`
#: build's `wasm-tools component wit --json` output. Anything narrower or
#: wider is rejected, never loosely matched on "is a record"/"is an int".
_COMPONENTIZE_PY_SYMBOLS_SHAPE = _rec(
    "symbols",
    [
        ("exports", _lst(_FUNCTION_EXPORT)),
        ("resources", _lst(_RESOURCE)),
        ("records", _lst(_RECORD_TYPE)),
        ("flags", _lst(_FLAGS_TYPE)),
        ("tuples", _lst(_TUPLE_TYPE)),
        ("variants", _lst(_VARIANT_TYPE)),
        ("enums", _lst(_ENUM_TYPE)),
        ("options", _lst(_OPTION_KIND_TYPE)),
        ("results", _lst(_RESULT_RECORD_TYPE)),
    ],
)

#: `init`'s real return type: `result<_, string>` (no `ok` payload, `err` is
#: a plain string) -- captured from the same real build as the shape above.
_COMPONENTIZE_PY_INIT_RESULT_SHAPE = _res(None, "string")


def _check_type(doc: dict[str, Any], type_ref: Any, expected: Any, owner_interface_id: int) -> bool:
    """Recursively verify `type_ref` matches the structural shape `expected`.

    `type_ref` is a WIT type reference as `wasm-tools component wit --json`
    encodes it: either a bare primitive string (`"string"`, `"bool"`,
    `"u32"`, ...), `None` (a variant case / return-style with no payload),
    or an `int` index into `doc["types"]`. `expected` is one of this
    module's `_rec`/`_lst`/`_res`/`_var`/`_enum` shape nodes, or a bare
    primitive string, or `None`.

    Every named type (`record`/`variant`/`enum`) must additionally be
    owned by `owner_interface_id` -- the same anonymous interface being
    validated -- so a hostile component cannot satisfy the shape by
    pointing `symbols`/`init`'s result at a same-shaped type borrowed from
    somewhere else in the component. Anonymous types (`list`/`result`)
    must have no owner at all, matching the real capture.
    """
    if isinstance(expected, str):
        return bool(type_ref == expected)
    if expected is None or type_ref is None:
        return expected is None and type_ref is None
    if not isinstance(type_ref, int) or not (0 <= type_ref < len(doc.get("types", []))):
        return False
    node = doc["types"][type_ref]
    if not isinstance(node, dict):
        return False
    kind_obj = node.get("kind")
    if not isinstance(kind_obj, dict) or len(kind_obj) != 1:
        return False
    ((tag, value),) = kind_obj.items()
    if tag != expected.get("kind"):
        return False

    if tag in ("record", "variant", "enum"):
        if node.get("name") != expected["name"]:
            return False
        if node.get("owner") != {"interface": owner_interface_id}:
            return False
    elif node.get("owner") is not None:
        # list/result: always anonymous in the real capture.
        return False

    if tag == "record":
        exp_fields = expected["fields"]
        fields = value.get("fields") if isinstance(value, dict) else None
        if not isinstance(fields, list) or len(fields) != len(exp_fields):
            return False
        return all(
            f.get("name") == exp_name
            and _check_type(doc, f.get("type"), exp_type, owner_interface_id)
            for f, (exp_name, exp_type) in zip(fields, exp_fields, strict=True)
        )
    if tag == "list":
        return _check_type(doc, value, expected["of"], owner_interface_id)
    if tag == "result":
        if not isinstance(value, dict):
            return False
        return _check_type(
            doc, value.get("ok"), expected["ok"], owner_interface_id
        ) and _check_type(doc, value.get("err"), expected["err"], owner_interface_id)
    if tag == "variant":
        exp_cases = expected["cases"]
        cases = value.get("cases") if isinstance(value, dict) else None
        if not isinstance(cases, list) or len(cases) != len(exp_cases):
            return False
        return all(
            c.get("name") == exp_name
            and _check_type(doc, c.get("type"), exp_type, owner_interface_id)
            for c, (exp_name, exp_type) in zip(cases, exp_cases, strict=True)
        )
    if tag == "enum":
        exp_cases = expected["cases"]
        cases = value.get("cases") if isinstance(value, dict) else None
        if not isinstance(cases, list) or len(cases) != len(exp_cases):
            return False
        return all(c.get("name") == exp_name for c, exp_name in zip(cases, exp_cases, strict=True))
    return False


_SUBPROCESS_TIMEOUT_S = 15


class ComponentValidatorUnavailableError(RuntimeError):
    """Raised when `wasm-tools` cannot be located or invoked.

    Deliberately NOT a validation failure (`ComponentValidationResult(ok=False,
    ...)`) -- an infra problem (missing binary, killed subprocess) must never
    be indistinguishable from "this component is genuinely non-conformant".
    Callers must fail closed (never treat this as "validated OK") but should
    NOT move the version to REJECTED for what is a retriable ops issue --
    see `bundle_version_service.process_prebuilt_component`.
    """


@dataclass(slots=True, frozen=True)
class ComponentValidationResult:
    """The outcome of `validate_component()`.

    `imports`/`exports` are the fully-qualified (`namespace:name/interface`)
    surface actually decoded, kept for logging/telemetry only -- never
    logged with any payload bytes, matching security.md (no secrets/PII;
    these are interface names, not data).
    """

    ok: bool
    reason: str | None = None
    imports: tuple[str, ...] = field(default_factory=tuple)
    exports: tuple[str, ...] = field(default_factory=tuple)


def _wasm_tools_path() -> str:
    path = shutil.which("wasm-tools")
    if path is None:
        raise ComponentValidatorUnavailableError(
            "wasm-tools CLI not found on PATH -- see hub_api/Dockerfile's pinned install"
        )
    return path


def _run_wasm_tools_wit_json(component_bytes: bytes) -> dict[str, Any]:
    """Run `wasm-tools component wit --json` on `component_bytes`; returns the parsed doc.

    Blocking (subprocess) -- callers must wrap in `asyncio.to_thread`
    (`penguin-python-dev` Concurrency Selection).
    """
    wasm_tools = _wasm_tools_path()
    with tempfile.NamedTemporaryFile(suffix=".wasm") as tmp:
        tmp.write(component_bytes)
        tmp.flush()
        try:
            # noqa: S603 -- fixed argv list, no shell=True, no user-controlled
            # executable path (resolved via shutil.which above); nosec B603
            # for bandit's equivalent check.
            proc = subprocess.run(  # noqa: S603 # nosec B603
                [wasm_tools, "component", "wit", "--json", tmp.name],
                capture_output=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ComponentValidatorUnavailableError(
                f"wasm-tools invocation failed: {exc}"
            ) from exc

    if proc.returncode != 0:
        # A non-zero exit here means "not a valid component" (e.g. not a
        # wasm binary at all, or a core module rather than a component) --
        # a genuine validation failure, not an infra problem.
        stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
        return {"_decode_error": stderr}

    try:
        doc: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ComponentValidatorUnavailableError(
            f"wasm-tools produced unparseable JSON: {exc}"
        ) from exc
    return doc


def _namespace(package_name: str) -> str:
    """`"wasi:cli@0.2.3"` -> `"wasi:cli"`; `"waddle:bundle@1.0.0"` -> `"waddle:bundle"`."""
    return package_name.split("@", 1)[0]


def _resolve_interface(doc: dict[str, Any], interface_id: int) -> tuple[str, dict[str, Any]]:
    """`(namespace, interface_record)` for the interface at `doc["interfaces"][interface_id]`."""
    iface = doc["interfaces"][interface_id]
    pkg = doc["packages"][iface["package"]]
    return _namespace(pkg["name"]), iface


def _is_componentize_py_runtime_reflection_export(
    doc: dict[str, Any], interface_id: int, namespace: str, iface: dict[str, Any]
) -> bool:
    """True iff `iface` is exactly componentize-py's synthetic `init` reflection export.

    See module docstring. Every check here is load-bearing -- loosening any
    one (e.g. matching on package/anonymous-name alone, or "symbols is some
    record") would let a hostile component reuse only part of this exact
    disguise to smuggle a different function, a different interface name,
    or a differently-shaped `symbols`/result type through as an export.
    """
    if namespace != _COMPONENTIZE_PY_RUNTIME_EXPORT_PACKAGE or iface.get("name") is not None:
        return False
    functions = iface.get("functions")
    if not isinstance(functions, dict) or set(functions) != {"init"}:
        return False
    init_fn = functions["init"]
    if not isinstance(init_fn, dict) or init_fn.get("kind") != "freestanding":
        return False
    params = init_fn.get("params")
    if not isinstance(params, list) or len(params) != 3:
        return False
    if tuple(p.get("name") for p in params) != _COMPONENTIZE_PY_RUNTIME_INIT_PARAM_NAMES:
        return False
    return (
        params[0].get("type") == "string"
        and params[2].get("type") == "bool"
        and _check_type(doc, params[1].get("type"), _COMPONENTIZE_PY_SYMBOLS_SHAPE, interface_id)
        and _check_type(
            doc, init_fn.get("result"), _COMPONENTIZE_PY_INIT_RESULT_SHAPE, interface_id
        )
    )


def _classify_world_items(
    doc: dict[str, Any], items: dict[str, Any], *, is_export: bool
) -> tuple[list[str], list[str]]:
    """Split a world's `imports`/`exports` map into `(qualified_names, violations)`.

    A violation is either a non-interface item (a bare top-level function
    import/export -- not part of any legitimate `stage`-world component),
    or an interface outside the allowlist -- except componentize-py's
    synthetic reflection export, permitted only on the export side and only
    when it matches `_is_componentize_py_runtime_reflection_export` exactly.
    """
    names: list[str] = []
    violations: list[str] = []
    for item in items.values():
        if not isinstance(item, dict) or "interface" not in item:
            shape = list(item.keys()) if isinstance(item, dict) else repr(item)
            violations.append(f"non_interface_item:{shape}")
            continue
        interface_id = item["interface"]["id"]
        namespace, iface = _resolve_interface(doc, interface_id)
        iface_name = iface.get("name")
        qualified = f"{namespace}/{iface_name if iface_name is not None else '<anonymous>'}"
        names.append(qualified)
        allowed = namespace == WADDLE_BUNDLE_NAMESPACE or namespace in ALLOWED_WASI_NAMESPACES
        if (
            not allowed
            and is_export
            and _is_componentize_py_runtime_reflection_export(doc, interface_id, namespace, iface)
        ):
            allowed = True
        if not allowed:
            violations.append(qualified)
    return names, violations


def _validate_document(doc: dict[str, Any]) -> ComponentValidationResult:
    if "_decode_error" in doc:
        return ComponentValidationResult(
            ok=False, reason=f"not_a_valid_component:{doc['_decode_error']}"
        )

    worlds = doc.get("worlds") or []
    if not worlds:
        return ComponentValidationResult(ok=False, reason="no_world_found")
    world = worlds[0]

    import_names, import_violations = _classify_world_items(
        doc, world.get("imports", {}), is_export=False
    )
    if import_violations:
        return ComponentValidationResult(
            ok=False,
            reason=f"disallowed_import:{sorted(import_violations)}",
            imports=tuple(import_names),
        )

    export_names, export_violations = _classify_world_items(
        doc, world.get("exports", {}), is_export=True
    )
    if export_violations:
        return ComponentValidationResult(
            ok=False,
            reason=f"disallowed_export:{sorted(export_violations)}",
            imports=tuple(import_names),
            exports=tuple(export_names),
        )

    bundle_prefix = f"{WADDLE_BUNDLE_NAMESPACE}/"
    exported_interface_names = {
        name.split("/", 1)[1] for name in export_names if name.startswith(bundle_prefix)
    }
    if exported_interface_names != REQUIRED_EXPORTS:
        missing = REQUIRED_EXPORTS - exported_interface_names
        extra = exported_interface_names - REQUIRED_EXPORTS
        return ComponentValidationResult(
            ok=False,
            reason=f"stage_export_mismatch: missing={sorted(missing)} extra={sorted(extra)}",
            imports=tuple(import_names),
            exports=tuple(export_names),
        )

    return ComponentValidationResult(
        ok=True, imports=tuple(import_names), exports=tuple(export_names)
    )


async def validate_component(component_bytes: bytes) -> ComponentValidationResult:
    """Validate `component_bytes` against the `stage` WIT world's import/export contract.

    Raises `ComponentValidatorUnavailableError` (never a `ComponentValidationResult`)
    when the check itself could not run -- callers must not treat that as
    "validated OK" (see that class's own docstring).
    """
    doc = await asyncio.to_thread(_run_wasm_tools_wit_json, component_bytes)
    return _validate_document(doc)
