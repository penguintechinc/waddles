"""Unit tests for scripts/ci/check-bundle-docker-context.py.

Fixture repos reproduce the alpha deploy run 43812541222 failure (a bundle stage
missing the `#[path]` source it compiles) and prove the gate fails on it, passes
once the COPY lands, and ignores `#[cfg(test)]`-only includes.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).parent.parent / "check-bundle-docker-context.py"


def _load() -> Any:
    """Loads the hyphenated CI script as a module."""
    spec = importlib.util.spec_from_file_location("check_bundle_docker_context", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

BASE = """\
FROM rust:1 AS rust-bundle-base
WORKDIR /repo
COPY wit wit
FROM rust-bundle-base AS bundle-peer
COPY bundles/rust/peer /repo/bundles/rust/peer
FROM rust-bundle-base AS bundle-app
{extra}COPY bundles/rust/app /repo/bundles/rust/app
"""

APP_LIB = """\
#[path = "../../peer/src/shared.rs"]
mod shared;
#[cfg(test)]
mod tests {
    const F: &str = include_str!("../../../../contracts/only-in-tests.json");
    fn brace() { let _ = "}"; }
}
"""

PEER_SHARED = """\
pub fn f() {}
#[cfg(test)]
mod t { const C: &str = include_str!("../contract/x.json"); }
"""


def _repo(tmp: Path, extra: str) -> Path:
    """Builds a two-crate fixture repo; `extra` is inserted into the app stage."""
    for crate in ("app", "peer"):
        d = tmp / "bundles" / "rust" / crate
        (d / "src").mkdir(parents=True)
        (d / "Cargo.toml").write_text(
            f'[package]\nname = "{crate}"\nversion = "0.1.0"\n'
            '[package.metadata.component.target]\npath = "../../../wit"\n'
            '[dev-dependencies]\nx = { path = "../../../nowhere" }\n'
        )
    (tmp / "wit").mkdir()
    (tmp / "bundles/rust/app/src/lib.rs").write_text(APP_LIB)
    (tmp / "bundles/rust/peer/src/shared.rs").write_text(PEER_SHARED)
    (tmp / "bundles/rust/peer/src/lib.rs").write_text("mod shared;\n")
    (tmp / "Dockerfile").write_text(BASE.format(extra=extra))
    return tmp


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    """Runs the checker CLI against a fixture repo."""
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--dockerfile",
            "Dockerfile",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_path_source_fails(tmp_path: Path) -> None:
    """The pre-fix shape (no COPY of the #[path] file) must fail."""
    res = _run(_repo(tmp_path, ""))
    assert res.returncode == 1
    assert (
        "app: bundles/rust/peer/src/shared.rs not COPYed into stage bundle-app"
        in res.stdout
    )
    assert "crates_examined=2" in res.stdout


def test_copied_path_source_passes(tmp_path: Path) -> None:
    """Copying the exact file to its /repo path satisfies the gate; test-only refs ignored."""
    extra = (
        "COPY bundles/rust/peer/src/shared.rs /repo/bundles/rust/peer/src/shared.rs\n"
    )
    res = _run(_repo(tmp_path, extra))
    assert res.returncode == 0, res.stdout
    assert "failing=0" in res.stdout


def test_copy_to_wrong_destination_does_not_count(tmp_path: Path) -> None:
    """A COPY landing somewhere other than /repo/<src> does not cover the reference."""
    extra = "COPY bundles/rust/peer/src/shared.rs /tmp/shared.rs\n"
    assert _run(_repo(tmp_path, extra)).returncode == 1


def test_zero_crates_fails(tmp_path: Path) -> None:
    """No crates examined is a failure, never a silent pass."""
    (tmp_path / "Dockerfile").write_text("FROM rust:1 AS x\n")
    res = _run(tmp_path)
    assert res.returncode == 1
    assert "crates_examined=0" in res.stdout


def test_mask_test_items_blanks_cfg_test_module() -> None:
    """Includes inside #[cfg(test)] items are masked; code after them is kept."""
    masked = mod.mask_test_items(APP_LIB + 'const K: &str = include_str!("k");\n')
    assert "only-in-tests" not in masked
    assert '#[path = "../../peer/src/shared.rs"]' in masked
    assert 'include_str!("k")' in masked
