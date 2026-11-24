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
    assert mod.find_unbumped(BASE, _head(), ["wit/waddle-bundle/stage.wit"]) == [
        "a.py@1.0.1"
    ]


def test_sdk_change_with_bump_passes() -> None:
    """A bumped version clears the gate."""
    assert mod.find_unbumped(BASE, _head(py="1.0.2"), ["sdk/waddle-sdk/src/x.py"]) == []


def test_own_source_change_flags_rust() -> None:
    """A bundle's own source change requires its bump regardless of language."""
    assert mod.find_unbumped(BASE, _head(), ["bundles/rust/b/src/lib.rs"]) == [
        "b.rs@1.0.0"
    ]


def test_version_file_only_edit_not_flagged() -> None:
    """Editing only version-carrying files never demands another bump."""
    assert (
        mod.find_unbumped(BASE, _head(), ["bundles/python/a/hub-manifest.yaml"]) == []
    )


def test_new_bundle_not_flagged() -> None:
    """Bundles absent from base were never published, so cannot conflict."""
    head = (
        _head()
        + '  - {app_id: c.py, version: "1.0.0", language: python, source_dir: python/c}\n'
    )
    assert mod.find_unbumped(BASE, head, ["wit/x.wit"]) == ["a.py@1.0.1"]


def test_unrelated_change_passes() -> None:
    """Changes outside any build input pass."""
    assert (
        mod.find_unbumped(
            BASE, _head(), ["hub_api/app.py", "bundles/python/ab/src/x.py"]
        )
        == []
    )


def test_real_catalog_denominator() -> None:
    """The real catalog parses with a meaningful number of bundles."""
    root = Path(__file__).resolve().parents[3]
    n = len(mod._entries((root / mod.CATALOG).read_text()))
    assert n >= 100, f"only {n} bundles examined"


def test_docs_only_edit_in_python_bundle_not_flagged() -> None:
    """A README/markdown/license edit in a bundle dir never needs a bump (regression: #1302)."""
    changed = [
        "bundles/python/a/README.md",
        "bundles/python/a/docs/usage.md",
        "bundles/python/a/LICENSE",
        "bundles/python/a/NOTICE.txt",
    ]
    assert mod.find_unbumped(BASE, _head(), changed) == []


def test_docs_only_edit_in_compiled_bundle_not_flagged() -> None:
    """Same for a Rust bundle: docs and test-tree edits do not change the component bytes."""
    changed = ["bundles/rust/b/README.md", "bundles/rust/b/tests/e2e.rs"]
    assert mod.find_unbumped(BASE, _head(), changed) == []


def test_docs_plus_source_change_still_flagged() -> None:
    """A real source change next to a docs edit still demands the bump (python and rust)."""
    py = ["bundles/python/a/README.md", "bundles/python/a/src/app.py"]
    rs = ["bundles/rust/b/README.md", "bundles/rust/b/src/lib.rs"]
    assert mod.find_unbumped(BASE, _head(), py) == ["a.py@1.0.1"]
    assert mod.find_unbumped(BASE, _head(), rs) == ["b.rs@1.0.0"]


def test_compiled_manifest_metadata_files_still_flagged() -> None:
    """Cargo metadata is a build input for compiled bundles: unchanged behaviour."""
    assert mod.find_unbumped(BASE, _head(), ["bundles/rust/b/Cargo.lock"]) == [
        "b.rs@1.0.0"
    ]


def test_non_build_file_classifier() -> None:
    """Classifier: docs/license/test content is non-build; sources and lockfiles are not."""
    f = mod._is_non_build_file
    pre = "bundles/rust/b/"
    assert f(pre + "README.md", pre)
    assert f(pre + "docs/a/b.png", pre)
    assert f(pre + "tests/x.rs", pre)
    assert f(pre + "LICENSE-APACHE", pre)
    assert not f(pre + "src/lib.rs", pre)
    assert not f(pre + "src/tests.rs", pre)
    assert not f(pre + "Cargo.toml", pre)


SDK_REFS = {
    "b.rs": ["sdk/waddle-sdk-rs", "wit/waddle-bundle", "bundles/contracts/x.v1.json"]
}


def test_rust_shared_sdk_change_without_bump_flags_rust() -> None:
    """Regression (dal2alpha run 43849873861): an SDK change re-digests dependent Rust bundles."""
    changed = ["sdk/waddle-sdk-rs/src/tool.rs"]
    assert mod.find_unbumped(BASE, _head(), changed, SDK_REFS) == ["b.rs@1.0.0"]


def test_rust_include_str_contract_change_flags_rust() -> None:
    """A file pulled in via include_str! is a build input of the including bundle."""
    changed = ["bundles/contracts/x.v1.json"]
    assert mod.find_unbumped(BASE, _head(), changed, SDK_REFS) == ["b.rs@1.0.0"]


def test_rust_shared_change_with_bump_passes() -> None:
    """Bumping the dependent Rust bundle clears the shared-input finding."""
    changed = ["sdk/waddle-sdk-rs/src/tool.rs"]
    assert mod.find_unbumped(BASE, _head(rs="1.0.1"), changed, SDK_REFS) == []


def test_rust_shared_docs_or_sibling_file_not_flagged() -> None:
    """SDK docs/tests, an unreferenced sibling contract and a prefix-alike dir never flag."""
    changed = [
        "sdk/waddle-sdk-rs/README.md",
        "sdk/waddle-sdk-rs/tests/t.rs",
        "bundles/contracts/other.v1.json",
        "sdk/waddle-sdk-rs-extra/src/lib.rs",
    ]
    assert mod.find_unbumped(BASE, _head(), changed, SDK_REFS) == []


def test_real_rust_shared_refs_cover_sdk_wit_and_contracts() -> None:
    """The real tree resolves every Rust catalog bundle's refs (SDK, WIT world, contracts)."""
    root = Path(__file__).resolve().parents[3]
    head = (root / mod.CATALOG).read_text()
    refs = mod.rust_shared_refs(head, root)
    n_rust = sum(1 for e in mod._entries(head).values() if e.get("language") == "rust")
    assert n_rust >= 10 and len(refs) == n_rust, (n_rust, len(refs))
    music = refs["penguintechinc.waddles.music-requests"]
    assert "sdk/waddle-sdk-rs" in music
    assert any(r.startswith("wit/") for r in music)
    # rooms compiles webrtc's grants.rs via #[path]: a webrtc edit must bump rooms too.
    assert "bundles/rust/webrtc/src/grants.rs" in refs["penguintechinc.waddles.rooms"]


def test_explicit_in_crate_test_fixture_include_flags() -> None:
    """A tests/ file named by a release include_str! is a build input, not test content."""
    refs = {"b.rs": ["bundles/rust/b/tests/contract/x.json"]}
    changed = ["bundles/rust/b/tests/contract/x.json"]
    assert mod.find_unbumped(BASE, _head(), changed, refs) == ["b.rs@1.0.0"]
