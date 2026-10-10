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

#: Fixtures for the tamper-evident audit-log tests (`tests/audit_support.py`): registered as a
#: plugin so test modules use `audit_dal` / `audit_service` / `gate` by name, without importing
#: fixtures into their own namespace.
pytest_plugins = ["tests.audit_support"]
