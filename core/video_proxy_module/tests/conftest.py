"""Pytest bootstrap for video_proxy_module's tests.

video_proxy_module isn't installed as a package (standalone Dockerfile, run
via `hypercorn app:app` -- same shape as `core/svc_action`/`core/
workflow_core_module`) -- so its own directory has to be put on sys.path
explicitly for `from config import Config` / `import app` / `from
services...` to resolve. Sets `RELEASE_MODE`-adjacent env vars needed for
`config.py`'s module-level `Config()` construction to succeed without a
real Postgres/S3-compatible object store/license server reachable in the
test sandbox.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MODULE_SECRET_KEY", "test-module-secret-key")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite:memory")
