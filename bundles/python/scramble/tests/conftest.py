"""Test-only `sys.path` wiring -- see `bundles/python/first/tests/conftest.py`'s own docstring."""

from __future__ import annotations

import sys
from pathlib import Path

_BUNDLE_SRC = Path(__file__).resolve().parent.parent / "src"
_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "waddle-sdk" / "src"

for _path in (_BUNDLE_SRC, _SDK_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
