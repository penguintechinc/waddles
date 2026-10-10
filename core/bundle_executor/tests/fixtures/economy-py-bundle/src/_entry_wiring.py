"""Hand-authored stand-in for `bundle_compiler`'s `generate_entry_wiring()`.

Same shape as `bundles/python/eightball/src/_entry_wiring.py`, this bundle's
own `app` module.
"""

from __future__ import annotations

from app import dispatch as bundle_dispatch
from app import transform as bundle_transform

__all__ = ["bundle_dispatch", "bundle_transform"]
