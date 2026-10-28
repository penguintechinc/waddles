#!/usr/local/bin/python3
"""Offline, in-Job entrypoint of the marketplace sandbox builder image (Phase 1, Task 7B).

Runs INSIDE the locked-down K8s Job (rootless, read-only root FS, deny-all egress). Input is a
gzip tarball of ONE bundle directory mounted read-only at `SANDBOX_INPUT` (layout
`bundles/<language>/<dir_name>/...`); output is the componentized `.wasm`, base64-printed to
stdout between fixed markers (the orchestrator reads it back through the pod log, so the Job
needs no volume, credential or network path to hand the artifact over).

Channel trust model: the bundle's own build scripts (build.rs, setup hooks, ...) are UNTRUSTED.
They run with stdout/stderr redirected to a file under the scratch dir -- never the pod log --
and in their own session, which is killed after the step. This entrypoint is the only intended
writer of the marker channel and emits exactly ONE block carrying the sha256 of the bytes it
read plus the hub's per-build nonce; the hub rejects any log with more than one block, a
digest mismatch, or a missing/wrong nonce.

Nonce channel: the hub passes `SANDBOX_BUILD_NONCE` in the Job env. `_take_nonce()` runs first
in `main`: it pops the variable from `os.environ` and marks the process NON-DUMPABLE
(`prctl(PR_SET_DUMPABLE, 0)`). The build runs as the same uid, so without that it could read the
original env block via `/proc/<ppid>/environ` (unsetenv does not scrub it) or `/proc/<ppid>/mem`;
a non-dumpable process's `/proc/<pid>/*` files are root-owned and, with all caps dropped, closed
to its same-uid children. The build subprocess also gets a hermetic env that never has it.

Fail-loud throughout: an unsupported language, an unsafe tar member, missing vendored
dependencies, a failed toolchain step, a missing/ambiguous output or an oversize artifact all
exit non-zero with a one-line `sandbox-build: <reason>` on stderr -- never a silent skip, never
a network fallback (cargo is pinned `--offline`, the sandbox has no route anyway).

Usage: sandbox_build.py <language> <dir_name>
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

BEGIN_MARKER = "WADDLE_WASM_B64_BEGIN"
END_MARKER = "WADDLE_WASM_B64_END"
NONCE_ENV = "SANDBOX_BUILD_NONCE"
PR_SET_DUMPABLE = 4
SUPPORTED_LANGUAGES = ("python", "rust")
BASE_REPO = Path(os.environ.get("SANDBOX_BASE_REPO", "/opt/repo-base"))
WORK = Path(os.environ.get("SANDBOX_WORK", "/work"))
INPUT = Path(os.environ.get("SANDBOX_INPUT", "/input/source.tgz"))
MAX_WASM_BYTES = int(os.environ.get("SANDBOX_MAX_WASM_BYTES", str(8 * 1024 * 1024)))
MAX_EXTRACT_BYTES = int(
    os.environ.get("SANDBOX_MAX_EXTRACT_BYTES", str(64 * 1024 * 1024))
)
COMPONENT_ENTRY_MODULE = "waddle_sdk._component_entry"
RUST_TARGET = "wasm32-wasip2"


class SandboxBuildError(RuntimeError):
    """A build-step problem; the message is the machine-checkable reason."""


def _take_nonce() -> str:
    """Consume the hub's nonce: pop it from the env and make this process non-dumpable.

    Must run before any untrusted code is spawned. Fails loud if the nonce is absent/malformed
    or dumpability cannot be cleared -- never a build whose output the hub could not verify.
    """
    nonce = os.environ.pop(NONCE_ENV, "")
    if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise SandboxBuildError("nonce_missing")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise SandboxBuildError("dumpable_clear_failed")
    return nonce


def _extract(repo: Path, prefix: str) -> Path:
    """Safely extract the tarball into `repo`; every member must live under `prefix`."""
    if not INPUT.is_file():
        raise SandboxBuildError("input_missing")
    total = 0
    with tarfile.open(INPUT, "r:gz") as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()):
                raise SandboxBuildError(f"unsafe_member_type:{member.name}")
            if not (member.name == prefix or member.name.startswith(prefix + "/")):
                raise SandboxBuildError(f"member_outside_bundle:{member.name}")
            total += max(member.size, 0)
            if total > MAX_EXTRACT_BYTES:
                raise SandboxBuildError("extracted_size_exceeded")
        tar.extractall(repo, filter="data")
    bundle = repo / prefix
    if not bundle.is_dir():
        raise SandboxBuildError("bundle_dir_missing")
    return bundle


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    """Run one toolchain step with a minimal environment; non-zero exit fails the build."""
    print(f"sandbox-build: running {cmd[0]}", file=sys.stderr, flush=True)
    log_path = WORK / "build-step.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as sink:
        # Untrusted output goes to a file, never the pod log the hub parses; new session so
        # stray background processes can be killed once the step ends.
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            start_new_session=True,
        )
        returncode = proc.wait()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if returncode != 0:
        raise SandboxBuildError(f"toolchain_failed:{cmd[0]}:{returncode}")


def _base_env() -> dict[str, str]:
    """Hermetic env: writable HOME under the scratch dir, offline flags, reproducibility pins."""
    home = WORK / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "CARGO_HOME": str(home / "cargo"),
        "CARGO_NET_OFFLINE": "true",
        "CARGO_INCREMENTAL": "0",
        "SOURCE_DATE_EPOCH": "1704067200",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
    }
    for key in ("RUSTUP_HOME", "RUSTUP_TOOLCHAIN"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _build_python(repo: Path, bundle: Path, env: dict[str, str]) -> Path:
    """Componentize a Python bundle (deps must already live inside its own `src/`)."""
    import build_python_bundles as bpb  # noqa: PLC0415 - sibling script baked into the image

    src = bundle / "src"
    manifest_path = bundle / "hub-manifest.yaml"
    if not src.is_dir() or not manifest_path.is_file():
        raise SandboxBuildError("python_bundle_layout_invalid")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    world = bpb.required_wit_world(bpb._permission_ids(manifest))
    out = WORK / "out" / "bundle.wasm"
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "componentize-py", "-d", str(repo / "wit" / "waddle-bundle"), "-w", world,
            "componentize", "-p", str(repo / "sdk" / "waddle-sdk" / "src"), "-p", str(src),
            COMPONENT_ENTRY_MODULE, "-o", str(out),
        ],
        repo,
        env,
    )  # fmt: skip
    return out


def _build_rust(repo: Path, bundle: Path, env: dict[str, str]) -> Path:
    """Build a Rust bundle with ONLY vendored crates (`<bundle>/vendor`), `--offline --locked`."""
    vendor = bundle / "vendor"
    if not vendor.is_dir() or not (bundle / "Cargo.lock").is_file():
        raise SandboxBuildError("rust_deps_not_vendored")
    cargo_home = Path(env["CARGO_HOME"])
    cargo_home.mkdir(parents=True, exist_ok=True)
    # Builder-owned source replacement (a repo-supplied .cargo/config.toml is never trusted).
    (cargo_home / "config.toml").write_text(
        '[source.crates-io]\nreplace-with = "vendored-sources"\n'
        f'[source.vendored-sources]\ndirectory = "{vendor}"\n'
        "[net]\noffline = true\n",
        encoding="utf-8",
    )
    env = {
        **env,
        "RUSTFLAGS": f"--remap-path-prefix={repo}=/build -C codegen-units=1 -C strip=symbols",
    }
    _run(
        [
            "cargo",
            "component",
            "build",
            "--release",
            "--target",
            RUST_TARGET,
            "--locked",
            "--offline",
        ],
        bundle,
        env,
    )
    wasms = sorted((bundle / "target" / RUST_TARGET / "release").glob("*.wasm"))
    if len(wasms) != 1:
        raise SandboxBuildError(f"rust_output_count:{len(wasms)}")
    return wasms[0]


def selftest() -> int:
    """`helm test` probe: toolchain present AND the sandbox really has no network route."""
    import socket  # noqa: PLC0415

    for cmd in (
        ["cargo", "--version"],
        ["componentize-py", "--version"],
        ["wasm-tools", "--version"],
    ):
        if subprocess.run(cmd, check=False, capture_output=True).returncode != 0:  # noqa: S603
            print(
                f"sandbox-build: selftest toolchain missing: {cmd[0]}", file=sys.stderr
            )
            return 4
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=5).close()
    except OSError:
        print("sandbox-build: selftest ok (toolchain present, egress denied)")
        return 0
    print(
        "sandbox-build: selftest FAILED: network egress is reachable", file=sys.stderr
    )
    return 5


def main(argv: list[str]) -> int:
    """Build one bundle and print its base64 `.wasm`; returns the process exit code."""
    if argv[1:] == ["--selftest"]:
        return selftest()
    try:
        if len(argv) != 3:
            raise SandboxBuildError("usage")
        nonce = _take_nonce()
        language, dir_name = argv[1], argv[2]
        if language not in SUPPORTED_LANGUAGES:
            raise SandboxBuildError(f"unsupported_language:{language}")
        if "/" in dir_name or dir_name in ("", ".", ".."):
            raise SandboxBuildError("invalid_dir_name")
        repo = WORK / "repo"
        shutil.copytree(BASE_REPO, repo)
        bundle = _extract(repo, f"bundles/{language}/{dir_name}")
        env = _base_env()
        wasm = (_build_python if language == "python" else _build_rust)(
            repo, bundle, env
        )
        data = wasm.read_bytes()
        if not data.startswith(b"\x00asm"):
            raise SandboxBuildError("output_not_wasm")
        if len(data) > MAX_WASM_BYTES:
            raise SandboxBuildError("output_too_large")
    except SandboxBuildError as exc:
        print(f"sandbox-build: {exc}", file=sys.stderr, flush=True)
        return 2
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"sandbox-build: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 3
    print(BEGIN_MARKER, flush=True)
    print(f"sha256:{hashlib.sha256(data).hexdigest()}")
    print(f"nonce:{nonce}")
    encoded = base64.b64encode(data).decode("ascii")
    for i in range(0, len(encoded), 76):
        print(encoded[i : i + 76])
    print(END_MARKER, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
