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
`stage` world's own export set) -- neither more nor fewer.
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


def _resolve_interface(doc: dict[str, Any], interface_id: int) -> tuple[str, str]:
    """`(namespace, interface_name)` for the interface at `doc["interfaces"][interface_id]`."""
    iface = doc["interfaces"][interface_id]
    pkg = doc["packages"][iface["package"]]
    return _namespace(pkg["name"]), iface["name"]


def _classify_world_items(
    doc: dict[str, Any], items: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Split a world's `imports`/`exports` map into `(qualified_names, violations)`.

    A violation is either a non-interface item (a bare top-level function
    import/export -- not part of any legitimate `stage`-world component)
    or an interface outside the allowlist.
    """
    names: list[str] = []
    violations: list[str] = []
    for item in items.values():
        if not isinstance(item, dict) or "interface" not in item:
            shape = list(item.keys()) if isinstance(item, dict) else repr(item)
            violations.append(f"non_interface_item:{shape}")
            continue
        namespace, iface_name = _resolve_interface(doc, item["interface"]["id"])
        qualified = f"{namespace}/{iface_name}"
        names.append(qualified)
        if namespace != WADDLE_BUNDLE_NAMESPACE and namespace not in ALLOWED_WASI_NAMESPACES:
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

    import_names, import_violations = _classify_world_items(doc, world.get("imports", {}))
    if import_violations:
        return ComponentValidationResult(
            ok=False,
            reason=f"disallowed_import:{sorted(import_violations)}",
            imports=tuple(import_names),
        )

    export_names, export_violations = _classify_world_items(doc, world.get("exports", {}))
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
