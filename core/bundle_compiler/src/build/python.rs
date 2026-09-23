//! Python build recipe: `componentize-py --stub-wasi` + pre-import
//! generation (spec SS9.3, M2a plan Task 8).
//!
//! **STUBBED THIS WAVE.** Wiring the real `componentize-py` invocation
//! (and the `sdk/waddle-sdk` package it depends on) is out of scope for
//! this pass -- see the M2a plan's Task 8 for the full recipe. This
//! placeholder preserves the `LanguageBuilder` seam so `build::run_build`
//! composes correctly today, and fails loudly (`CompileFailed`, never a
//! silent no-op) rather than pretending to produce a component.

use super::LanguageBuilder;
use crate::errors::CompilerError;
use crate::manifest::BundleManifest;
use std::path::{Path, PathBuf};

/// Placeholder Python [`LanguageBuilder`]. See module doc comment.
pub struct PythonBuilder;

impl LanguageBuilder for PythonBuilder {
    fn build(
        &self,
        _source_dir: &Path,
        _manifest: &BundleManifest,
        _out_dir: &Path,
    ) -> Result<PathBuf, CompilerError> {
        Err(CompilerError::CompileFailed {
            language: "python".to_string(),
            message: "componentize-py recipe not yet wired -- see M2a plan Task 8".to_string(),
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
            Path::new("tests/fixtures/manifests/valid_python.yaml"),
            &ManifestOptions::default(),
        )
        .unwrap();
        let err = PythonBuilder
            .build(
                Path::new("tests/fixtures/bundles/scan-clean"),
                &m,
                Path::new("/tmp"),
            )
            .unwrap_err();
        match err {
            CompilerError::CompileFailed { language, .. } => assert_eq!(language, "python"),
            other => panic!("expected CompileFailed, got {other:?}"),
        }
    }
}
