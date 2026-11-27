"""Gate tests: Cargo.toml package versions must match their Cargo.lock entries.

Regression for the alpha "Build seeder" failure after 9e2f8eba3 (reputation-accrual
Cargo.toml 1.0.3 vs Cargo.lock 1.0.2 -> `--locked` refused to build).
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "check_bundle_lockfiles", REPO / "scripts/ci/check-bundle-lockfiles.py"
)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _crate(dir_: Path, name: str, version: str, deps: str = "") -> None:
    """Writes a minimal Cargo.toml for `name`@`version` with optional dependency lines."""
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "{version}"\nedition = "2024"\n'
        f"\n[dependencies]\n{deps}"
    )


def _lock(dir_: Path, *entries: tuple[str, str, bool]) -> None:
    """Writes a Cargo.lock with (name, version, from_registry) package entries."""
    body = "version = 4\n"
    for name, version, registry in entries:
        body += f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
        if registry:
            body += 'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
    (dir_ / "Cargo.lock").write_text(body)


def test_matching_bundle_passes(tmp_path: Path, capsys) -> None:
    """A lock whose root entry matches Cargo.toml passes and reports its denominator."""
    b = tmp_path / "bundles/rust/ok"
    _crate(b, "waddle-bundle-ok", "1.0.3", 'serde = "1"\n')
    _lock(b, ("serde", "1.0.0", True), ("waddle-bundle-ok", "1.0.3", False))
    assert mod.main(["--repo-root", str(tmp_path)]) == 0
    assert (
        "1 lockfile(s) examined, 1 local package(s) compared, 0 mismatched"
        in capsys.readouterr().out
    )


def test_root_version_drift_fails(tmp_path: Path, capsys) -> None:
    """The 9e2f8eba3 shape: Cargo.toml bumped, Cargo.lock root entry stale -> fail."""
    b = tmp_path / "bundles/rust/reputation-accrual"
    _crate(b, "waddle-bundle-reputation-accrual", "1.0.3")
    _lock(b, ("waddle-bundle-reputation-accrual", "1.0.2", False))
    assert mod.main(["--repo-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "Cargo.toml=1.0.3" in captured.err and "Cargo.lock=1.0.2" in captured.err
    assert "1 mismatched" in captured.out


def test_registry_entry_with_same_name_does_not_mask_drift(tmp_path: Path) -> None:
    """Only source-less (local) entries count; a registry crate of the same name is not the root."""
    b = tmp_path / "core/foo"
    _crate(b, "foo", "2.0.0")
    _lock(b, ("foo", "2.0.0", True), ("foo", "1.9.0", False))
    assert mod.main(["--repo-root", str(tmp_path)]) == 1


def test_path_dependency_drift_fails(tmp_path: Path) -> None:
    """A bumped local path dependency whose lock entry is stale also breaks --locked."""
    sdk = tmp_path / "sdk/waddle-sdk-rs"
    _crate(sdk, "waddle-sdk", "0.4.0")
    _lock(sdk, ("waddle-sdk", "0.4.0", False))
    b = tmp_path / "bundles/rust/ping"
    _crate(
        b,
        "waddle-bundle-ping",
        "1.0.0",
        'waddle-sdk = { path = "../../../sdk/waddle-sdk-rs" }\n',
    )
    _lock(b, ("waddle-bundle-ping", "1.0.0", False), ("waddle-sdk", "0.3.0", False))
    assert mod.main(["--repo-root", str(tmp_path)]) == 1


def test_missing_root_entry_fails(tmp_path: Path) -> None:
    """A lockfile with no entry for its own crate is a mismatch, never a silent pass."""
    b = tmp_path / "bundles/rust/x"
    _crate(b, "x", "1.0.0")
    _lock(b, ("serde", "1.0.0", True))
    assert mod.main(["--repo-root", str(tmp_path)]) == 1


def test_zero_lockfiles_examined_fails(tmp_path: Path, capsys) -> None:
    """A scanner pointed at an empty root must fail, not report clean."""
    assert mod.main(["--repo-root", str(tmp_path)]) == 1
    assert "0 Cargo.lock files examined" in capsys.readouterr().err


def test_shell_entrypoint_fails_on_mismatch(tmp_path: Path) -> None:
    """The .sh entrypoint propagates the failing exit status (no masking)."""
    b = tmp_path / "bundles/rust/bad"
    _crate(b, "bad", "1.0.1")
    _lock(b, ("bad", "1.0.0", False))
    rc = subprocess.run(
        ["bash", str(REPO / "scripts/ci/check-bundle-lockfiles.sh"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    ).returncode
    assert rc == 1


def test_real_tree_is_consistent(capsys) -> None:
    """Every in-repo lockfile agrees with its manifests (and the scan found real files)."""
    assert mod.main(["--repo-root", str(REPO)]) == 0
    out = capsys.readouterr().out
    examined = int(out.split("check-bundle-lockfiles: ")[1].split(" ")[0])
    assert examined >= 27


def test_inactive_optional_dep_of_dependency_is_not_required(tmp_path: Path) -> None:
    """cargo omits a dependency crate's un-activated optional deps from the lock; not a mismatch."""
    opt = tmp_path / "core/hub_client"
    _crate(opt, "hub_client", "0.1.0")
    _lock(opt, ("hub_client", "0.1.0", False))
    mid = tmp_path / "core/crypto"
    _crate(
        mid,
        "crypto",
        "0.1.0",
        'hub_client = { path = "../hub_client", optional = true }\n',
    )
    _lock(mid, ("crypto", "0.1.0", False), ("hub_client", "0.1.0", False))
    top = tmp_path / "core/storage"
    _crate(top, "storage", "0.1.0", 'crypto = { path = "../crypto" }\n')
    _lock(top, ("storage", "0.1.0", False), ("crypto", "0.1.0", False))
    assert mod.main(["--repo-root", str(tmp_path)]) == 0
