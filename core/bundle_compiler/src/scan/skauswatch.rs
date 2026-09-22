//! Hand-off to PenguinTech's own scanner (Skauswatch), when configured.
//! `SKAUSWATCH_URL` unset means "not configured", reported as such --
//! never silently treated as a pass (spec SS9.3). Runs inside the
//! untrusted `build` container; the source-tree fingerprint below is a
//! dedup key for Skauswatch's own request, NOT the artifact digest -- the
//! artifact digest is computed exactly once, by the `publisher` container,
//! per the M2a plan's Artifact & Digest Contract.

use crate::errors::CompilerError;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::path::Path;

/// The outcome of asking Skauswatch to scan a bundle's source tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SkauswatchVerdict {
    /// `SKAUSWATCH_URL` was not set -- reportable, distinct from a pass.
    NotConfigured,
    /// Skauswatch scanned the source and found nothing.
    Pass,
    /// Skauswatch found something worth recording but not blocking.
    Warn {
        /// Number of findings Skauswatch reported.
        findings: usize,
    },
    /// Skauswatch found something that must block the build.
    Fail {
        /// Skauswatch's own reason string.
        reason: String,
    },
}

/// Request body sent to Skauswatch's `/api/v1/scan`.
#[derive(Debug, Serialize)]
struct ScanRequest {
    source_fingerprint: String,
    language: String,
}

/// Response body Skauswatch's `/api/v1/scan` returns.
#[derive(Debug, Deserialize)]
struct ScanResponse {
    verdict: String,
    reason: Option<String>,
    findings: Option<usize>,
}

/// Computes a deterministic fingerprint over every file in `dir`, sorted
/// by relative path -- a dedup key for Skauswatch's own request only.
/// This is NOT the artifact digest.
fn source_tree_fingerprint(dir: &Path) -> Result<String, CompilerError> {
    let mut hasher = Sha256::new();
    let mut paths: Vec<_> = walkdir::WalkDir::new(dir)
        .into_iter()
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_file())
        .map(|e| e.path().to_path_buf())
        .collect();
    paths.sort();
    for path in paths {
        hasher.update(std::fs::read(&path)?);
    }
    Ok(format!("sha256:{}", hex::encode(hasher.finalize())))
}

/// POSTs the bundle source tree's fingerprint to Skauswatch's
/// `/api/v1/scan` and classifies the verdict.
///
/// # Errors
/// A network or response-parsing failure is always `Err` (never mistaken
/// for `NotConfigured`, which only means `base_url` was `None`). Returns
/// `CompilerError::ScanBlocked` with `reason = "skauswatch_fail"` if
/// Skauswatch itself reports a fail verdict.
pub async fn scan_with_skauswatch(
    source_dir: &Path,
    base_url: Option<&str>,
) -> Result<SkauswatchVerdict, CompilerError> {
    let Some(base_url) = base_url else {
        return Ok(SkauswatchVerdict::NotConfigured);
    };

    let fingerprint = source_tree_fingerprint(source_dir)?;
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{base_url}/api/v1/scan"))
        .json(&ScanRequest {
            source_fingerprint: fingerprint,
            language: "unknown".to_string(),
        })
        .send()
        .await
        .map_err(|e| CompilerError::ScanBlocked {
            reason: "skauswatch_unreachable".to_string(),
            message: format!("Skauswatch request failed: {e}"),
        })?;
    let parsed: ScanResponse = resp.json().await.map_err(|e| CompilerError::ScanBlocked {
        reason: "skauswatch_bad_response".to_string(),
        message: format!("Skauswatch returned an unparseable response: {e}"),
    })?;

    match parsed.verdict.as_str() {
        "pass" => Ok(SkauswatchVerdict::Pass),
        "warn" => Ok(SkauswatchVerdict::Warn {
            findings: parsed.findings.unwrap_or(0),
        }),
        "fail" => Err(CompilerError::ScanBlocked {
            reason: "skauswatch_fail".to_string(),
            message: parsed
                .reason
                .unwrap_or_else(|| "no reason given".to_string()),
        }),
        other => Err(CompilerError::ScanBlocked {
            reason: "skauswatch_bad_response".to_string(),
            message: format!("unknown verdict {other:?}"),
        }),
    }
}
