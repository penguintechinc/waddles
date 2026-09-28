"""Test-only `sys.path` wiring so `app`/`_entry_wiring` and `waddle_sdk` import
without a real componentize-py build or a package install.

Mirrors `bundles/python/pyping/tests/conftest.py` exactly (same
`componentize-py componentize -p sdk/waddle-sdk/src -p
bundles/python/shoutout/src ...` build-time module search path, exercised
host-side here with no WASM/wasmtime involved).
"""

from __future__ import annotations

import sys
from pathlib import Path

_BUNDLE_SRC = Path(__file__).resolve().parent.parent / "src"
_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "waddle-sdk" / "src"

for _path in (_BUNDLE_SRC, _SDK_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
