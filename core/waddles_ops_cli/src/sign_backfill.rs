//! `sign-approved-bundles`: signs every already-approved `app_versions` row missing a signature.
//!
//! Port of `hub_api/cli/sign_approved_bundles.py`. Selection, the one-transaction DB write
//! (digest re-read -> sign -> UPDATE, rolled back whole on any failure), the post-commit sidecar
//! upload, per-row failure counting and the exit code all match the Python:
//!
//! * exit `0` -- every row signed (or nothing to do), `1` -- at least one row failed
//!   (sign error, or signed-but-sidecar-upload-failed), `3` -- the run could not start at all
//!   (database unreachable / S3 misconfigured; Python crashed with a traceback, exit 1).

use object_store::ObjectStore;
use sqlx::{PgPool, Row};

use crate::db::{describe_sqlx_error, DbError};
use crate::signing::PlatformSigner;
use crate::storage::upload_signed_sidecar;

/// Same SQL as the Python selection (one row per `app_versions.id`, any one current approval).
pub const SELECT_UNSIGNED_APPROVED_VERSIONS_SQL: &str = "\
SELECT v.id::bigint AS version_id, v.app_id AS app_id, v.version AS version, MIN(a.id)::bigint AS approval_id \
FROM app_versions v \
JOIN app_install_approvals a \
  ON a.app_id = v.app_id AND a.version = v.version AND a.superseded_by IS NULL \
WHERE v.artifact_digest IS NOT NULL \
  AND v.artifact_signature IS NULL \
GROUP BY v.id, v.app_id, v.version \
ORDER BY v.id ASC";

/// One unsigned-but-approved version.
#[derive(Debug, Clone)]
pub struct PendingRow {
    pub version_id: i64,
    pub app_id: String,
    pub version: String,
    pub approval_id: i64,
}

/// Outcome of one row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Signed,
    SignedSidecarUploadFailed,
    Failed,
}

impl Outcome {
    /// Stable label used in logs.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Signed => "signed",
            Self::SignedSidecarUploadFailed => "signed_sidecar_upload_failed",
            Self::Failed => "failed",
        }
    }
}

/// Totals for one run (the denominators that make "clean" mean something).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Summary {
    pub examined: usize,
    pub signed: usize,
    pub failures: usize,
}

impl Summary {
    /// Process exit code: 0 clean, 1 any row failed.
    pub fn exit_code(&self) -> u8 {
        u8::from(self.failures > 0)
    }
}

/// Selects the rows needing a signature.
pub async fn select_unsigned_approved_versions(pool: &PgPool) -> Result<Vec<PendingRow>, DbError> {
    let rows = sqlx::query(SELECT_UNSIGNED_APPROVED_VERSIONS_SQL)
        .fetch_all(pool)
        .await?;
    rows.iter()
        .map(|r| {
            Ok(PendingRow {
                version_id: r.try_get("version_id")?,
                app_id: r.try_get("app_id")?,
                version: r.try_get("version")?,
                approval_id: r.try_get("approval_id")?,
            })
        })
        .collect::<Result<Vec<_>, sqlx::Error>>()
        .map_err(DbError::from)
}

/// Signs and records one row inside ONE transaction; returns the sidecar document.
///
/// A missing digest, missing/invalid key, or any SQL failure aborts and rolls back the whole
/// transaction (dropping the `Transaction` without commit rolls back), so a failed row never
/// leaves a half-written signature.
pub async fn sign_and_record(
    pool: &PgPool,
    row: &PendingRow,
) -> Result<crate::signing::SignedSidecar, String> {
    let mut tx = pool.begin().await.map_err(|e| describe_sqlx_error(&e))?;
    let digest: Option<String> =
        sqlx::query_scalar("SELECT artifact_digest FROM app_versions WHERE id = $1")
            .bind(row.version_id)
            .fetch_optional(&mut *tx)
            .await
            .map_err(|e| describe_sqlx_error(&e))?
            .flatten();
    let Some(digest) = digest else {
        return Err(format!(
            "missing_digest_for_signing: app_versions.id={} has no artifact_digest to sign",
            row.version_id
        ));
    };
    let signer = PlatformSigner::from_env().map_err(|e| e.to_string())?;
    let doc = signer
        .sign(&row.app_id, &row.version, &digest, row.approval_id)
        .map_err(|e| e.to_string())?;
    sqlx::query(
        "UPDATE app_versions SET artifact_signature = $1, artifact_signature_key_id = $2, \
         artifact_signed_approval_id = $3, artifact_signed_at = now() WHERE id = $4",
    )
    .bind(&doc.signature)
    .bind(&doc.key_id)
    .bind(doc.approval_id)
    .bind(row.version_id)
    .execute(&mut *tx)
    .await
    .map_err(|e| describe_sqlx_error(&e))?;
    tx.commit().await.map_err(|e| describe_sqlx_error(&e))?;
    Ok(doc)
}

/// Signs one row then uploads its sidecar (upload failure is this row's outcome, never raised).
pub async fn sign_one(pool: &PgPool, store: &dyn ObjectStore, row: &PendingRow) -> Outcome {
    let doc = match sign_and_record(pool, row).await {
        Ok(d) => d,
        Err(e) => {
            tracing::error!(app_id = %row.app_id, version = %row.version, error = %e,
                "sign-approved-bundles: row failed");
            return Outcome::Failed;
        }
    };
    match upload_signed_sidecar(store, &doc).await {
        Ok(key) => {
            tracing::info!(app_id = %row.app_id, version = %row.version, key = %key,
                key_id = %doc.key_id, "bundle signing: signed sidecar uploaded");
            Outcome::Signed
        }
        Err(e) => {
            tracing::error!(app_id = %row.app_id, version = %row.version, error = %e,
                "sign-approved-bundles: sidecar upload failed after the signature committed");
            Outcome::SignedSidecarUploadFailed
        }
    }
}

/// Runs the whole backfill and returns the totals.
pub async fn run(pool: &PgPool, store: &dyn ObjectStore) -> Result<Summary, DbError> {
    let rows = select_unsigned_approved_versions(pool).await?;
    tracing::info!(
        unsigned_approved_versions_examined = rows.len(),
        "sign-approved-bundles: starting"
    );
    let mut signed = 0usize;
    let mut failures = 0usize;
    for row in &rows {
        let outcome = sign_one(pool, store, row).await;
        tracing::info!(app_id = %row.app_id, version = %row.version,
            outcome = outcome.as_str(), "sign-approved-bundles: result");
        if outcome == Outcome::Signed {
            signed += 1;
        } else {
            failures += 1;
        }
    }
    let summary = Summary {
        examined: rows.len(),
        signed,
        failures,
    };
    tracing::info!(
        examined = summary.examined,
        failures = summary.failures,
        "sign-approved-bundles: summary"
    );
    Ok(summary)
}
