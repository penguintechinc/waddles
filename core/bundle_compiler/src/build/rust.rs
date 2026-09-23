//! Rust build recipe: `cargo component build --target wasm32-wasip2`
//! (spec SS9.3, M2a plan Task 9).
//!
//! **STUBBED THIS WAVE.** See `python.rs`'s module doc comment -- same
//! reasoning applies here: this placeholder preserves the
//! `LanguageBuilder` seam and fails loudly rather than silently.

use super::LanguageBuilder;
use crate::errors::CompilerError;
use crate::manifest::BundleManifest;
use std::path::{Path, PathBuf};

/// Placeholder Rust [`LanguageBuilder`]. See module doc comment.
pub struct RustBuilder;

impl LanguageBuilder for RustBuilder {
    fn build(
        &self,
        _source_dir: &Path,
        _manifest: &BundleManifest,
        _out_dir: &Path,
    ) -> Result<PathBuf, CompilerError> {
        Err(CompilerError::CompileFailed {
            language: "rust".to_string(),
            message: "cargo component recipe not yet wired -- see M2a plan Task 9".to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::manifest::{parse_and_validate, ManifestOptions};

    #[test]
    fn stub_always_fails_loudly() {
        let m = parse_and_validate(
            Path::new("tests/fixtures/manifests/valid_rust.yaml"),
            &ManifestOptions::default(),
        )
        .unwrap();
        let err = RustBuilder
            .build(
                Path::new("tests/fixtures/bundles/scan-clean"),
                &m,
                Path::new("/tmp"),
            )
            .unwrap_err();
        match err {
            CompilerError::CompileFailed { language, .. } => assert_eq!(language, "rust"),
            other => panic!("expected CompileFailed, got {other:?}"),
        }
    }
}
