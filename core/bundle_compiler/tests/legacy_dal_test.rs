//! Coverage for `scan::legacy_dal` -- rejects legacy `flask_core.database`/
//! `pydal` imports (D21b), and treats a zero-file scan as a failure, not
//! a silent pass.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bundle_compiler::errors::CompilerError;
use bundle_compiler::scan::legacy_dal::scan_legacy_dal_imports;
use std::path::Path;

#[test]
fn rejects_flask_core_database_import() {
    let err = scan_legacy_dal_imports(Path::new("tests/fixtures/bundles/legacy-dal")).unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, message } => {
            assert_eq!(reason, "legacy_dal_import");
            assert!(message.contains("flask_core.database"));
            assert!(message.contains("app.py"));
            assert!(message.contains("penguin_dal") || message.contains("penguin-dal"));
        }
        other => panic!("expected ScanBlocked, got {other:?}"),
    }
}

#[test]
fn accepts_clean_bundle() {
    let count = scan_legacy_dal_imports(Path::new("tests/fixtures/bundles/clean-dal")).unwrap();
    assert_eq!(count, 1);
}

#[test]
fn zero_files_scanned_is_a_failure_not_a_pass() {
    let tmp = tempfile::tempdir().unwrap();
    let err = scan_legacy_dal_imports(tmp.path()).unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "scan_empty_denominator"),
        other => panic!("expected ScanBlocked(scan_empty_denominator), got {other:?}"),
    }
}
