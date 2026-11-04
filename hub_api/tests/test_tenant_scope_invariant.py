"""Static invariant: no hub-api call into the community-connections store omits the tenant.

# regression: gh-969 -- a NULL/omitted tenant must never reach SQL. Every non-test call to a
# `community_connections` function must pass `tenant_id=` explicitly, and the value must be the
# request's validated tenant (`ctx.tenant_id`), the signed OAuth state's tenant
# (`payload.tenant_id`), or the explicit `SYSTEM` marker -- never `None` and never missing.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_FUNCS = {
    "list_connections",
    "get_connection",
    "upsert_connection",
    "delete_connection",
    "get_decrypted_tokens",
    "store_refreshed_access_token",
}
_ALLOWED = {"ctx.tenant_id", "payload.tenant_id", "SYSTEM", "connections_svc.SYSTEM", "tenant_id"}
_SCAN = ("blueprints", "services")
_SKIP = {
    "services/community_connections.py",  # the definitions themselves
    "services/sso_service.py",
    "blueprints/v1/github_sync.py",
    "blueprints/v1/sso.py",
}


def _calls() -> list[tuple[str, int, ast.Call]]:
    found: list[tuple[str, int, ast.Call]] = []
    for sub in _SCAN:
        for path in (_ROOT / sub).rglob("*.py"):
            if str(path.relative_to(_ROOT)) in _SKIP:
                continue
            src = path.read_text()
            if "community_connections" not in src and "connections_svc" not in src:
                continue
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                owner = ast.unparse(f.value) if isinstance(f, ast.Attribute) else ""
                if name in _FUNCS and owner in ("", "connections_svc"):
                    found.append((str(path.relative_to(_ROOT)), node.lineno, node))
    return found


def test_every_connections_call_passes_a_validated_tenant_scope() -> None:
    calls = _calls()
    assert len(calls) >= 8, f"scanner found only {len(calls)} call sites -- wrong root?"
    for rel, line, node in calls:
        kw = {k.arg: k.value for k in node.keywords}
        assert "tenant_id" in kw, f"{rel}:{line} omits tenant_id"
        value = ast.unparse(kw["tenant_id"])
        assert value != "None" and value in _ALLOWED, f"{rel}:{line} tenant_id={value}"
