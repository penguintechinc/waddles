"""Coverage for `src/_entry_wiring.py`'s static re-export shape.

See `src/_entry_wiring.py`'s own module docstring -- a hand-authored stand-in for
`bundle_compiler`'s `generate_entry_wiring()`; this just proves the two names it exports are
in fact `app.dispatch`/`app.transform`.
"""

from __future__ import annotations

import _entry_wiring
import app


def test_bundle_dispatch_is_app_dispatch() -> None:
    assert _entry_wiring.bundle_dispatch is app.dispatch


def test_bundle_transform_is_app_transform() -> None:
    assert _entry_wiring.bundle_transform is app.transform
