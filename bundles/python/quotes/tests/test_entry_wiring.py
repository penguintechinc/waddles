"""Proves `_entry_wiring.py` wires this bundle's real `transform`/`dispatch`.

`_entry_wiring.py` is the hand-authored `generate_entry_wiring()` stand-in
(see that module's own docstring); this checks it points
`bundle_transform`/`bundle_dispatch` at this bundle's real
`app.transform`/`app.dispatch` -- the exact static-import shape
`waddle_sdk._component_entry` expects.
"""

from __future__ import annotations

import _entry_wiring
import app


def test_entry_wiring_points_at_the_real_transform_and_dispatch() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
