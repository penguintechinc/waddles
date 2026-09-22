//! `bundle.yaml` v2 parsing and validation, delegated entirely to the
//! landed `penguin-bundle-host::manifest` crate (31 rules, spec SS6.4.4).
//!
//! The M2a plan (`docs/superpowers/plans/2026-09-14-rust-data-plane-m2a-compiler-sdks.md`)
//! was written under Plan-Level Assumption PA1: no `penguin-bundle-host`
//! crate existed yet, so the plan's Task 3 vendors its own copy of the
//! validator, with a documented follow-up to switch to the crate once it
//! landed. That crate has since landed (`penguin-libs`
//! `release/rust-bundle-host/v0.1.x`), so this module implements PA1's
//! follow-up directly rather than vendoring a copy that would immediately
//! be dead code: it is a thin adapter from a file path and this crate's
//! own owned `ManifestOptions` to `penguin_bundle_host::manifest`'s
//! borrowing `ValidationContext`, and from its `ManifestError`/`Reason`
//! to this crate's `CompilerError::ManifestInvalid`.

use crate::errors::CompilerError;
use penguin_bundle_host::manifest::{parse_yaml, ParseError, ValidationContext};
use std::collections::HashSet;
use std::path::Path;

/// A validated `bundle.yaml` v2 manifest. Re-exported directly from
/// `penguin-bundle-host` -- this crate never re-derives or duplicates its
/// fields.
pub use penguin_bundle_host::manifest::Manifest as BundleManifest;

/// Owned, `Default`-able validation options. Mirrors
/// `penguin_bundle_host::manifest::ValidationContext` field-for-field but
/// owns its data, so callers (CLI args today; hub-api-supplied per-tenant
/// config once M2b wires it) do not need to manage the context's
/// borrows themselves.
#[derive(Debug, Clone, Default)]
pub struct ManifestOptions {
    /// Global `bundles.allow_prebuilt` setting (V18).
    pub allow_prebuilt: bool,
    /// Tenant-level egress host denylist (V21).
    pub egress_denylist: Vec<String>,
    /// Tenant setting `allow_wildcard_consumes` (V30).
    pub allow_wildcard_consumes: bool,
    /// Platform names registered as `custom:<name>` for this tenant (V29).
    pub custom_platforms: Vec<String>,
    /// `app_id`s registered in `app_catalog` for this tenant (V30a);
    /// `None` skips the existence half of that rule.
    pub known_app_ids: Option<HashSet<String>>,
    /// Operator-raised ceiling for `limits.egress_rps` (V24); `None` uses
    /// the spec default.
    pub egress_rps_ceiling: Option<i64>,
}

impl ManifestOptions {
    /// Borrows this crate's owned options into the shape
    /// `penguin_bundle_host::manifest::validate` requires. `component_exports`
    /// and `component_imports` are always `None` here: this function runs
    /// in the untrusted `build` container, before any component exists to
    /// inspect (V25/V31 are artifact-level checks Task 11/12 run later,
    /// against the compiled bytes).
    fn as_validation_context(&self) -> ValidationContext<'_> {
        ValidationContext {
            allow_prebuilt: self.allow_prebuilt,
            egress_denylist: &self.egress_denylist,
            allow_wildcard_consumes: self.allow_wildcard_consumes,
            custom_platforms: &self.custom_platforms,
            known_app_ids: self.known_app_ids.as_ref(),
            component_exports: None,
            component_imports: None,
            egress_rps_ceiling: self.egress_rps_ceiling,
        }
    }
}

/// Reads `bundle.yaml` v2 source text from `path` and parses+validates it
/// in one step via `penguin_bundle_host::manifest::parse_yaml`. Pure text
/// parsing -- no bundle code executes here, so this is safe to call from
/// the untrusted `build` container before any scan or compile step.
///
/// # Errors
/// Returns `CompilerError::ManifestInvalid` on a YAML syntax error or on
/// the first validation rule that fails (`penguin-bundle-host` validates
/// in ascending rule-number order and stops at the first failure).
/// Returns `CompilerError::Io` if `path` cannot be read.
pub fn parse_and_validate(
    path: &Path,
    opts: &ManifestOptions,
) -> Result<BundleManifest, CompilerError> {
    let source = std::fs::read_to_string(path)?;
    let ctx = opts.as_validation_context();
    parse_yaml(&source, &ctx).map_err(|err| match err {
        ParseError::Syntax(yaml_err) => CompilerError::ManifestInvalid {
            reason: "invalid_yaml".to_string(),
            message: yaml_err.to_string(),
        },
        ParseError::Validation(manifest_err) => CompilerError::ManifestInvalid {
            reason: manifest_err.reason.as_str().to_string(),
            message: manifest_err.detail,
        },
    })
}
