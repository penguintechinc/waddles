"""Hand-authored stand-in for `bundle_compiler`'s `generate_entry_wiring()`.

Same role, same shape as `bundles/python/pyping/src/_entry_wiring.py` (see
that file's own docstring for the full rationale): `waddle_sdk.
_component_entry` imports this exact module name -- `_entry_wiring` --
from the bundle's own source directory, expecting static
`bundle_transform`/`bundle_dispatch` attributes. `generate_entry_wiring()`
itself is still stubbed in `bundle_compiler`, so this hand-built bundle
(not routed through the compiler, same as `pyping`) hand-authors the file
the compiler would otherwise emit from `bundle.yaml`'s `stages.<s>.entry`.
"""

from __future__ import annotations

from app import dispatch as bundle_dispatch
from app import transform as bundle_transform

__all__ = ["bundle_dispatch", "bundle_transform"]
