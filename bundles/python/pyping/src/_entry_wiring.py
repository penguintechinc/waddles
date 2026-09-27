"""Hand-authored stand-in for `bundle_compiler`'s `generate_entry_wiring()`.

`waddle_sdk._component_entry` (the shared SDK entry module every Python
bundle points `componentize-py componentize` at, per its own module
docstring) imports this exact module name -- `_entry_wiring` -- from the
bundle's own source directory, expecting static `bundle_transform`/
`bundle_dispatch` attributes. Normally the compiler generates this file from
`bundle.yaml`'s `stages.<s>.entry` at build time; `generate_entry_wiring()`
itself is still stubbed (out of scope here, same as `pyping`'s own
`app.py` module docstring notes), so this bundle -- hand-built rather than
routed through the compiler, the same way `bundles/rust/ping` is hand-built
with `cargo-component` -- hand-authors the file the compiler would otherwise
emit, in exactly the shape `_component_entry.py` expects: a static
`from {module} import {function} as bundle_{transform,dispatch}` import, not
a runtime-environment-driven `importlib.import_module` (the WIT world
excludes `wasi:cli/environment`, spec Sec6.5).
"""

from __future__ import annotations

from app import dispatch as bundle_dispatch
from app import transform as bundle_transform

__all__ = ["bundle_dispatch", "bundle_transform"]
