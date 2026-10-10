"""Bootstrap for the shared real-Ollama test support (`<repo>/tests/support/ollama_realpath.py`).

hub_api's tests are a package rooted at `hub_api/`, so the repo-level support
directory is not importable by default; this shim puts it on `sys.path` once and
re-exports the fixtures/helpers the real-endpoint test modules use.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SUPPORT_DIR = Path(__file__).resolve().parents[2] / "tests" / "support"
if str(_SUPPORT_DIR) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_DIR))

from ollama_realpath import (  # noqa: E402
    OLLAMA_URL_ENV,
    RecordedRequest,
    SingleFlightGuard,
    SingleFlightViolation,
    json_model,
    ollama_url,
    ollama_url_or_none,
    safety_model,
    single_flight,
    split_endpoint,
    text_model,
)

__all__ = [
    "OLLAMA_URL_ENV",
    "RecordedRequest",
    "SingleFlightGuard",
    "SingleFlightViolation",
    "json_model",
    "ollama_url",
    "ollama_url_or_none",
    "safety_model",
    "single_flight",
    "split_endpoint",
    "text_model",
]
