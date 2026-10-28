"""Hand-authored stand-in for `bundle_compiler`'s `generate_entry_wiring()`.

See `bundles/python/pyping/src/_entry_wiring.py`'s own module docstring for the full
rationale -- identical shape, this bundle's own `app` module.
"""

from __future__ import annotations

from app import dispatch as bundle_dispatch
from app import transform as bundle_transform

__all__ = ["bundle_dispatch", "bundle_transform"]
