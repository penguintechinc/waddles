//! `bundle-compiler`'s library surface. Two entry points map to the two
//! containers of the Kubernetes Job that builds and publishes one bundle
//! version (M2a plan Artifact & Digest Contract): [`build::run_build`] is
//! called by the untrusted `build` initContainer; `run_publish` is called
//! by the trusted `publisher` container. They never call into each
//! other's credentialed or bundle-code-executing paths.
//!
//! **This wave's scope:** the normative WIT world (`wit/waddle-bundle/`,
//! outside this crate), manifest validation (delegated to the landed
//! `penguin-bundle-host` crate, see `manifest`), and the D34-ordered
//! source-scanning pipeline (`scan`, wired into `build::run_build`) are
//! fully implemented and tested. `validate`, `artifact`, `sidecar`,
//! `bucket`, `db`, and `callback` -- the publisher-side re-validation,
//! signing, upload, and DB/callback steps -- are stubbed with TODOs; see
//! each module's doc comment and the M2a plan's Tasks 11-18.

/// Publisher-only digest computation (component + `.cwasm`). Stubbed.
pub mod artifact;
/// S3-compatible bucket upload client. Stubbed.
pub mod bucket;
/// The untrusted `build` initContainer's pipeline: manifest validation,
/// ordered source scanning, per-language compilation.
pub mod build;
/// hub-api notification client. Stubbed.
pub mod callback;
/// `waddles_publisher` Postgres client. Stubbed.
pub mod db;
/// Every failure mode `bundle-compiler` can exit with.
pub mod errors;
/// Structured JSON logging via `tracing`.
pub mod logging;
/// `bundle.yaml` v2 parsing and validation, via `penguin-bundle-host`.
pub mod manifest;
/// Security scanning orchestration (legacy-DAL, SAST/dependency-audit/
/// secrets, Skauswatch).
pub mod scan;
/// Ed25519 sidecar signing. Stubbed.
pub mod sidecar;
/// Component import/export validation. Stubbed.
pub mod validate;

use errors::CompilerError;
use std::path::Path;

/// The trusted `publisher` container's entire job: re-validate the
/// component from bytes, compute both digests, sign the sidecar, upload
/// to the bucket, write `app_versions`, notify hub-api.
///
/// **STUBBED THIS WAVE.** See the M2a plan's Task 18, which composes
/// Tasks 11-17 into this function's real body.
///
/// # Errors
/// Always returns `CompilerError::Config` until Task 18 lands.
pub fn run_publish(
    _component: &Path,
    _manifest: &Path,
    _language: &str,
    _artifact_kind: &str,
) -> Result<(), CompilerError> {
    Err(CompilerError::Config(
        "publish pipeline not yet wired -- see M2a plan Task 18".to_string(),
    ))
}
