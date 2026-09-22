//! Coverage for `scan::sast` -- orchestrates gitleaks (secrets), semgrep
//! (SAST), and the dependency audit, each gated on a non-zero examined
//! count.
//!
//! The real `gitleaks` binary is used throughout (it is installed and
//! working in CI and in this crate's dev environment); `semgrep_bin` is
//! pointed at a fixture stub (`tests/fixtures/bin/semgrep-stub.sh`) for
//! the one test that must reach semgrep, because a real semgrep install
//! is not reliably available on every host this suite runs on. The stub
//! honors the same `--json`/`paths.scanned` contract real semgrep does
//! and always reports zero findings -- see the stub's own header comment.
//! `run_source_scans` (the production entry point, real binaries only) is
//! exercised directly for the zero-file case, which fails before any
//! external tool is invoked.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bundle_compiler::errors::CompilerError;
use bundle_compiler::scan::sast::{run_source_scans, run_source_scans_with_config, ScannerConfig};
use std::path::Path;

/// A `ScannerConfig` usable in this crate's own test suite: real gitleaks
/// with a small self-contained rule file (some gitleaks distributions
/// ship without a working embedded default -- see the crate's own
/// `gitleaks_config_path` doc comment), and the fixture semgrep stub (see
/// `tests/fixtures/bin/semgrep-stub.sh`'s header comment for why).
fn test_scanner_config() -> ScannerConfig {
    ScannerConfig {
        gitleaks_config_path: "tests/fixtures/gitleaks-test-config.toml".to_string(),
        semgrep_bin: "tests/fixtures/bin/semgrep-stub.sh".to_string(),
        ..ScannerConfig::default()
    }
}

#[test]
fn clean_bundle_passes_with_nonzero_denominator() {
    let report = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/scan-clean"),
        "python",
        &test_scanner_config(),
    )
    .unwrap();
    assert!(report.files_examined_for_secrets > 0);
    assert!(report.semgrep_examined > 0);
    assert_eq!(report.secrets_findings, 0);
    assert_eq!(report.semgrep_findings, 0);
}

#[test]
fn secret_in_source_blocks() {
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/scan-secret"),
        "python",
        &test_scanner_config(),
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, message } => {
            assert_eq!(reason, "secrets_found");
            assert!(message.contains("app.py"));
        }
        other => panic!("expected ScanBlocked, got {other:?}"),
    }
}

#[test]
fn zero_examined_is_a_failure() {
    let tmp = tempfile::tempdir().unwrap();
    let err = run_source_scans(tmp.path(), "python").unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "scan_empty_denominator"),
        other => panic!("expected ScanBlocked(scan_empty_denominator), got {other:?}"),
    }
}

#[test]
fn missing_scanner_binary_reports_scan_tool_missing() {
    let mut config = test_scanner_config();
    config.gitleaks_bin = "definitely-not-a-real-binary-xyz".to_string();
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/scan-clean"),
        "python",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "scan_tool_missing"),
        other => panic!("expected ScanBlocked(scan_tool_missing), got {other:?}"),
    }
}

#[test]
fn no_lockfile_means_zero_dependencies_examined_not_a_failure() {
    // scan-clean has no requirements.txt -- the dependency audit must
    // legitimately report (0, 0) rather than tripping the denominator
    // gate, which only applies to the file-scan/semgrep/secrets counts.
    let report = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/scan-clean"),
        "python",
        &test_scanner_config(),
    )
    .unwrap();
    assert_eq!(report.dependency_advisories, 0);
    assert_eq!(report.dependencies_examined, 0);
}

#[test]
fn rust_dependency_audit_examines_a_real_lockfile() {
    // Fixture Cargo.lock is this crate's own -- a real, valid lockfile
    // real `cargo audit` can parse, run against the real RustSec advisory
    // DB (network-fetched/cached by cargo-audit itself, not this crate).
    let report = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/rust-with-deps"),
        "rust",
        &test_scanner_config(),
    )
    .unwrap();
    assert!(
        report.dependencies_examined > 0,
        "cargo audit should have examined this crate's own real Cargo.lock"
    );
}

#[test]
fn js_dependency_audit_examines_a_real_lockfile() {
    let report = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/js-with-deps"),
        "javascript",
        &test_scanner_config(),
    )
    .unwrap();
    assert!(
        report.dependencies_examined > 0,
        "npm audit should have examined the fixture package-lock.json"
    );
    assert_eq!(report.dependency_advisories, 0);
}

#[test]
fn unknown_language_is_a_config_error() {
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/scan-clean"),
        "cobol",
        &test_scanner_config(),
    )
    .unwrap_err();
    assert!(matches!(err, CompilerError::Config(_)));
}

#[test]
fn semgrep_error_severity_finding_blocks() {
    let config = ScannerConfig {
        semgrep_bin: "tests/fixtures/bin/semgrep-stub-error-finding.sh".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/scan-clean"),
        "python",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "sast_finding"),
        other => panic!("expected ScanBlocked(sast_finding), got {other:?}"),
    }
}

#[test]
fn semgrep_non_json_output_is_a_tool_error() {
    let config = ScannerConfig {
        semgrep_bin: "tests/fixtures/bin/semgrep-stub-bad-json.sh".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/scan-clean"),
        "python",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "scan_tool_error"),
        other => panic!("expected ScanBlocked(scan_tool_error), got {other:?}"),
    }
}

#[test]
fn semgrep_reporting_zero_scanned_is_a_failure() {
    let config = ScannerConfig {
        semgrep_bin: "tests/fixtures/bin/semgrep-stub-zero-scanned.sh".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/scan-clean"),
        "python",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "scan_empty_denominator"),
        other => panic!("expected ScanBlocked(scan_empty_denominator), got {other:?}"),
    }
}

#[test]
fn missing_semgrep_binary_reports_scan_tool_missing() {
    let config = ScannerConfig {
        semgrep_bin: "definitely-not-a-real-binary-xyz".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/scan-clean"),
        "python",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "scan_tool_missing"),
        other => panic!("expected ScanBlocked(scan_tool_missing), got {other:?}"),
    }
}

#[test]
fn scanner_config_is_debug_and_clone() {
    // Cheap coverage for the derive-generated impls -- exercised so a
    // future accidental removal of the derive is caught by a test, not
    // just a downstream compile error somewhere else.
    let config = ScannerConfig::default();
    let cloned = config.clone();
    assert_eq!(format!("{config:?}"), format!("{cloned:?}"));
}

#[test]
fn python_dependency_audit_blocks_on_a_real_known_vulnerable_pin() {
    // `requests==2.31.0` carries real, published PyPI advisories -- real
    // `pip-audit` finds them (network-fetched from PyPI's advisory feed,
    // not this crate's own doing), exercising both the python branch of
    // `run_dependency_audit` and the `dependency_vulnerability` blocking
    // path in one real, non-fabricated case.
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/python-with-vulnerable-deps"),
        "python",
        &test_scanner_config(),
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "dependency_vulnerability"),
        other => panic!("expected ScanBlocked(dependency_vulnerability), got {other:?}"),
    }
}

#[test]
fn js_dependency_audit_blocks_on_a_fabricated_advisory() {
    // Unlike the python case above, this uses a fixture stub rather than
    // a real currently-vulnerable npm package, which would make the test
    // flaky as advisories are published/fixed upstream over time -- see
    // the stub's own header comment.
    let config = ScannerConfig {
        npm_bin: "tests/fixtures/bin/npm-audit-stub-vulnerable.sh".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/js-with-deps"),
        "javascript",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "dependency_vulnerability"),
        other => panic!("expected ScanBlocked(dependency_vulnerability), got {other:?}"),
    }
}

#[test]
fn missing_pip_audit_binary_reports_scan_tool_missing() {
    let config = ScannerConfig {
        pip_audit_bin: "definitely-not-a-real-binary-xyz".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/python-with-vulnerable-deps"),
        "python",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "scan_tool_missing"),
        other => panic!("expected ScanBlocked(scan_tool_missing), got {other:?}"),
    }
}

#[test]
fn missing_cargo_binary_reports_scan_tool_missing() {
    let config = ScannerConfig {
        cargo_bin: "definitely-not-a-real-binary-xyz".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/rust-with-deps"),
        "rust",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "scan_tool_missing"),
        other => panic!("expected ScanBlocked(scan_tool_missing), got {other:?}"),
    }
}

#[test]
fn missing_npm_binary_reports_scan_tool_missing() {
    let config = ScannerConfig {
        npm_bin: "definitely-not-a-real-binary-xyz".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/js-with-deps"),
        "javascript",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "scan_tool_missing"),
        other => panic!("expected ScanBlocked(scan_tool_missing), got {other:?}"),
    }
}
