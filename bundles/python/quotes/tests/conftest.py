"""Test-only `sys.path` wiring for `app`/`_entry_wiring`/`waddle_sdk` imports.

No real componentize-py build or package install needed. Mirrors
`bundles/python/pyping/tests/conftest.py` -- same approach as
`componentize-py componentize -p sdk/waddle-sdk/src -p
bundles/python/quotes/src ...` puts both directories on the guest's module
search path at build time.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BUNDLE_SRC = Path(__file__).resolve().parent.parent / "src"
_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "waddle-sdk" / "src"

for _path in (_BUNDLE_SRC, _SDK_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
