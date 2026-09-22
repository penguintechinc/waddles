"""Re-export of `scripts/db/rbac_matrix.py`, loaded by absolute path.

`scripts/` is a repo-root sibling of `hub_api/`, not a package hub-api
depends on -- loading by `importlib.util.spec_from_file_location`
against this file's own `__file__`-relative path means this module
works whether hub-api is imported as `/app` (the Docker layout) or as
`hub_api.*` from a repo checkout, with no `sys.path` mutation and no
risk of two independent copies of the matrix-parsing logic drifting.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix", _SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load rbac matrix module from {_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["waddles_rbac_matrix"] = module
    spec.loader.exec_module(module)
    return module


_impl = _load()

GrantSpec = _impl.GrantSpec
MatrixError = _impl.MatrixError
ALL_PRIVILEGES = _impl.ALL_PRIVILEGES
DEFAULT_MATRIX_PATH = _impl.DEFAULT_MATRIX_PATH
load_matrix = _impl.load_matrix
matrix_roles = _impl.matrix_roles
matrix_tables = _impl.matrix_tables
render_create_roles_sql = _impl.render_create_roles_sql
render_revoke_public_sql = _impl.render_revoke_public_sql
render_grant_sql = _impl.render_grant_sql
