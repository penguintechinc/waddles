"""Pytest bootstrap for ai_researcher_module's tests.

ai_researcher_module isn't installed as a package (standalone Dockerfile,
run via `hypercorn app:app`) -- so its own directory has to be put on
sys.path explicitly for `import app` / `from config import Config` to
resolve.

`app.py` inserts `<repo_root>/libs` onto `sys.path` itself (for its own
`from flask_core import (...)` line) -- but `<repo_root>/libs/flask_core`
is a directory containing the REAL `flask_core` package one level deeper
(`<repo_root>/libs/flask_core/flask_core/`), not the package itself, so
that sys.path entry resolves `flask_core` as an empty implicit namespace
package instead, shadowing the properly pip-installed one and breaking
every name import from it. Importing `flask_core` here FIRST caches the
correctly-resolved module in `sys.modules` before `app.py` ever runs its
own `sys.path.insert` -- Python's import system checks `sys.modules`
before re-searching `sys.path`, so `app.py`'s later `from flask_core
import (...)` reuses this same, correct module instead of re-resolving it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-ai-researcher-module-tests")

import flask_core  # noqa: E402,F401

# Env-gated real-Ollama fixtures (skip unless WADDLE_TEST_OLLAMA_URL is set).
# Shared support lives in <repo>/tests/support -- see docs/testing/ollama-realpath.md.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "tests", "support"
    ),
)
from ollama_realpath import (  # noqa: E402,F401
    json_model,
    ollama_url,
    safety_model,
    single_flight,
    text_model,
)
