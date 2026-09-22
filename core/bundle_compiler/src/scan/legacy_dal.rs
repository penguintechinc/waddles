//! Rejects any Python bundle source still importing `flask_core.database`
//! or `pydal` (D21b) -- the compiler-side backstop for the M1.5 DAL
//! migration. Runs inside the untrusted `build` container: pure text
//! scanning, no bundle code executed here.

use crate::errors::CompilerError;
use regex::Regex;
use std::path::Path;
use std::sync::LazyLock;
use walkdir::WalkDir;

/// Matches a top-level `from flask_core.database import ...` / `import
/// pydal` style statement. Infallible: a literal regex compiled once.
#[allow(clippy::expect_used)]
static LEGACY_IMPORT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?m)^\s*(from\s+(flask_core\.database|pydal)\b|import\s+(flask_core\.database|pydal)\b)",
    )
    .expect("legacy DAL import regex is a fixed literal, compiled once at process startup")
});

/// Scans every `.py` file under `source_dir` for a `flask_core.database`
/// or `pydal` import.
///
/// # Errors
/// Returns `CompilerError::ScanBlocked` with `reason = "legacy_dal_import"`
/// on the first match, or `reason = "scan_empty_denominator"` if zero
/// `.py` files were found -- an empty scan is a failure, never a silent
/// pass. Returns `CompilerError::Io` if a matched file cannot be read.
pub fn scan_legacy_dal_imports(source_dir: &Path) -> Result<usize, CompilerError> {
    let mut scanned = 0usize;
    for entry in WalkDir::new(source_dir).into_iter().filter_map(Result::ok) {
        if !entry.file_type().is_file()
            || entry.path().extension().and_then(|e| e.to_str()) != Some("py")
        {
            continue;
        }
        scanned += 1;
        let contents = std::fs::read_to_string(entry.path())?;
        if let Some(m) = LEGACY_IMPORT_RE.find(&contents) {
            let line_no = contents[..m.start()].matches('\n').count() + 1;
            return Err(CompilerError::ScanBlocked {
                reason: "legacy_dal_import".to_string(),
                message: format!(
                    "{}:{}: imports a legacy DAL module ({}) -- migrate to the penguin_dal-compatible \
                     waddle_sdk.db facade (sdk/waddle-sdk/src/waddle_sdk/db.py)",
                    entry.path().display(),
                    line_no,
                    m.as_str().trim(),
                ),
            });
        }
    }
    if scanned == 0 {
        return Err(CompilerError::ScanBlocked {
            reason: "scan_empty_denominator".to_string(),
            message: format!(
                "legacy DAL scan examined 0 .py files under {}",
                source_dir.display()
            ),
        });
    }
    Ok(scanned)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn regex_matches_from_import() {
        assert!(LEGACY_IMPORT_RE.is_match("from flask_core.database import AsyncDAL\n"));
    }

    #[test]
    fn regex_matches_bare_pydal_import() {
        assert!(LEGACY_IMPORT_RE.is_match("import pydal\n"));
    }

    #[test]
    fn regex_does_not_match_unrelated_import() {
        assert!(!LEGACY_IMPORT_RE.is_match("from waddle_sdk.db import get_bundle_dal\n"));
    }
}
