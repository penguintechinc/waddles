"""Hand-authored stand-in for `bundle_compiler`'s `generate_entry_wiring()`.

Identical role and shape to `bundles/python/pyping/src/_entry_wiring.py` --
see that module's own docstring for why this file exists and is
hand-authored rather than generated: `generate_entry_wiring()` itself is
still stubbed in `bundle_compiler` (out of scope here), so this bundle --
hand-built rather than routed through the compiler, same as `pyping` -- hand
-authors the file the compiler would otherwise emit from `bundle.yaml`'s
`stages.<s>.entry`.
"""

from __future__ import annotations

from app import dispatch as bundle_dispatch
from app import transform as bundle_transform

__all__ = ["bundle_dispatch", "bundle_transform"]
