"""Gate tests: a bundle whose build inputs change must bump its catalog version.

Regression for dal2alpha deploy run 43154169543 (release sha 1e4d2ee3): a shared wit/
change re-digested 133 Python bundles under unchanged versions -> seeder 409 digest_conflict.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_bundle_version_bump",
    Path(__file__).resolve().parents[1] / "check-bundle-version-bump.py",
)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

BASE = """
bundles:
  - {app_id: a.py, version: "1.0.1", language: python, source_dir: python/a}
  - {app_id: b.rs, version: "1.0.0", language: rust, source_dir: rust/b}
"""


def _head(py: str = "1.0.1", rs: str = "1.0.0") -> str:
    """Build a head catalog with the given versions."""
    return BASE.replace('"1.0.1"', f'"{py}"').replace('"1.0.0"', f'"{rs}"')


def test_shared_wit_change_without_bump_flags_python_only() -> None:
    """wit/ change re-digests every Python bundle but not Rust ones."""
    assert mod.find_unbumped(BASE, _head(), ["wit/waddle-bundle/stage.wit"]) == ["a.py@1.0.1"]


def test_sdk_change_with_bump_passes() -> None:
    """A bumped version clears the gate."""
    assert mod.find_unbumped(BASE, _head(py="1.0.2"), ["sdk/waddle-sdk/src/x.py"]) == []


def test_own_source_change_flags_rust() -> None:
    """A bundle's own source change requires its bump regardless of language."""
    assert mod.find_unbumped(BASE, _head(), ["bundles/rust/b/src/lib.rs"]) == ["b.rs@1.0.0"]


def test_version_file_only_edit_not_flagged() -> None:
    """Editing only version-carrying files never demands another bump."""
    assert mod.find_unbumped(BASE, _head(), ["bundles/python/a/hub-manifest.yaml"]) == []


def test_new_bundle_not_flagged() -> None:
    """Bundles absent from base were never published, so cannot conflict."""
    head = _head() + '  - {app_id: c.py, version: "1.0.0", language: python, source_dir: python/c}\n'
    assert mod.find_unbumped(BASE, head, ["wit/x.wit"]) == ["a.py@1.0.1"]


def test_unrelated_change_passes() -> None:
    """Changes outside any build input pass."""
    assert mod.find_unbumped(BASE, _head(), ["hub_api/app.py", "bundles/python/ab/src/x.py"]) == []


def test_real_catalog_denominator() -> None:
    """The real catalog parses with a meaningful number of bundles."""
    root = Path(__file__).resolve().parents[3]
    n = len(mod._entries((root / mod.CATALOG).read_text()))
    assert n >= 100, f"only {n} bundles examined"
