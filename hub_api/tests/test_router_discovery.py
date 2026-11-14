"""Blueprint auto-discovery -- the v1/v2 port extension point (`routers/_discovery.py`).

Proves the mechanism the parallel controller-port wave depends on: a
port agent drops exactly one `blueprints/v{1,2}/<group>.py` module
exposing `BLUEPRINTS`, and NEVER touches `routers/v1.py`, `routers/v2.py`,
or `blueprints/__init__.py` -- removing what was previously a guaranteed
collision point for ~10 agents editing the same two files in parallel.

Two levels of proof:

- `TestDiscoverBlueprintsHelper` -- the generic `discover_blueprints()`
  function against a hermetic, `tmp_path`-built synthetic package (no
  writes to the real source tree; parallel-test-safe).
- `TestRealPackageDiscovery` -- the real thing: a stub module is
  physically written into `hub_api/blueprints/v2/` at test time (exactly
  what a port agent's PR would add), `register_v2()` is called against
  the REAL `blueprints.v2` package, and the newly-dropped route is
  asserted reachable -- then the file is removed and its module entry
  evicted from `sys.modules` in a `finally`, so the tree is byte-for-byte
  unchanged after the test regardless of pass/fail.

Fail-first proof (executed, not narrated): temporarily made
`discover_blueprints` `continue` (skip) on EVERY module, whether or not
it had `BLUEPRINTS`, rather than only those missing it --
`test_finds_module_with_blueprints_and_skips_module_without`,
`test_register_v1_mounts_the_real_auth_group`,
`test_register_v2_mounts_the_real_platform_group`, and
`test_newly_dropped_v2_module_is_auto_registered` all went red (0
blueprints found / 404 instead of finding `auth`/`platform`/the dropped
probe module); reverted, all green again.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from quart import Quart
from quart_schema import QuartSchema

import blueprints.v2 as v2_package
from routers._discovery import discover_blueprints
from routers.v1 import register_v1
from routers.v2 import register_v2


class TestDiscoverBlueprintsHelper:
    """Hermetic proof of the generic mechanism -- no writes to the real tree."""

    async def test_finds_module_with_blueprints_and_skips_module_without(
        self, tmp_path: Path
    ) -> None:
        pkg_dir = tmp_path / "probe_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "with_blueprints.py").write_text(
            "from quart import Blueprint\n\n"
            "probe_bp = Blueprint('probe', __name__, url_prefix='/probe')\n\n"
            "@probe_bp.route('/ping')\n"
            "async def ping():\n"
            "    return {'ok': True}\n\n"
            "BLUEPRINTS = [probe_bp]\n"
        )
        (pkg_dir / "without_blueprints.py").write_text(
            '"""A helper module with no routes -- must be skipped, not an error."""\n'
        )

        sys.path.insert(0, str(tmp_path))
        try:
            importlib.invalidate_caches()
            probe_pkg = importlib.import_module("probe_pkg")
            found = discover_blueprints(probe_pkg)
        finally:
            sys.path.remove(str(tmp_path))
            for name in ("probe_pkg", "probe_pkg.with_blueprints", "probe_pkg.without_blueprints"):
                sys.modules.pop(name, None)

        assert len(found) == 1
        assert found[0].name == "probe"

    async def test_package_with_no_matching_modules_returns_empty(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "empty_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "helper_only.py").write_text('"""No BLUEPRINTS here either."""\n')

        sys.path.insert(0, str(tmp_path))
        try:
            importlib.invalidate_caches()
            empty_pkg = importlib.import_module("empty_pkg")
            found = discover_blueprints(empty_pkg)
        finally:
            sys.path.remove(str(tmp_path))
            for name in ("empty_pkg", "empty_pkg.helper_only"):
                sys.modules.pop(name, None)

        assert found == []


class TestRealPackageDiscovery:
    """`register_v1`/`register_v2` against the REAL `blueprints.v1`/`blueprints.v2` packages."""

    async def test_register_v1_mounts_the_real_auth_group(self) -> None:
        app = Quart(__name__)
        QuartSchema(app)  # required: /login's @validate_request reads QUART_SCHEMA_CONVERT_CASING
        register_v1(app)
        response = await app.test_client().post("/api/v1/auth/login", json={})
        # blueprints/v1/auth.py's real M1 port, found by discovery -- 400 (quart-schema
        # @validate_request rejects the empty body), not 404. Was 501 (the pre-M1 stub).
        assert response.status_code == 400

    async def test_register_v1_mounts_bundle_admin_gateway(self) -> None:
        """The webui bundle-settings page depends on the gateway being live, not just defined."""
        app = Quart(__name__)
        QuartSchema(app)
        register_v1(app)
        rules = {
            (rule.rule, method)
            for rule in app.url_map.iter_rules()
            for method in rule.methods or ()
        }
        prefix = "/api/v1/apps/community/<int:community_id>/bundles/<app_id>"
        for path, method in [
            ("/admin", "GET"),
            ("/config", "GET"),
            ("/config", "PUT"),
            ("/config/history", "GET"),
        ]:
            assert (prefix + path, method) in rules, f"{method} {path} not mounted"

    async def test_register_v2_mounts_the_real_platform_group(self, tenant_db: Any) -> None:
        app = Quart(__name__)
        app.config["dal"] = tenant_db
        register_v2(app)
        response = await app.test_client().get("/api/v2/core/platform/default/status")
        assert response.status_code == 401  # reached tenant_middleware -- not a 404

    async def test_newly_dropped_v2_module_is_auto_registered(self) -> None:
        """The literal port-agent workflow: drop a file, change nothing else, it's live.

        Writes a real module into the real `blueprints/v2/` package
        directory (mirroring exactly what a port PR adds), calls
        `register_v2()` against a fresh app with no code changes anywhere
        else, and asserts the new route answers. Removed in `finally`
        regardless of outcome -- this test never leaves a trace in git.
        """
        probe_path = Path(v2_package.__path__[0]) / "_discovery_probe_test_only.py"
        assert not probe_path.exists(), "stale probe file from a previous failed run"
        probe_path.write_text(
            "from quart import Blueprint\n\n"
            "probe_bp = Blueprint('discovery_probe', __name__, url_prefix='/api/v2/_probe')\n\n"
            "@probe_bp.route('/ping')\n"
            "async def ping():\n"
            "    return {'ok': True}\n\n"
            "BLUEPRINTS = [probe_bp]\n"
        )
        module_name = "blueprints.v2._discovery_probe_test_only"
        try:
            importlib.invalidate_caches()
            app = Quart(__name__)
            register_v2(app)
            response = await app.test_client().get("/api/v2/_probe/ping")
            assert response.status_code == 200
            body = await response.get_json()
            assert body == {"ok": True}
        finally:
            probe_path.unlink(missing_ok=True)
            sys.modules.pop(module_name, None)
            importlib.invalidate_caches()
