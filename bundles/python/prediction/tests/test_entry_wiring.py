"""The hand-authored entry wiring re-exports the real stage handlers."""

from __future__ import annotations

import _entry_wiring
import app


def test_wiring_exports_app_handlers() -> None:
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.bundle_transform is app.transform
