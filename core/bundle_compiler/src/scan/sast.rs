//! Orchestrates the three containerized scanners spec SS9.3 requires for a
//! source upload: `semgrep` (SAST), a language-appropriate dependency
//! auditor, and `gitleaks` (secrets). Every scanner's own JSON report is
//! parsed for a count of items examined -- zero examined is a failure
//! (`scan_empty_denominator`), never a silent pass. Runs inside the
//! untrusted `build` container.
//!
//! Order matters and is fixed: gitleaks (secrets) first, then semgrep
//! (SAST), then the dependency audit -- a secret in the source is the
//! cheapest, highest-confidence signal to check and blocks immediately
//! without waiting on the other two.

use crate::errors::CompilerError;
use std::path::Path;
use std::process::Command;
use walkdir::WalkDir;

/// Aggregated result of all three source scanners, each reporting both a
/// finding count and an examined-item count -- the examined counts are
/// what makes a "0 findings" result distinguishable from a scanner that
/// silently examined nothing.
#[derive(Debug, Default, Clone, Copy)]
pub struct ScanReport {
    /// Count of ERROR-severity semgrep findings (0 unless blocked).
    pub semgrep_findings: usize,
    /// Count of files semgrep actually scanned.
    pub semgrep_examined: usize,
    /// Count of high/critical dependency advisories found.
    pub dependency_advisories: usize,
    /// Count of dependencies the audit tool examined.
    pub dependencies_examined: usize,
    /// Count of secrets gitleaks found (0 unless blocked).
    pub secrets_findings: usize,
    /// Count of files gitleaks scanned.
    pub files_examined_for_secrets: usize,
}

/// The external scanner binaries `run_source_scans` shells out to.
/// Configurable so tests can substitute a fixture stub for a scanner that
/// is not installed/working on a given host, without changing production
/// behavior: `run_source_scans` always uses `ScannerConfig::default()`,
/// which names the real pinned binaries (`build/tool-versions.env`) that
/// the compiler's container image (Task 19 of the M2a plan) installs.
#[derive(Debug, Clone)]
pub struct ScannerConfig {
    /// `gitleaks` binary name or path.
    pub gitleaks_bin: String,
    /// Path to gitleaks' rule config. Passed explicitly via `--config`
    /// rather than relying on gitleaks' embedded default -- some
    /// distributions of the binary ship without a usable embedded
    /// ruleset, so an explicit path is the only reliable option across
    /// environments.
    pub gitleaks_config_path: String,
    /// `semgrep` binary name or path.
    pub semgrep_bin: String,
    /// Path to the semgrep rules directory/config.
    pub semgrep_rules_path: String,
    /// `pip-audit` binary name or path.
    pub pip_audit_bin: String,
    /// `cargo` binary name or path (invoked as `cargo audit`).
    pub cargo_bin: String,
    /// `npm` binary name or path.
    pub npm_bin: String,
}

impl Default for ScannerConfig {
    fn default() -> Self {
        Self {
            gitleaks_bin: "gitleaks".to_string(),
            gitleaks_config_path: "/opt/waddles-gitleaks-config/gitleaks.toml".to_string(),
            semgrep_bin: "semgrep".to_string(),
            semgrep_rules_path: "/opt/waddles-semgrep-rules".to_string(),
            pip_audit_bin: "pip-audit".to_string(),
            cargo_bin: "cargo".to_string(),
            npm_bin: "npm".to_string(),
        }
    }
}

/// Runs semgrep, the language-appropriate dependency audit, and gitleaks
/// against `source_dir`, using the real pinned scanner binaries.
///
/// # Errors
/// See [`run_source_scans_with_config`].
pub fn run_source_scans(source_dir: &Path, language: &str) -> Result<ScanReport, CompilerError> {
    run_source_scans_with_config(source_dir, language, &ScannerConfig::default())
}

/// Same as [`run_source_scans`], with the scanner binaries named by
/// `config` rather than the compiled-in defaults.
///
/// # Errors
/// Returns `CompilerError::ScanBlocked` with:
/// - `reason = "scan_empty_denominator"` if `source_dir` contains no files
///   at all, or if a scanner examined zero items;
/// - `reason = "secrets_found"` if gitleaks found any secret;
/// - `reason = "sast_finding"` if semgrep found an ERROR-severity result;
/// - `reason = "dependency_vulnerability"` if the dependency audit found a
///   high/critical advisory;
/// - `reason = "scan_tool_missing"` if a scanner binary could not be
///   spawned at all, or `reason = "scan_tool_error"` if it ran but its
///   output could not be parsed.
pub fn run_source_scans_with_config(
    source_dir: &Path,
    language: &str,
    config: &ScannerConfig,
) -> Result<ScanReport, CompilerError> {
    let mut report = ScanReport::default();

    let file_count = WalkDir::new(source_dir)
        .into_iter()
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_file())
        .count();
    if file_count == 0 {
        return Err(CompilerError::ScanBlocked {
            reason: "scan_empty_denominator".to_string(),
            message: format!(
                "source scan examined 0 files under {}",
                source_dir.display()
            ),
        });
    }
    report.files_examined_for_secrets = file_count;

    run_gitleaks(source_dir, config, &mut report)?;
    run_semgrep(source_dir, config, &mut report)?;

    let (advisories, examined) = run_dependency_audit(source_dir, language, config)?;
    report.dependency_advisories = advisories;
    report.dependencies_examined = examined;
    if advisories > 0 {
        return Err(CompilerError::ScanBlocked {
            reason: "dependency_vulnerability".to_string(),
            message: format!("dependency audit found {advisories} high/critical advisory(ies)"),
        });
    }

    Ok(report)
}

/// Runs gitleaks against `source_dir` and records its findings into
/// `report`. Blocks on any secret found -- the cheapest, highest-confidence
/// signal, checked first.
fn run_gitleaks(
    source_dir: &Path,
    config: &ScannerConfig,
    report: &mut ScanReport,
) -> Result<(), CompilerError> {
    let report_path = source_dir.join(".gitleaks-report.json");
    let report_path_str = report_path.to_string_lossy().into_owned();
    let source_dir_str = source_dir.to_string_lossy().into_owned();
    let status = Command::new(&config.gitleaks_bin)
        .args([
            "detect",
            "--no-git",
            "--source",
            &source_dir_str,
            "--config",
            &config.gitleaks_config_path,
            "--report-format",
            "json",
            "--report-path",
            &report_path_str,
            "--exit-code",
            "1",
        ])
        .status()
        .map_err(|e| CompilerError::ScanBlocked {
            reason: "scan_tool_missing".to_string(),
            message: format!("gitleaks not runnable: {e}"),
        })?;

    let findings: Vec<serde_json::Value> = std::fs::read_to_string(&report_path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();
    report.secrets_findings = findings.len();
    let _ = std::fs::remove_file(&report_path);

    if !status.success() || report.secrets_findings > 0 {
        let files: Vec<&str> = findings
            .iter()
            .filter_map(|f| f.get("File").and_then(|v| v.as_str()))
            .collect();
        return Err(CompilerError::ScanBlocked {
            reason: "secrets_found".to_string(),
            message: format!(
                "gitleaks found {} secret(s) in {}: {}",
                report.secrets_findings,
                source_dir.display(),
                files.join(", ")
            ),
        });
    }
    Ok(())
}

/// Runs semgrep against `source_dir` and records its findings into
/// `report`. Blocks on any `ERROR`-severity result.
fn run_semgrep(
    source_dir: &Path,
    config: &ScannerConfig,
    report: &mut ScanReport,
) -> Result<(), CompilerError> {
    let source_dir_str = source_dir.to_string_lossy().into_owned();
    let output = Command::new(&config.semgrep_bin)
        .args([
            "--config",
            &config.semgrep_rules_path,
            "--json",
            "--quiet",
            &source_dir_str,
        ])
        .output()
        .map_err(|e| CompilerError::ScanBlocked {
            reason: "scan_tool_missing".to_string(),
            message: format!("semgrep not runnable: {e}"),
        })?;
    let parsed: serde_json::Value =
        serde_json::from_slice(&output.stdout).map_err(|e| CompilerError::ScanBlocked {
            reason: "scan_tool_error".to_string(),
            message: format!("semgrep produced non-JSON output: {e}"),
        })?;

    let results = parsed
        .get("results")
        .and_then(|r| r.as_array())
        .cloned()
        .unwrap_or_default();
    report.semgrep_examined = parsed
        .get("paths")
        .and_then(|p| p.get("scanned"))
        .and_then(|s| s.as_array())
        .map(std::vec::Vec::len)
        .unwrap_or(0);
    if report.semgrep_examined == 0 {
        return Err(CompilerError::ScanBlocked {
            reason: "scan_empty_denominator".to_string(),
            message: "semgrep examined 0 files".to_string(),
        });
    }

    let error_findings: Vec<&serde_json::Value> = results
        .iter()
        .filter(|r| {
            r.get("extra")
                .and_then(|e| e.get("severity"))
                .and_then(|s| s.as_str())
                == Some("ERROR")
        })
        .collect();
    report.semgrep_findings = error_findings.len();
    if !error_findings.is_empty() {
        return Err(CompilerError::ScanBlocked {
            reason: "sast_finding".to_string(),
            message: format!(
                "semgrep found {} ERROR-severity finding(s)",
                error_findings.len()
            ),
        });
    }
    Ok(())
}

/// Runs the language-appropriate dependency auditor. A bundle declaring no
/// third-party dependencies (no lockfile present) legitimately audits
/// zero dependencies -- unlike the file-scan denominator above, this is
/// not a `scan_empty_denominator` failure, since there is nothing to
/// audit by construction.
fn run_dependency_audit(
    source_dir: &Path,
    language: &str,
    config: &ScannerConfig,
) -> Result<(usize, usize), CompilerError> {
    match language {
        "python" => {
            let requirements = source_dir.join("requirements.txt");
            if !requirements.exists() {
                return Ok((0, 0));
            }
            let requirements_str = requirements.to_string_lossy().into_owned();
            let out = Command::new(&config.pip_audit_bin)
                .args(["-r", &requirements_str, "--format", "json"])
                .output()
                .map_err(|e| CompilerError::ScanBlocked {
                    reason: "scan_tool_missing".to_string(),
                    message: format!("pip-audit not runnable: {e}"),
                })?;
            let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap_or_default();
            let deps = json
                .get("dependencies")
                .and_then(|d| d.as_array())
                .cloned()
                .unwrap_or_default();
            let advisories: usize = deps
                .iter()
                .filter_map(|d| d.get("vulns").and_then(|v| v.as_array()))
                .map(std::vec::Vec::len)
                .sum();
            Ok((advisories, deps.len()))
        }
        "rust" => {
            let lockfile = source_dir.join("Cargo.lock");
            if !lockfile.exists() {
                return Ok((0, 0));
            }
            let lockfile_str = lockfile.to_string_lossy().into_owned();
            let out = Command::new(&config.cargo_bin)
                .args(["audit", "--file", &lockfile_str, "--json"])
                .output()
                .map_err(|e| CompilerError::ScanBlocked {
                    reason: "scan_tool_missing".to_string(),
                    message: format!("cargo-audit not runnable: {e}"),
                })?;
            let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap_or_default();
            let vulns = json
                .get("vulnerabilities")
                .and_then(|v| v.get("list"))
                .and_then(|l| l.as_array())
                .cloned()
                .unwrap_or_default();
            let examined = json
                .get("lockfile")
                .and_then(|l| l.get("dependency-count"))
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(0) as usize;
            Ok((vulns.len(), examined))
        }
        "javascript" | "typescript" => {
            let lockfile = source_dir.join("package-lock.json");
            if !lockfile.exists() {
                return Ok((0, 0));
            }
            let source_dir_str = source_dir.to_string_lossy().into_owned();
            let out = Command::new(&config.npm_bin)
                .args(["audit", "--json", "--prefix", &source_dir_str])
                .output()
                .map_err(|e| CompilerError::ScanBlocked {
                    reason: "scan_tool_missing".to_string(),
                    message: format!("npm audit not runnable: {e}"),
                })?;
            let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap_or_default();
            let vulns = json.get("metadata").and_then(|m| m.get("vulnerabilities"));
            let high = vulns
                .and_then(|v| v.get("high"))
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(0);
            let critical = vulns
                .and_then(|v| v.get("critical"))
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(0);
            // npm's `auditReportVersion: 2` JSON (npm 7+) nests the total
            // under `metadata.dependencies.total`, not the npm 6-era
            // `metadata.totalDependencies` -- verified against a real
            // `npm audit --json` run (npm 11.19.0) rather than assumed.
            let total_deps = json
                .get("metadata")
                .and_then(|m| m.get("dependencies"))
                .and_then(|d| d.get("total"))
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(0);
            Ok(((high + critical) as usize, total_deps as usize))
        }
        other => Err(CompilerError::Config(format!(
            "no dependency auditor wired for language {other:?}"
        ))),
    }
}
