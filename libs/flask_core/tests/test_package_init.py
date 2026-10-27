"""Tests for the flask_core package ``__init__`` (public API surface) and ``setup.py``.

conftest installs a lightweight stub ``flask_core`` package so leaf-module tests do not
pay for the full import graph.  These tests deliberately do the opposite: they import the
REAL package ``__init__`` in a clean module namespace (restoring the stub afterwards) so
the full public API is proven importable and consistent with ``__all__``.
"""

import importlib
import runpy
import sys
import types
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def real_flask_core():
    """Import the real ``flask_core`` package fresh, then put the original modules back."""
    saved = {n: m for n, m in sys.modules.items() if n == "flask_core" or n.startswith("flask_core.")}
    for name in saved:
        del sys.modules[name]
    try:
        yield importlib.import_module("flask_core")
    finally:
        for name in [n for n in sys.modules if n == "flask_core" or n.startswith("flask_core.")]:
            del sys.modules[name]
        sys.modules.update(saved)


class TestPackageInit:
    def test_version_matches_platform_version(self, real_flask_core):
        assert real_flask_core.__version__
        assert real_flask_core.__version__ == real_flask_core.get_platform_version()

    def test_every_name_in_all_is_resolvable(self, real_flask_core):
        exported = getattr(real_flask_core, "__all__", None)
        assert exported, "flask_core must declare a non-empty __all__"
        missing = [n for n in exported if not hasattr(real_flask_core, n)]
        assert missing == []

    def test_core_entrypoints_are_the_real_callables(self, real_flask_core):
        from flask_core import auth, tenancy

        for name in ("create_jwt_token", "verify_jwt_token", "require_scope", "AsyncDAL", "tenant_middleware"):
            assert callable(getattr(real_flask_core, name)), name
        assert real_flask_core.create_jwt_token is auth.create_jwt_token
        assert real_flask_core.tenant_middleware is tenancy.tenant_middleware


class TestSetupPy:
    def test_setup_reads_abstract_requirements(self, monkeypatch):
        # Hermetic stand-in so the test does not depend on setuptools being installed in the
        # venv (3.13 venvs ship without it); setup.py's own file parsing still runs for real.
        captured: dict = {}
        fake = types.ModuleType("setuptools")
        fake.setup = lambda **kw: captured.update(kw)
        fake.find_packages = lambda: ["flask_core", "flask_core.models"]
        monkeypatch.setitem(sys.modules, "setuptools", fake)
        monkeypatch.chdir(PKG_ROOT)
        runpy.run_path(str(PKG_ROOT / "setup.py"), run_name="__main__")

        assert captured["name"] == "waddlebot-flask-core"
        assert captured["python_requires"] == ">=3.12"
        reqs = captured["install_requires"]
        assert reqs, "install_requires must not be empty"
        # abstract PEP 508 specifiers only -- never the hash-annotated lock lines
        assert all("--hash" not in r and not r.startswith("#") and r == r.strip() for r in reqs)
        assert any(r.lower().startswith("quart") for r in reqs)
        assert "flask_core" in captured["packages"]
        assert "pytest-asyncio>=0.23.0" in captured["extras_require"]["dev"]
