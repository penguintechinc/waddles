"""Test configuration for ai_interaction_module.

Sets up sys.path so tests can import from the module root (services/,
config.py, app.py) the same way app.py does at runtime -- mirrors
action/interactive/quote_interaction_module/tests/conftest.py.
"""
import os
import sys

# Pre-import the real, pip-installed flask_core (from this venv's
# site-packages) into sys.modules BEFORE app.py's own module-level
# `sys.path.insert(0, '<repo>/libs')` runs -- see
# quote_interaction_module/tests/conftest.py's own docstring for the full
# explanation of the shadowing bug this sidesteps.
import flask_core  # noqa: F401

# Add module root so `from app import app`, `import config`, and
# `from services.* import ...` all work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Env-gated real-Ollama fixtures (skip unless WADDLE_TEST_OLLAMA_URL is set).
# Shared support lives in <repo>/tests/support -- see docs/testing/ollama-realpath.md.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "tests", "support"
    ),
)
from ollama_realpath import (  # noqa: E402,F401
    json_model,
    ollama_url,
    safety_model,
    single_flight,
    text_model,
)
