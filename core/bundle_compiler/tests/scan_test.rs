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
//!
//! **The three dependency-audit tools (`pip-audit`/`cargo-audit`/`npm
//! audit`) are never invoked for real anywhere in this file, by the same
//! network-hermeticity requirement as `semgrep`'s stub above -- CI has no
//! network access to any of their advisory feeds, and this crate's fix
//! for a MED security-review finding (fail-closed on a dependency-audit
//! tool spawn/parse error, see `scan::sast::run_dependency_audit`'s doc
//! comment) turns exactly that fetch failure into a hard block. Every
//! test exercising the dependency-audit branches therefore uses a
//! fixture stub -- `*-stub-clean.sh` (empty advisories, happy path),
//! `*-stub-vulnerable.sh` (one fabricated advisory, blocking path), or
//! `audit-stub-network-failure.sh` (spawns fine, produces no parseable
//! output, fails-closed path) -- each mirroring the real tool's JSON
//! shape exactly, never a real network call.**
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
    // Fixture Cargo.lock is this crate's own -- a real, valid lockfile.
    // The audit *tool* itself is a stub (`cargo-audit-stub-clean.sh`),
    // deliberately never the real `cargo audit` binary: CI runs with no
    // network access to the RustSec advisory database, so a real
    // invocation here would hit the exact fetch failure
    // `audit-stub-network-failure.sh` simulates, which now correctly
    // fails closed (see the `_fails_closed_by_default` tests below) --
    // this happy-path test must stay hermetic, asserting only that
    // `dependencies_examined` is parsed from the real lockfile on disk,
    // independent of any tool/network at all.
    let config = ScannerConfig {
        cargo_bin: "tests/fixtures/bin/cargo-audit-stub-clean.sh".to_string(),
        ..test_scanner_config()
    };
    let report = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/rust-with-deps"),
        "rust",
        &config,
    )
    .unwrap();
    assert!(
        report.dependencies_examined > 0,
        "examined must come from this crate's own real Cargo.lock on disk"
    );
    assert_eq!(report.dependency_advisories, 0);
}

#[test]
fn js_dependency_audit_examines_a_real_lockfile() {
    // Same hermeticity rationale as the rust test above -- the audit tool
    // is a stub (`npm-audit-stub-clean.sh`), never real `npm audit`,
    // since CI has no network access to the npm registry.
    let config = ScannerConfig {
        npm_bin: "tests/fixtures/bin/npm-audit-stub-clean.sh".to_string(),
        ..test_scanner_config()
    };
    let report = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/js-with-deps"),
        "javascript",
        &config,
    )
    .unwrap();
    assert!(
        report.dependencies_examined > 0,
        "examined must come from the fixture's real package-lock.json on disk"
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
fn python_dependency_audit_blocks_on_a_fabricated_advisory() {
    // `requests==2.31.0` carries real, published PyPI advisories -- but
    // this test uses a fixture stub (`pip-audit-stub-vulnerable.sh`)
    // reporting one fabricated advisory for it, mirroring real
    // pip-audit's JSON shape exactly, rather than the real `pip-audit`
    // binary hitting PyPI's live advisory feed: CI runs with no network
    // access to that feed, where a real invocation would fail exactly
    // like `audit-stub-network-failure.sh` simulates and now correctly
    // fails closed with `scan_tool_error` (see the
    // `_fails_closed_by_default` test below) -- not the
    // `dependency_vulnerability` reason this test exercises. Same
    // hermeticity rationale as `js_dependency_audit_blocks_on_a_fabricated_advisory`.
    let config = ScannerConfig {
        pip_audit_bin: "tests/fixtures/bin/pip-audit-stub-vulnerable.sh".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/python-with-vulnerable-deps"),
        "python",
        &config,
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

// Regression coverage for a real CI incident: `cargo-audit` failing to
// fetch the RustSec advisory database over the network (a full git clone,
// slower and less reliable than pip-audit's/npm's registry calls) made
// `dependencies_examined` come back `0` -- indistinguishable from "no
// dependencies declared" -- because it was read from cargo-audit's own
// JSON, which the tool never produces on a fetch failure. `examined` is
// now parsed from the lockfile/requirements file directly (these three
// tests still prove that, via `tolerate_degraded_dependency_audit: true`
// -- the explicit first-party/dev-tier opt-in that accepts a degraded
// advisory count). The *default* (`ScannerConfig::default()`, what every
// untrusted community-bundle build actually uses) instead fails **closed**
// on the exact same tool failure -- see the `_fails_closed_by_default`
// tests below, added for a MED security-review finding: a fail-open
// advisory count let an attacker induce/await this network failure and
// ship known-vulnerable dependencies past the gate.

#[test]
fn rust_examined_count_survives_a_cargo_audit_network_failure_when_tolerated() {
    let config = ScannerConfig {
        cargo_bin: "tests/fixtures/bin/audit-stub-network-failure.sh".to_string(),
        tolerate_degraded_dependency_audit: true,
        ..test_scanner_config()
    };
    let report = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/rust-with-deps"),
        "rust",
        &config,
    )
    .unwrap();
    assert!(
        report.dependencies_examined > 0,
        "examined must come from Cargo.lock on disk, not cargo-audit's (failed) JSON output"
    );
    assert_eq!(
        report.dependency_advisories, 0,
        "advisories degrade to 0 on a tool failure only under the explicit opt-in"
    );
}

#[test]
fn python_examined_count_survives_a_pip_audit_network_failure_when_tolerated() {
    let config = ScannerConfig {
        pip_audit_bin: "tests/fixtures/bin/audit-stub-network-failure.sh".to_string(),
        tolerate_degraded_dependency_audit: true,
        ..test_scanner_config()
    };
    let report = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/python-with-vulnerable-deps"),
        "python",
        &config,
    )
    .unwrap();
    assert!(
        report.dependencies_examined > 0,
        "examined must come from requirements.txt on disk, not pip-audit's (failed) JSON output"
    );
    assert_eq!(
        report.dependency_advisories, 0,
        "advisories degrade to 0 on a tool failure only under the explicit opt-in"
    );
}

#[test]
fn js_examined_count_survives_an_npm_audit_network_failure_when_tolerated() {
    let config = ScannerConfig {
        npm_bin: "tests/fixtures/bin/audit-stub-network-failure.sh".to_string(),
        tolerate_degraded_dependency_audit: true,
        ..test_scanner_config()
    };
    let report = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/js-with-deps"),
        "javascript",
        &config,
    )
    .unwrap();
    assert!(
        report.dependencies_examined > 0,
        "examined must come from package-lock.json on disk, not npm audit's (failed) JSON output"
    );
    assert_eq!(
        report.dependency_advisories, 0,
        "advisories degrade to 0 on a tool failure only under the explicit opt-in"
    );
}

#[test]
fn rust_dependency_audit_tool_error_fails_closed_by_default() {
    // `test_scanner_config()` inherits `ScannerConfig::default()`'s
    // `tolerate_degraded_dependency_audit: false` -- the only value the
    // real community-bundle build path (`sast::run_source_scans`) ever
    // uses. A tool failure must block, never degrade-and-proceed.
    let config = ScannerConfig {
        cargo_bin: "tests/fixtures/bin/audit-stub-network-failure.sh".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/rust-with-deps"),
        "rust",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, message } => {
            assert_eq!(reason, "scan_tool_error");
            assert!(message.contains("cargo-audit"));
        }
        other => panic!("expected ScanBlocked(scan_tool_error), got {other:?}"),
    }
}

#[test]
fn python_dependency_audit_tool_error_fails_closed_by_default() {
    let config = ScannerConfig {
        pip_audit_bin: "tests/fixtures/bin/audit-stub-network-failure.sh".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/python-with-vulnerable-deps"),
        "python",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, message } => {
            assert_eq!(reason, "scan_tool_error");
            assert!(message.contains("pip-audit"));
        }
        other => panic!("expected ScanBlocked(scan_tool_error), got {other:?}"),
    }
}

#[test]
fn js_dependency_audit_tool_error_fails_closed_by_default() {
    let config = ScannerConfig {
        npm_bin: "tests/fixtures/bin/audit-stub-network-failure.sh".to_string(),
        ..test_scanner_config()
    };
    let err = run_source_scans_with_config(
        Path::new("tests/fixtures/bundles/js-with-deps"),
        "javascript",
        &config,
    )
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, message } => {
            assert_eq!(reason, "scan_tool_error");
            assert!(message.contains("npm audit"));
        }
        other => panic!("expected ScanBlocked(scan_tool_error), got {other:?}"),
    }
}
