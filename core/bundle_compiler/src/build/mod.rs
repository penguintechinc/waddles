//! Everything the untrusted `build` initContainer does, in order: parse
//! and validate the manifest (pure YAML, `crate::manifest`), scan the
//! source (`crate::scan`), then compile with the manifest's declared
//! language's recipe. No credential of any kind is read here -- no
//! bucket, no DB, no signing key -- by construction: this module never
//! imports `crate::bucket`, `crate::db`, `crate::sidecar` or
//! `crate::callback` at all.
//!
//! **D34 (spec SS9.3), enforced by `run_build`'s call order below, not by
//! convention:** every scan here runs against inert source text and must
//! pass before the one step that executes bundle-supplied code -- the
//! per-language [`LanguageBuilder::build`] call
//! (`componentize-py`/`cargo component`/`jco`). Reordering these calls
//! would let unscanned code run; `build_orchestration_test.rs` asserts
//! that a manifest or scan rejection never reaches a builder.

/// JS/TS build recipe (`jco componentize --disable all`). Stub in this
/// wave -- see `JsBuilder`'s doc comment.
mod js;
/// Python build recipe (`componentize-py --stub-wasi`). Stub in this
/// wave -- see `PythonBuilder`'s doc comment.
mod python;
/// Rust build recipe (`cargo component build --target wasm32-wasip2`).
/// Stub in this wave -- see `RustBuilder`'s doc comment.
mod rust;

use crate::errors::CompilerError;
use crate::manifest::{self, BundleManifest, ManifestOptions};
use crate::scan::{legacy_dal, sast, skauswatch};
use serde::Serialize;
use std::path::{Path, PathBuf};

/// Governs what happens when a `source`-artifact build reaches the
/// Skauswatch hand-off with `SKAUSWATCH_URL` unset (reported as
/// [`skauswatch::SkauswatchVerdict::NotConfigured`]).
///
/// `Required` -- the only value `run_build`/`run_build_with_options` (and
/// therefore this crate's CLI) ever select -- fails the build **closed**:
/// the untrusted community/prebuilt bundle intake path must not let an
/// unconfigured malware scanner silently stand in for a clean scan.
/// `OptionalForDev` is an explicit opt-in escape hatch reserved for a
/// future first-party/dev build path that intentionally tolerates
/// skipping the scan; nothing in this crate constructs it today.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SkauswatchRequirement {
    /// Fail closed if Skauswatch is not configured (default, only value
    /// the CLI selects).
    #[default]
    Required,
    /// Explicit first-party/dev-tier opt-in: proceed even if Skauswatch
    /// is not configured.
    OptionalForDev,
}

/// Pure decision logic for the Skauswatch fail-closed gate, factored out
/// of the I/O-heavy [`run_build_with_options_and_skauswatch_policy`] so it
/// is unit-testable without spawning real scanner binaries (`semgrep` in
/// particular is not reliably installable on every host this crate's own
/// test suite runs on -- see `tests/fixtures/bin/semgrep-stub.sh`'s header
/// comment).
///
/// # Errors
/// Returns `CompilerError::ScanBlocked { reason: "skauswatch_not_configured", .. }`
/// if `verdict` is [`skauswatch::SkauswatchVerdict::NotConfigured`] and
/// `requirement` is [`SkauswatchRequirement::Required`].
fn skauswatch_gate(
    verdict: &skauswatch::SkauswatchVerdict,
    requirement: SkauswatchRequirement,
) -> Result<(), CompilerError> {
    if matches!(verdict, skauswatch::SkauswatchVerdict::NotConfigured)
        && requirement == SkauswatchRequirement::Required
    {
        return Err(CompilerError::ScanBlocked {
            reason: "skauswatch_not_configured".to_string(),
            message: "SKAUSWATCH_URL is not set; the community/prebuilt bundle intake tier \
                      requires a Skauswatch malware scan before compiling untrusted source. \
                      Set SKAUSWATCH_URL, or select SkauswatchRequirement::OptionalForDev \
                      explicitly for a first-party/dev build."
                .to_string(),
        });
    }
    Ok(())
}

/// One per-language compilation recipe. Each implementation shells out to
/// its own pinned toolchain binary (never a library call) so the
/// untrusted process boundary the toolchain itself provides is a real
/// subprocess, not an in-process call that could carry state across
/// bundles.
pub trait LanguageBuilder {
    /// Compiles `source_dir` (already scanned and manifest-validated)
    /// into a component, returning the path to the produced `.wasm` file.
    ///
    /// # Errors
    /// Returns `CompilerError::CompileFailed` if the underlying toolchain
    /// fails or is not yet wired.
    fn build(
        &self,
        source_dir: &Path,
        manifest: &BundleManifest,
        out_dir: &Path,
    ) -> Result<PathBuf, CompilerError>;
}

/// Selects the `LanguageBuilder` for a manifest's declared `language`.
fn builder_for(language: &str) -> Result<Box<dyn LanguageBuilder>, CompilerError> {
    match language {
        "python" => Ok(Box::new(python::PythonBuilder)),
        "rust" => Ok(Box::new(rust::RustBuilder)),
        "javascript" | "typescript" => Ok(Box::new(js::JsBuilder)),
        other => Err(CompilerError::Config(format!(
            "no LanguageBuilder for language {other:?}"
        ))),
    }
}

/// A JSON-serializable snapshot of a validated [`BundleManifest`], written
/// to `{out}/manifest.json` for the `publisher` container to read from
/// the shared `/work` `emptyDir`. `BundleManifest` itself (re-exported
/// from `penguin-bundle-host`) intentionally does not derive `Serialize`
/// -- constructing one bypasses validation, so the crate does not want it
/// to look like a plain data-transfer type. This snapshot is that
/// boundary: built only from an already-validated manifest.
#[derive(Debug, Serialize)]
struct ManifestSnapshot {
    schema_version: i64,
    app_id: String,
    name: String,
    version: String,
    feature: String,
    module: String,
    provider: String,
    language: String,
    artifact: String,
    execution_model: String,
    is_default: bool,
    stages: Vec<String>,
    egress: Vec<(String, Vec<String>)>,
    data_tables: Vec<String>,
    timeout_ms: i64,
    memory_mb: i64,
    egress_rps: i64,
    permissions: Vec<String>,
    routes_to: Vec<String>,
    compatible_with: Vec<String>,
    incompatible_with: Vec<String>,
}

impl From<&BundleManifest> for ManifestSnapshot {
    fn from(m: &BundleManifest) -> Self {
        Self {
            schema_version: m.schema_version,
            app_id: m.app_id.clone(),
            name: m.name.clone(),
            version: m.version.clone(),
            feature: m.feature.clone(),
            module: m.module.clone(),
            provider: m.provider.clone(),
            language: m.language.clone(),
            artifact: m.artifact.clone(),
            execution_model: m.execution_model.clone(),
            is_default: m.is_default,
            stages: m.stages.clone(),
            egress: m.egress.clone(),
            data_tables: m.data_tables.clone(),
            timeout_ms: m.timeout_ms,
            memory_mb: m.memory_mb,
            egress_rps: m.egress_rps,
            permissions: m.permissions.clone(),
            routes_to: m.routes_to.clone(),
            compatible_with: m.compatible_with.clone(),
            incompatible_with: m.incompatible_with.clone(),
        }
    }
}

/// The untrusted `build` container's entire job. Runs entirely offline on
/// bytes already present in `bundle`/`manifest_path` -- no bucket, DB, or
/// hub-api credential exists in this process's environment (enforced at
/// the Kubernetes Job level, and asserted by the crate's e2e test once
/// Task 18 lands).
///
/// # Errors
/// Returns `CompilerError::ManifestInvalid` if `manifest_path` fails
/// validation, `CompilerError::ScanBlocked` if a source scan blocks (D34
/// -- checked before any compile step below, including
/// `reason = "skauswatch_not_configured"` if `SKAUSWATCH_URL` is unset,
/// since this entry point always runs with
/// [`SkauswatchRequirement::Required`]), or `CompilerError::CompileFailed`
/// if the per-language builder fails.
pub fn run_build(bundle: &Path, manifest_path: &Path, out: &Path) -> Result<(), CompilerError> {
    // hub-api (M2b) will supply real per-tenant `ManifestOptions` once
    // wired; the default is the most conservative set (e.g.
    // `allow_prebuilt: false`) that still runs every syntax/shape rule.
    run_build_with_options(bundle, manifest_path, out, &ManifestOptions::default())
}

/// Same as [`run_build`], with the manifest validation options named by
/// `opts` rather than the conservative defaults. The seam hub-api (M2b)
/// will call once per-tenant options exist; also what this crate's own
/// tests use to exercise the `artifact: prebuilt` path, which
/// `ManifestOptions::default()`'s `allow_prebuilt: false` never reaches.
/// Always runs with [`SkauswatchRequirement::Required`] -- see
/// [`run_build_with_options_and_skauswatch_policy`] for the seam that
/// takes an explicit policy.
///
/// # Errors
/// See [`run_build`].
pub fn run_build_with_options(
    bundle: &Path,
    manifest_path: &Path,
    out: &Path,
    opts: &ManifestOptions,
) -> Result<(), CompilerError> {
    run_build_with_options_and_skauswatch_policy(
        bundle,
        manifest_path,
        out,
        opts,
        SkauswatchRequirement::Required,
    )
}

/// Same as [`run_build_with_options`], additionally naming the
/// [`SkauswatchRequirement`] policy rather than hardcoding `Required`.
/// This crate's own CLI never selects anything other than `Required`; the
/// seam exists for a hypothetical first-party/dev build path (and this
/// crate's own tests) to opt out explicitly.
///
/// # Errors
/// See [`run_build`], plus `CompilerError::ScanBlocked` with
/// `reason = "skauswatch_not_configured"` if `SKAUSWATCH_URL` is unset
/// and `skauswatch_requirement` is [`SkauswatchRequirement::Required`].
pub fn run_build_with_options_and_skauswatch_policy(
    bundle: &Path,
    manifest_path: &Path,
    out: &Path,
    opts: &ManifestOptions,
    skauswatch_requirement: SkauswatchRequirement,
) -> Result<(), CompilerError> {
    // Step 1 (D34): parse + validate the manifest. Pure text, no bundle
    // code executed.
    let m = manifest::parse_and_validate(manifest_path, opts)?;

    if m.artifact == "source" {
        // Step 2 (D34): scan inert source, in order, before any compile.
        if m.language == "python" {
            legacy_dal::scan_legacy_dal_imports(bundle)?;
        }
        sast::run_source_scans(bundle, &m.language)?;

        // Skauswatch hand-off: `SKAUSWATCH_URL` is read from env here, not
        // a CLI flag, per Global Constraints (Token & Secret Hygiene).
        let skauswatch_url = std::env::var("SKAUSWATCH_URL").ok();
        let verdict = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| CompilerError::Config(format!("cannot start async runtime: {e}")))?
            .block_on(skauswatch::scan_with_skauswatch(
                bundle,
                skauswatch_url.as_deref(),
            ))?;
        // Fail closed on an unconfigured scanner for the community/
        // prebuilt tier (D34, security-review finding) -- see
        // `skauswatch_gate`'s doc comment.
        skauswatch_gate(&verdict, skauswatch_requirement)?;
        tracing::info!(?verdict, "skauswatch verdict recorded");

        // Step 3: only after every scan above has passed does bundle code
        // actually run, inside the per-language builder.
        std::fs::create_dir_all(out)?;
        let builder = builder_for(&m.language)?;
        let wasm_path = builder.build(bundle, &m, out)?;
        let component_path = out.join("component.wasm");
        if wasm_path != component_path {
            std::fs::copy(&wasm_path, &component_path)?;
        }
    } else {
        // artifact: prebuilt -- nothing to compile; `publisher` does all
        // real validation of a Tier 2 upload directly from bytes.
        std::fs::create_dir_all(out)?;
        std::fs::copy(bundle, out.join("component.wasm"))?;
    }

    let snapshot = ManifestSnapshot::from(&m);
    let manifest_json =
        serde_json::to_vec_pretty(&snapshot).map_err(|e| CompilerError::Config(e.to_string()))?;
    std::fs::write(out.join("manifest.json"), manifest_json)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn builder_for_selects_the_right_language() {
        assert!(builder_for("python").is_ok());
        assert!(builder_for("rust").is_ok());
        assert!(builder_for("javascript").is_ok());
        assert!(builder_for("typescript").is_ok());
    }

    #[test]
    fn builder_for_rejects_an_unrecognized_language() {
        match builder_for("cobol") {
            Err(CompilerError::Config(_)) => {}
            other => panic!("expected Err(Config(_)), got a builder: {}", other.is_ok()),
        }
    }

    // Security-review regression coverage (LOW finding): an unconfigured
    // Skauswatch scanner must block the build for the default/community
    // tier, never silently proceed. Exercised as a pure unit test against
    // `skauswatch_gate` directly (rather than the full `run_build`
    // pipeline) because the pipeline also requires a working real
    // `semgrep` install, which is not guaranteed on every host this
    // suite runs on -- see `tests/fixtures/bin/semgrep-stub.sh`'s header
    // comment for the same tradeoff made elsewhere in this crate.

    #[test]
    fn not_configured_blocks_by_default() {
        let err = skauswatch_gate(
            &skauswatch::SkauswatchVerdict::NotConfigured,
            SkauswatchRequirement::Required,
        )
        .unwrap_err();
        match err {
            CompilerError::ScanBlocked { reason, .. } => {
                assert_eq!(reason, "skauswatch_not_configured");
            }
            other => panic!("expected ScanBlocked(skauswatch_not_configured), got {other:?}"),
        }
    }

    #[test]
    fn not_configured_is_tolerated_under_explicit_dev_opt_out() {
        skauswatch_gate(
            &skauswatch::SkauswatchVerdict::NotConfigured,
            SkauswatchRequirement::OptionalForDev,
        )
        .expect("OptionalForDev must tolerate an unconfigured scanner");
    }

    #[test]
    fn configured_verdicts_never_block_regardless_of_requirement() {
        for verdict in [
            skauswatch::SkauswatchVerdict::Pass,
            skauswatch::SkauswatchVerdict::Warn { findings: 3 },
        ] {
            skauswatch_gate(&verdict, SkauswatchRequirement::Required)
                .expect("a configured verdict must never be blocked by the gate itself");
            skauswatch_gate(&verdict, SkauswatchRequirement::OptionalForDev)
                .expect("a configured verdict must never be blocked by the gate itself");
        }
    }

    #[test]
    fn skauswatch_requirement_default_is_required() {
        assert_eq!(
            SkauswatchRequirement::default(),
            SkauswatchRequirement::Required
        );
    }
}
