"""Root pytest bootstrap -- ensures `hub_api/` itself is importable.

`app.py`, `config.py`, `blueprints/`, `routers/`, and `openapi/` are all
top-level modules/packages relative to this directory (matching how the
Dockerfile's runtime stage lays them out under `/app` -- see `Dockerfile`).
Pytest's own rootdir-insertion normally handles this, but a root
conftest.py makes it explicit and independent of whether `tests/` carries
an `__init__.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HUB_API_DIR = Path(__file__).resolve().parent
if str(_HUB_API_DIR) not in sys.path:
    sys.path.insert(0, str(_HUB_API_DIR))

# In-tree shared libs (installed as packages in the image; here resolved from the repo so the
# suite imports them without a prior `pip install -e`). Each entry is a package-parent dir.
for _lib in ("flask_core", "waddle_transports"):
    _lib_dir = _HUB_API_DIR.parent / "libs" / _lib
    if not (_lib_dir / _lib).is_dir():
        raise RuntimeError(f"in-tree lib missing: {_lib_dir / _lib}")
    if str(_lib_dir) not in sys.path:
        sys.path.append(str(_lib_dir))

#: Fixtures for the tamper-evident audit-log tests (`tests/audit_support.py`): registered as a
#: plugin so test modules use `audit_dal` / `audit_service` / `gate` by name, without importing
#: fixtures into their own namespace.
pytest_plugins = ["tests.audit_support"]
