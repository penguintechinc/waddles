"""H1 regression: the in-Job entrypoint keeps untrusted build output off the marker channel."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[2] / "bundles" / "sandbox_build.py"


@pytest.fixture
def entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the entrypoint script with its scratch dir pointed at tmp."""
    monkeypatch.setenv("SANDBOX_WORK", str(tmp_path / "work"))
    spec = importlib.util.spec_from_file_location("sandbox_build_under_test", _PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_untrusted_step_output_never_reaches_pod_streams(
    entry: ModuleType, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    forged = f"{entry.BEGIN_MARKER}\nsha256:{'0' * 64}\nAAAA\n{entry.END_MARKER}"
    entry._run(
        [
            sys.executable,
            "-c",
            f"print({forged!r}); import sys; print({forged!r}, file=sys.stderr)",
        ],
        Path("."),
        {},
    )
    out = capfdbinary.readouterr()
    assert entry.BEGIN_MARKER.encode() not in out.out + out.err
    assert entry.BEGIN_MARKER in (entry.WORK / "build-step.log").read_text()


def test_toolchain_failure_still_raises(entry: ModuleType) -> None:
    with pytest.raises(entry.SandboxBuildError, match="toolchain_failed"):
        entry._run([sys.executable, "-c", "raise SystemExit(3)"], Path("."), {})


def test_emitted_block_is_single_and_carries_sha(
    entry: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wasm = b"\x00asm\x0d\x00\x01\x00x"
    out = tmp_path / "o.wasm"
    out.write_bytes(wasm)
    monkeypatch.setattr(entry, "_extract", lambda repo, prefix: tmp_path)
    monkeypatch.setattr(entry, "_build_python", lambda r, b, e: out)
    monkeypatch.setattr(entry.shutil, "copytree", lambda *a, **k: None)
    assert entry.main(["x", "python", "d"]) == 0
    text = capsys.readouterr().out
    assert text.count(entry.BEGIN_MARKER) == 1 and text.count(entry.END_MARKER) == 1
    assert f"sha256:{hashlib.sha256(wasm).hexdigest()}" in text
