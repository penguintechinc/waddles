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
    /// Tier-gated escape hatch for a dependency-audit tool (`pip-audit`/
    /// `cargo-audit`/`npm audit`) that ran but produced no parseable
    /// output -- typically a transient failure fetching its advisory
    /// database. `false` (fail-**closed**: the audit blocks with
    /// `CompilerError::ScanBlocked { reason: "scan_tool_error", .. }`) is
    /// the only value `ScannerConfig::default()` ever returns, which is
    /// what every untrusted community-bundle build uses -- see
    /// `run_dependency_audit`'s doc comment. Setting this `true` is a
    /// deliberate opt-in reserved for a first-party/dev build tier that
    /// explicitly accepts a degraded (zeroed) advisory count rather than
    /// blocking on a transient network issue; nothing in this crate turns
    /// it on today.
    pub tolerate_degraded_dependency_audit: bool,
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
            tolerate_degraded_dependency_audit: false,
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
///
/// **`examined` is always parsed from the lockfile/requirements file on
/// disk, never from the auditor tool's own report.** The three
/// third-party auditors here (`pip-audit`, `cargo-audit`, `npm audit`)
/// each need network access to fetch advisory data before they can
/// produce a report at all; a transient failure to do so (observed in CI:
/// `cargo-audit` fetching the full RustSec advisory-db git repo) makes
/// the tool emit no parseable JSON, which used to silently zero out
/// `dependencies_examined` too -- indistinguishable from "this bundle
/// declares no dependencies." Reading the count straight from the
/// manifest file makes it hermetic and always accurate.
///
/// **The `advisories` half fails *closed* by default.** A dependency
/// audit exists specifically to catch known-vulnerable pins in an
/// untrusted community bundle; degrading the advisory count to `0` and
/// letting the build proceed on a tool failure would let an attacker
/// induce (or simply wait for) the exact transient network failure this
/// module observed in CI and ship known-vulnerable dependencies past the
/// gate. So unless `config.tolerate_degraded_dependency_audit` is
/// explicitly set (a first-party/dev-tier opt-in `ScannerConfig::default()`
/// never sets), a tool-spawn success with unparseable output returns
/// `CompilerError::ScanBlocked { reason: "scan_tool_error", .. }` instead
/// of a degraded report -- see [`warn_degraded_audit`]'s doc comment for
/// the (opt-in-only) tolerated path.
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
            // `examined` is parsed from requirements.txt directly, not from
            // pip-audit's own JSON -- see this function's doc comment.
            let examined = count_requirements_entries(&requirements)?;
            let requirements_str = requirements.to_string_lossy().into_owned();
            let out = Command::new(&config.pip_audit_bin)
                .args(["-r", &requirements_str, "--format", "json"])
                .output()
                .map_err(|e| CompilerError::ScanBlocked {
                    reason: "scan_tool_missing".to_string(),
                    message: format!("pip-audit not runnable: {e}"),
                })?;
            let advisories = match serde_json::from_slice::<serde_json::Value>(&out.stdout) {
                Ok(json) => json
                    .get("dependencies")
                    .and_then(|d| d.as_array())
                    .map(|deps| {
                        deps.iter()
                            .filter_map(|d| d.get("vulns").and_then(|v| v.as_array()))
                            .map(std::vec::Vec::len)
                            .sum()
                    })
                    .unwrap_or(0),
                Err(e) => {
                    if config.tolerate_degraded_dependency_audit {
                        warn_degraded_audit("pip-audit", &e, &out.stderr);
                        0
                    } else {
                        return Err(degraded_audit_blocked("pip-audit", &e, &out.stderr));
                    }
                }
            };
            Ok((advisories, examined))
        }
        "rust" => {
            let lockfile = source_dir.join("Cargo.lock");
            if !lockfile.exists() {
                return Ok((0, 0));
            }
            // `examined` is parsed from Cargo.lock directly (count of
            // `[[package]]` entries), not from cargo-audit's own
            // `lockfile.dependency-count` -- see this function's doc
            // comment: a cargo-audit run that fails to fetch the RustSec
            // advisory DB (a real, observed CI failure mode -- network
            // hiccups fetching a full git clone of the advisory DB) still
            // leaves us knowing exactly how many crates this bundle
            // pinned, from bytes already on disk.
            let examined = count_cargo_lock_packages(&lockfile)?;
            let lockfile_str = lockfile.to_string_lossy().into_owned();
            let out = Command::new(&config.cargo_bin)
                .args(["audit", "--file", &lockfile_str, "--json"])
                .output()
                .map_err(|e| CompilerError::ScanBlocked {
                    reason: "scan_tool_missing".to_string(),
                    message: format!("cargo-audit not runnable: {e}"),
                })?;
            let advisories = match serde_json::from_slice::<serde_json::Value>(&out.stdout) {
                Ok(json) => json
                    .get("vulnerabilities")
                    .and_then(|v| v.get("list"))
                    .and_then(|l| l.as_array())
                    .map(std::vec::Vec::len)
                    .unwrap_or(0),
                Err(e) => {
                    if config.tolerate_degraded_dependency_audit {
                        warn_degraded_audit("cargo-audit", &e, &out.stderr);
                        0
                    } else {
                        return Err(degraded_audit_blocked("cargo-audit", &e, &out.stderr));
                    }
                }
            };
            Ok((advisories, examined))
        }
        "javascript" | "typescript" => {
            let lockfile = source_dir.join("package-lock.json");
            if !lockfile.exists() {
                return Ok((0, 0));
            }
            // `examined` is parsed from package-lock.json directly, not
            // from npm audit's own JSON -- see this function's doc comment.
            let examined = count_package_lock_entries(&lockfile)?;
            let source_dir_str = source_dir.to_string_lossy().into_owned();
            let out = Command::new(&config.npm_bin)
                .args(["audit", "--json", "--prefix", &source_dir_str])
                .output()
                .map_err(|e| CompilerError::ScanBlocked {
                    reason: "scan_tool_missing".to_string(),
                    message: format!("npm audit not runnable: {e}"),
                })?;
            let advisories = match serde_json::from_slice::<serde_json::Value>(&out.stdout) {
                Ok(json) => {
                    let vulns = json.get("metadata").and_then(|m| m.get("vulnerabilities"));
                    let high = vulns
                        .and_then(|v| v.get("high"))
                        .and_then(serde_json::Value::as_u64)
                        .unwrap_or(0);
                    let critical = vulns
                        .and_then(|v| v.get("critical"))
                        .and_then(serde_json::Value::as_u64)
                        .unwrap_or(0);
                    (high + critical) as usize
                }
                Err(e) => {
                    if config.tolerate_degraded_dependency_audit {
                        warn_degraded_audit("npm audit", &e, &out.stderr);
                        0
                    } else {
                        return Err(degraded_audit_blocked("npm audit", &e, &out.stderr));
                    }
                }
            };
            Ok((advisories, examined))
        }
        other => Err(CompilerError::Config(format!(
            "no dependency auditor wired for language {other:?}"
        ))),
    }
}

/// Blocks the build on a dependency-audit tool producing non-JSON output
/// -- the default, fail-**closed** path for the untrusted community-bundle
/// tier (`config.tolerate_degraded_dependency_audit == false`, which is
/// what `ScannerConfig::default()` always sets). See
/// [`run_dependency_audit`]'s doc comment for why a degraded advisory
/// count must never silently pass: an attacker able to induce or await
/// the tool's advisory-database fetch failing (a real, observed CI
/// failure mode) would otherwise ship known-vulnerable dependencies past
/// this gate.
fn degraded_audit_blocked(
    tool: &str,
    parse_error: &serde_json::Error,
    stderr: &[u8],
) -> CompilerError {
    CompilerError::ScanBlocked {
        reason: "scan_tool_error".to_string(),
        message: format!(
            "{tool} produced non-JSON output ({parse_error}); refusing to proceed with a \
             degraded advisory count for an untrusted bundle (stderr: {})",
            String::from_utf8_lossy(stderr)
        ),
    }
}

/// Logs (never silently swallows) a dependency-audit tool producing
/// non-JSON output -- typically a network failure fetching an advisory
/// database (RustSec for cargo-audit, PyPI's for pip-audit, npm's
/// registry for `npm audit`). Only reached when the caller has explicitly
/// set `config.tolerate_degraded_dependency_audit = true` -- a
/// first-party/dev-tier opt-in `ScannerConfig::default()` never enables;
/// every other caller gets [`degraded_audit_blocked`] instead. The
/// advisory count degrades to `0` for this run rather than blocking; the
/// WARN line is the operational signal that the check did not actually
/// run, so a silently-passing scan is at least visible in logs, not
/// indistinguishable from a real clean result. `dependencies_examined`
/// (computed separately, from the lockfile/requirements file on disk) is
/// unaffected either way.
fn warn_degraded_audit(tool: &str, parse_error: &serde_json::Error, stderr: &[u8]) {
    tracing::warn!(
        tool,
        error = %parse_error,
        stderr = %String::from_utf8_lossy(stderr),
        "dependency-audit tool produced non-JSON output (advisory count degraded to 0 for this run; tolerated by explicit opt-in)"
    );
}

/// Counts non-empty, non-comment lines in a `requirements.txt` -- a
/// hermetic proxy for "how many dependencies were declared," independent
/// of whether `pip-audit` itself could reach PyPI's advisory feed.
fn count_requirements_entries(path: &Path) -> Result<usize, CompilerError> {
    let contents = std::fs::read_to_string(path)?;
    Ok(contents
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .count())
}

/// Counts `[[package]]` entries in a `Cargo.lock` -- a hermetic proxy for
/// "how many crates were pinned," independent of whether `cargo audit`
/// itself could fetch the RustSec advisory database. Cargo.lock is a
/// machine-generated, format-stable file; every package entry begins with
/// this exact line, so a plain line count is reliable without pulling in
/// a TOML parser dependency for one field.
fn count_cargo_lock_packages(path: &Path) -> Result<usize, CompilerError> {
    let contents = std::fs::read_to_string(path)?;
    Ok(contents
        .lines()
        .filter(|line| line.trim() == "[[package]]")
        .count())
}

/// Counts dependency entries in a `package-lock.json` -- a hermetic proxy
/// for "how many packages were resolved," independent of whether `npm
/// audit` itself could reach the npm registry. Supports the `packages`
/// map (lockfile v2/v3, excluding the root `""` entry) and falls back to
/// the `dependencies` map (lockfile v1) when `packages` is absent.
fn count_package_lock_entries(path: &Path) -> Result<usize, CompilerError> {
    let contents = std::fs::read_to_string(path)?;
    let json: serde_json::Value =
        serde_json::from_str(&contents).map_err(|e| CompilerError::ScanBlocked {
            reason: "scan_tool_error".to_string(),
            message: format!("{}: not valid JSON: {e}", path.display()),
        })?;
    if let Some(packages) = json.get("packages").and_then(|p| p.as_object()) {
        return Ok(packages.keys().filter(|k| !k.is_empty()).count());
    }
    if let Some(deps) = json.get("dependencies").and_then(|d| d.as_object()) {
        return Ok(deps.len());
    }
    Ok(0)
}
