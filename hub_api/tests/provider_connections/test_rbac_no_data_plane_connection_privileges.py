"""No data-plane role may hold table privileges on the connection-secret tables.

Decrypted connection secrets are only ever reachable through hub-api's scope-gated, community-bound
routes. If a data-plane DB role (svc_ingest, svc_process, svc_action, ...) gained any privilege on
``platform_connections`` it could read the sealed ciphertext (and, with the shared key, the
secrets) around that gate. Only ``hub_api`` (runtime) and ``migration_runner`` (DDL) may hold any.
The assertion counts rows examined: a matrix that lost the table, or an empty one, is a FAILURE,
never a pass (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

from services.rbac_matrix import load_matrix

_SECRET_TABLES = ("platform_connections", "platform_integrations")
_ALLOWED_HOLDERS = {"hub_api", "migration_runner"}


def test_no_data_plane_role_has_table_privileges_on_connection_secret_tables() -> None:
    rows = load_matrix()
    roles_checked = 0
    for row in rows:
        if row.table not in _SECRET_TABLES or row.role in _ALLOWED_HOLDERS:
            continue
        roles_checked += 1
        assert not row.privileges, f"{row.role} must hold no privileges on {row.table}"
    assert roles_checked > 0, "no data-plane role rows examined -- the matrix lost the table"


def test_hub_api_still_owns_platform_connections() -> None:
    owners = {r.role for r in load_matrix() if r.table == "platform_connections" and r.privileges}
    assert owners == _ALLOWED_HOLDERS, owners
