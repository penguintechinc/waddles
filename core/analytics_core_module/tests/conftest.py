"""Shared fixtures for the analytics-core regression tests.

Puts the module root (``config``, ``services``, ``app``) and the installable
``flask_core`` package on ``sys.path`` -- in the container ``flask_core`` is
pip-installed, here it is imported straight from the repo checkout.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_MODULE_ROOT = Path(__file__).resolve().parent.parent
_FLASK_CORE_PKG_ROOT = _MODULE_ROOT.parent.parent / "libs" / "flask_core"

for _path in (str(_MODULE_ROOT), str(_FLASK_CORE_PKG_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# AAALogger tries to open /var/log/waddlebotlog at import time of ``app``.
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="analytics-core-test-logs-"))
os.environ.setdefault("LOG_LEVEL", "DEBUG")


class RecordCollector(logging.Handler):
    """Collects every emitted log record so tests can inspect all channels."""

    def __init__(self) -> None:
        """Create an empty collector that accepts every level."""
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store the record (formatting happens in `rendered`)."""
        self.records.append(record)


@dataclass(slots=True)
class Capture:
    """An AAALogger wired to a `RecordCollector`, for one test."""

    logger: Any
    collector: RecordCollector

    def rendered(self) -> str:
        """Return everything logged, as every formatter channel would print it.

        Includes the structured line, the plain (``exc_info``-rendering) format
        and the raw record dict, so a leak through any channel is caught.
        """
        from flask_core.logging_config import StructuredFormatter

        structured = StructuredFormatter("analytics-core", "test")
        plain = logging.Formatter()
        parts: list[str] = []
        for record in self.collector.records:
            parts.append(structured.format(record))
            parts.append(plain.format(record))
            parts.append(repr(record.__dict__))
        return "\n".join(parts)

    def error_records(self) -> list[logging.LogRecord]:
        """Return the ERROR-level records."""
        return [r for r in self.collector.records if r.levelno >= logging.ERROR]


@pytest.fixture
def capture() -> Iterator[Capture]:
    """Provide an AAALogger whose output is fully captured for one test."""
    from flask_core.logging_config import setup_aaa_logging

    aaa = setup_aaa_logging("analytics-core", "test", log_level="DEBUG")
    collector = RecordCollector()
    aaa.logger.addHandler(collector)
    try:
        yield Capture(logger=aaa, collector=collector)
    finally:
        aaa.logger.removeHandler(collector)
