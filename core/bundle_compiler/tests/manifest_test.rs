//! Coverage for `bundle_compiler::manifest`'s adapter over
//! `penguin-bundle-host::manifest` -- proves the adapter round-trips a
//! valid manifest and maps a validation rejection to
//! `CompilerError::ManifestInvalid` with the expected stable reason code.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bundle_compiler::errors::CompilerError;
use bundle_compiler::manifest::{parse_and_validate, ManifestOptions};
use std::path::Path;

#[test]
fn valid_python_manifest_parses() {
    let m = parse_and_validate(
        Path::new("tests/fixtures/manifests/valid_python.yaml"),
        &ManifestOptions::default(),
    )
    .unwrap();
    assert_eq!(m.app_id, "waddles.core.example.echo");
    assert_eq!(m.language, "python");
    assert_eq!(m.artifact, "source");
}

#[test]
fn valid_rust_manifest_parses() {
    let m = parse_and_validate(
        Path::new("tests/fixtures/manifests/valid_rust.yaml"),
        &ManifestOptions::default(),
    )
    .unwrap();
    assert_eq!(m.language, "rust");
}

#[test]
fn valid_js_manifest_parses() {
    let m = parse_and_validate(
        Path::new("tests/fixtures/manifests/valid_js.yaml"),
        &ManifestOptions::default(),
    )
    .unwrap();
    assert_eq!(m.language, "javascript");
}

#[test]
fn unsupported_schema_version_is_rejected_with_stable_reason() {
    let err = parse_and_validate(
        Path::new("tests/fixtures/manifests/invalid_v14_schema_version.yaml"),
        &ManifestOptions::default(),
    )
    .unwrap_err();
    match err {
        CompilerError::ManifestInvalid { reason, .. } => {
            assert_eq!(reason, "unsupported_schema_version")
        }
        other => panic!("expected ManifestInvalid, got {other:?}"),
    }
}

#[test]
fn missing_file_is_an_io_error_not_a_manifest_error() {
    let err = parse_and_validate(
        Path::new("tests/fixtures/manifests/does_not_exist.yaml"),
        &ManifestOptions::default(),
    )
    .unwrap_err();
    assert!(matches!(err, CompilerError::Io(_)));
}

#[test]
fn malformed_yaml_is_rejected_as_invalid_yaml() {
    let tmp = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(tmp.path(), "not: valid: yaml: [").unwrap();
    let err = parse_and_validate(tmp.path(), &ManifestOptions::default()).unwrap_err();
    match err {
        CompilerError::ManifestInvalid { reason, .. } => assert_eq!(reason, "invalid_yaml"),
        other => panic!("expected ManifestInvalid, got {other:?}"),
    }
}
