"""Shared fixtures for waddle-sdk's pure-Python test suite.

No WASM/wasmtime here -- every test in this package runs entirely host-side
against a fake ``wit_world`` module (see ``tests/wit_fakes.py``) built to the
exact shapes ``componentize-py bindings`` generates from the committed
``wit/waddle-bundle/stage.wit`` (verified directly: ``componentize-py==0.25.1``
was run against that file during development of this SDK to confirm the
binding shapes below, rather than guessed).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_bundle_runtime_state():
    """Ensure no bundle_runtime module-level state leaks between tests."""
    yield
    from waddle_sdk.flask_core.bundle_runtime import reset_bundle_dal_for_tests

    reset_bundle_dal_for_tests()
