//! D34 ordering: `build::run_build` must reject an invalid manifest or a
//! blocked scan before any bundle code executes. Both tests here fail at
//! a point strictly before the per-language builder would ever run, and
//! assert `component.wasm` was never written as proof no compile was
//! attempted.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bundle_compiler::build::{run_build, run_build_with_options};
use bundle_compiler::errors::CompilerError;
use bundle_compiler::manifest::ManifestOptions;
use std::path::Path;
use tempfile::tempdir;

#[test]
fn rejects_invalid_manifest_before_any_scan_or_compile() {
    let out = tempdir().unwrap();
    let err = run_build(
        Path::new("tests/fixtures/bundles/scan-clean"),
        Path::new("tests/fixtures/manifests/invalid_v14_schema_version.yaml"),
        out.path(),
    )
    .unwrap_err();
    match err {
        CompilerError::ManifestInvalid { reason, .. } => {
            assert_eq!(reason, "unsupported_schema_version")
        }
        other => panic!("expected ManifestInvalid, got {other:?}"),
    }
    assert!(
        !out.path().join("component.wasm").exists(),
        "no compile attempted after a manifest rejection"
    );
}

#[test]
fn rejects_legacy_dal_import_before_compiling() {
    let out = tempdir().unwrap();
    // valid_python.yaml's app_id/feature/module line up with any Python
    // bundle source; the source itself still imports the legacy DAL, so
    // the legacy-DAL scan (which runs before the SAST/secrets scan and
    // long before any compile step) must be what blocks this.
    let err = run_build(
        Path::new("tests/fixtures/bundles/legacy-dal"),
        Path::new("tests/fixtures/manifests/valid_python.yaml"),
        out.path(),
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "legacy_dal_import"),
        other => panic!("expected ScanBlocked, got {other:?}"),
    }
    assert!(
        !out.path().join("component.wasm").exists(),
        "no compile attempted after a scan rejection"
    );
}

#[test]
fn rust_manifest_skips_legacy_dal_scan_but_still_fails_before_compile() {
    // A non-Python manifest never runs the Python-only legacy-DAL check,
    // but must still reach (and stop at) the SAST/secrets scan or the
    // (stubbed) builder before producing a component -- this crate's own
    // `scan-clean` fixture is real Python source, but the manifest's
    // declared language, not the source's actual language, is what
    // selects the builder/scan path in this wave's wiring.
    let out = tempdir().unwrap();
    let err = run_build(
        Path::new("tests/fixtures/bundles/scan-clean"),
        Path::new("tests/fixtures/manifests/valid_rust.yaml"),
        out.path(),
    )
    .unwrap_err();
    // Either the SAST scan or the (stubbed) RustBuilder can be what
    // fails first depending on the host's semgrep availability -- both
    // are acceptable here; what matters is nothing wrote a component.
    assert!(matches!(
        err,
        CompilerError::ScanBlocked { .. } | CompilerError::CompileFailed { .. }
    ));
    assert!(!out.path().join("component.wasm").exists());
}

#[test]
fn prebuilt_artifact_succeeds_without_scanning_or_compiling() {
    // `artifact: prebuilt` skips every scan and the per-language builder
    // entirely (`publisher`, not `build`, validates a Tier 2 upload's
    // actual bytes) -- `run_build`'s hardcoded `ManifestOptions::default()`
    // (`allow_prebuilt: false`) never reaches this path, which is exactly
    // why `run_build_with_options` exists as a seam.
    let out = tempdir().unwrap();
    let opts = ManifestOptions {
        allow_prebuilt: true,
        ..ManifestOptions::default()
    };
    run_build_with_options(
        Path::new("tests/fixtures/bundles/prebuilt-component.wasm"),
        Path::new("tests/fixtures/manifests/valid_prebuilt.yaml"),
        out.path(),
        &opts,
    )
    .unwrap();

    let component = std::fs::read(out.path().join("component.wasm")).unwrap();
    let source = std::fs::read("tests/fixtures/bundles/prebuilt-component.wasm").unwrap();
    assert_eq!(component, source, "prebuilt bytes copied verbatim");

    let manifest_json = std::fs::read_to_string(out.path().join("manifest.json")).unwrap();
    let parsed: serde_json::Value = serde_json::from_str(&manifest_json).unwrap();
    assert_eq!(parsed["app_id"], "waddles.core.example.prebuilt");
    assert_eq!(parsed["artifact"], "prebuilt");
    assert_eq!(parsed["language"], "other");
}

#[test]
fn prebuilt_artifact_rejected_when_not_allowed() {
    let out = tempdir().unwrap();
    let err = run_build(
        Path::new("tests/fixtures/bundles/prebuilt-component.wasm"),
        Path::new("tests/fixtures/manifests/valid_prebuilt.yaml"),
        out.path(),
    )
    .unwrap_err();
    match err {
        CompilerError::ManifestInvalid { reason, .. } => assert_eq!(reason, "prebuilt_not_allowed"),
        other => panic!("expected ManifestInvalid, got {other:?}"),
    }
}
