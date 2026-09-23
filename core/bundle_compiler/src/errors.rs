//! Every failure mode `bundle-compiler` can exit with, and the exit code
//! each one maps to. The Kubernetes Job's status is machine-readable
//! through the exit code, not just stderr (spec SS4.6).

use thiserror::Error;

/// Every top-level failure `bundle-compiler` can produce, mapped 1:1 to a
/// process exit code so the Kubernetes Job's status is machine-readable.
#[derive(Debug, Error)]
pub enum CompilerError {
    /// The `bundle.yaml` v2 manifest failed one of `penguin-bundle-host`'s
    /// 31 validation rules (or was not valid YAML at all).
    #[error("manifest invalid: {reason}: {message}")]
    ManifestInvalid {
        /// The stable, machine-checkable reason code (e.g. `bad_semver`).
        reason: String,
        /// Human-readable detail. May change without notice; never assert
        /// on this in a test, only on `reason`.
        message: String,
    },

    /// A source scan (legacy-DAL import check, SAST/dependency-audit/
    /// secrets, or Skauswatch) blocked the build before any bundle code
    /// ran (D34).
    #[error("security scan blocked the build: {reason}: {message}")]
    ScanBlocked {
        /// The stable, machine-checkable reason code (e.g. `secrets_found`).
        reason: String,
        /// Human-readable detail.
        message: String,
    },

    /// The per-language toolchain (`componentize-py`, `cargo component`,
    /// `jco`) failed to produce a component.
    #[error("compilation failed for language {language}: {message}")]
    CompileFailed {
        /// The manifest's declared `language`.
        language: String,
        /// The toolchain's own error output or failure description.
        message: String,
    },

    /// The compiled component's actual WASI/WIT imports failed the
    /// per-language allowlist, or an export a declared stage requires is
    /// missing (V25/V31).
    #[error("component validation failed: {reason}: {message}")]
    ValidationFailed {
        /// The stable, machine-checkable reason code.
        reason: String,
        /// Human-readable detail.
        message: String,
    },

    /// Digest computation, Ed25519 signing, or the bucket upload failed.
    #[error("artifact signing or upload failed: {0}")]
    ArtifactFailed(String),

    /// The `waddles_publisher` Postgres write to `app_versions` failed.
    #[error("database write failed: {0}")]
    DbFailed(String),

    /// The hub-api artifact-ready notification callback failed.
    #[error("hub-api callback failed: {0}")]
    CallbackFailed(String),

    /// A configuration error not attributable to any pipeline phase above
    /// (missing env var, unwired module, bad CLI argument combination).
    #[error("configuration error: {0}")]
    Config(String),

    /// An underlying I/O failure (reading the bundle source tree, writing
    /// the output directory, etc.).
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

impl CompilerError {
    /// The process exit code this error maps to. `78` (`EX_CONFIG`) and
    /// `74` (`EX_IOERR`) match the BSD sysexits.h convention the other
    /// Rust data-plane services in this repo use for the same failure
    /// classes.
    #[must_use]
    pub fn exit_code(&self) -> i32 {
        match self {
            CompilerError::ManifestInvalid { .. } => 1,
            CompilerError::ScanBlocked { .. } => 2,
            CompilerError::CompileFailed { .. } => 3,
            CompilerError::ValidationFailed { .. } => 4,
            CompilerError::ArtifactFailed(_) => 5,
            CompilerError::DbFailed(_) => 6,
            CompilerError::CallbackFailed(_) => 7,
            CompilerError::Config(_) => 78,
            CompilerError::Io(_) => 74,
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn every_variant_maps_to_its_documented_exit_code() {
        let cases: Vec<(CompilerError, i32)> = vec![
            (
                CompilerError::ManifestInvalid {
                    reason: "x".to_string(),
                    message: "x".to_string(),
                },
                1,
            ),
            (
                CompilerError::ScanBlocked {
                    reason: "x".to_string(),
                    message: "x".to_string(),
                },
                2,
            ),
            (
                CompilerError::CompileFailed {
                    language: "x".to_string(),
                    message: "x".to_string(),
                },
                3,
            ),
            (
                CompilerError::ValidationFailed {
                    reason: "x".to_string(),
                    message: "x".to_string(),
                },
                4,
            ),
            (CompilerError::ArtifactFailed("x".to_string()), 5),
            (CompilerError::DbFailed("x".to_string()), 6),
            (CompilerError::CallbackFailed("x".to_string()), 7),
            (CompilerError::Config("x".to_string()), 78),
            (CompilerError::Io(std::io::Error::other("x")), 74),
        ];
        for (err, expected) in cases {
            assert_eq!(err.exit_code(), expected, "exit code for {err:?}");
        }
    }

    #[test]
    fn display_messages_include_context() {
        let err = CompilerError::ManifestInvalid {
            reason: "bad_semver".to_string(),
            message: "version is not semver".to_string(),
        };
        assert!(err.to_string().contains("bad_semver"));
        assert!(err.to_string().contains("version is not semver"));
    }
}
