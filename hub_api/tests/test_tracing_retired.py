"""`flask_core.tracing` (TracingManager) is retired; nothing may re-introduce its anti-patterns.

The old manager read the non-standard `OTLP_ENDPOINT` / `TRACING_*` variables and hardcoded an
insecure OTLP channel. Telemetry destinations come only from the standard `OTEL_*` env (see
`hub_api/telemetry.py`). These tests prove the module and its exports are gone and scan the
whole repo (counting the files examined; zero examined is a failure) for the two patterns.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

import flask_core
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".venv", "node_modules", "target", ".git", ".worktrees", ".claude", "__pycache__"}
NON_STANDARD_ENDPOINT = re.compile(r"\bOTLP_ENDPOINT\b")
HARDCODED_INSECURE = re.compile(r"insecure\s*=\s*True")
SCANNED_SUFFIXES = {".py", ".yaml", ".yml", ".tpl", ".env"}


def _scannable_files() -> list[Path]:
    """Non-test source/config files under the repo root (test files may name the patterns)."""
    out: list[Path] = []
    for root, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in {"tests", "test"}]
        for name in names:
            if name.startswith("test_"):
                continue
            if Path(name).suffix in SCANNED_SUFFIXES or name == "Dockerfile":
                out.append(Path(root) / name)
    return out


def test_tracing_module_and_exports_are_gone() -> None:
    assert flask_core.__file__ and flask_core.__all__, "must be the real flask_core, not a shim"
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("flask_core.tracing")
    for name in ("TracingManager", "create_tracing_manager", "init_tracing", "get_tracing_manager"):
        assert not hasattr(flask_core, name), name
        assert name not in flask_core.__all__, name


def test_no_component_reads_nonstandard_otlp_endpoint_or_hardcodes_insecure() -> None:
    files = _scannable_files()
    print(f"RETIRED-TRACING-SCAN examined {len(files)} files")
    assert len(files) > 100, "scanner is pointed at the wrong root (zero/low denominator)"
    offenders: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in (NON_STANDARD_ENDPOINT, HARDCODED_INSECURE):
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {match.group(0)}")
    assert not offenders, offenders
