"""Test-only `sys.path` wiring -- see `bundles/python/pyping/tests/conftest.py`'s own docstring."""

from __future__ import annotations

import sys
from pathlib import Path

_BUNDLE_SRC = Path(__file__).resolve().parent.parent / "src"
_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "waddle-sdk" / "src"

for _path in (_BUNDLE_SRC, _SDK_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


import json as _json
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from waddle_sdk.testing import FakeAiHost, install_fake_ai_host


@dataclass(slots=True)
class MockHost:
    """Mocked WIT host for `ask`: the SDK's `FakeAiHost` plus relay/log recorders."""

    ai: FakeAiHost
    relays: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    logs: list[tuple[int, str, str]] = field(default_factory=list)

    def log_text(self) -> str:
        """Every recorded log line (message + fields JSON) joined, for PII assertions."""
        return "\n".join(f"{m} {f}" for _l, m, f in self.logs)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> MockHost:
    """Install a mocked `wit_world` (ai + relay + log); the real Ollama is NOT involved."""
    fake_ai = install_fake_ai_host(monkeypatch)
    mock = MockHost(ai=fake_ai)
    import wit_world

    wit_world.imports.relay = types.SimpleNamespace(
        push=lambda p, m: mock.relays.append((p, _json.loads(m)))
    )
    wit_world.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: mock.logs.append((lvl, msg, fields)),
    )
    return mock
