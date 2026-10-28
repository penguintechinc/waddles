"""H1 regression: the in-Job entrypoint keeps untrusted build output off the marker channel."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

NONCE = "ab" * 16
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
    monkeypatch.setattr(entry, "_take_nonce", lambda: NONCE)  # real one makes pytest undumpable
    assert entry.main(["x", "python", "d"]) == 0
    text = capsys.readouterr().out
    assert text.count(entry.BEGIN_MARKER) == 1 and text.count(entry.END_MARKER) == 1
    lines = text.split()
    assert lines[1] == f"sha256:{hashlib.sha256(wasm).hexdigest()}"
    assert lines[2] == f"nonce:{NONCE}"  # hub verifies this line


def test_missing_or_malformed_nonce_fails_loud(
    entry: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SANDBOX_BUILD_NONCE", raising=False)
    assert entry.main(["x", "python", "d"]) == 2
    captured = capsys.readouterr()
    assert "nonce_missing" in captured.err and entry.BEGIN_MARKER not in captured.out
    monkeypatch.setenv("SANDBOX_BUILD_NONCE", "short")
    assert entry.main(["x", "python", "d"]) == 2


_PROBE = r"""
import importlib.util, os, subprocess, sys
spec = importlib.util.spec_from_file_location("sb", sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
assert os.environ["SANDBOX_BUILD_NONCE"] == sys.argv[2]  # delivered like the Job env: initial block
assert mod._take_nonce() == sys.argv[2]
assert "SANDBOX_BUILD_NONCE" not in os.environ
assert sys.argv[2] not in repr(mod._base_env())
child = (
    "import os,sys\n"
    "leak=False\n"
    "for f in ('environ','mem'):\n"
    "    try:\n"
    "        leak |= sys.argv[1].encode() in open('/proc/%d/%s' % (os.getppid(), f),'rb').read()\n"
    "    except OSError:\n"
    "        pass\n"
    "print('LEAK' if leak or sys.argv[1] in os.environ.values() else 'SAFE')\n"
)
out = subprocess.run([sys.executable, "-c", child, sys.argv[2]], capture_output=True, text=True)
print(out.stdout.strip(), out.stderr.strip()[:200])
"""


def test_nonce_unreadable_by_untrusted_build_child(tmp_path: Path) -> None:
    """The same-uid build child cannot recover the nonce (env, /proc/<ppid>/environ)."""
    import subprocess

    res = subprocess.run(  # noqa: S603 - fixed argv, test probe
        [sys.executable, "-c", _PROBE, str(_PATH), NONCE],
        capture_output=True,
        text=True,
        check=False,
        env={
            "SANDBOX_WORK": str(tmp_path / "w"),
            "PATH": "/usr/bin:/bin",
            "SANDBOX_BUILD_NONCE": NONCE,
        },
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("SAFE"), res.stdout
