"""svc-streaming verifies platform JWTs with public keys read from `platform_vault.public_keys`.

Secrets T10: the service connects as the role its Helm template names (`waddles_svc_streaming`), so
that exact role must be granted SELECT on the `public_keys` VIEW by migration 0109 (and nothing on
the sealed tables), and it must exist in `config/postgres/service-roles.yaml`. Static checks over
the three sources of truth -- no database; the live grant matrix is in `test_0109_platform_vault`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "alembic" / "versions" / "0109_platform_vault.py"
TEMPLATE = REPO / "k8s" / "helm" / "waddlebot" / "templates" / "svc-streaming.yaml"
SERVICE_ROLES = REPO / "config" / "postgres" / "service-roles.yaml"


def _tuple_constant(source: str, name: str) -> tuple[str, ...]:
    """Value of a module-level tuple-of-strings constant, read without importing alembic."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found")


def test_pod_role_is_a_public_key_reader() -> None:
    template = TEMPLATE.read_text()
    match = re.search(r'"waddlebot\.dbEnv" \(dict "root" \. "role" "([a-z_]+)"\)', template)
    assert match, "svc-streaming.yaml no longer names its database role"
    role = match.group(1)
    readers = _tuple_constant(MIGRATION.read_text(), "PUBLIC_KEY_READERS")
    assert role in readers, f"{role} is not granted SELECT on platform_vault.public_keys"
    assert len(readers) >= 5  # denominator: ingest, process, action, streaming, presentation


def test_role_exists_and_holds_no_direct_vault_table_grant() -> None:
    roles = yaml.safe_load(SERVICE_ROLES.read_text())["roles"]
    tables = set(roles["waddles_svc_streaming"].get("tables", {}))
    assert tables, "role has no tables: wrong yaml shape"
    assert not any(t.startswith("platform_vault.") for t in tables)
    assert {"secrets", "secret_events", "root_marker"}.isdisjoint(tables)
