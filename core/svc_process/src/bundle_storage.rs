//! Startup wiring for the bundle `object-storage` host capability (spec
//! 2026-11-06-bundle-object-storage).
//!
//! The chart (`waddlebot.bundleStorage.env`) renders five `BUNDLE_STORAGE_*`
//! variables into the host-stage pods. This module turns them into the
//! scope-enforcing [`StorageHost`] (one bucket, a prefix per bundle, grant-gated
//! cross-prefix access, host-side tag filtering -- all inside `bundle-host-storage`)
//! and wraps it in the [`ObjectStorageWiring`] that `StageCapabilities::
//! with_object_storage` consumes.
//!
//! Three explicit states, never a silent one:
//! - none of the five set: capability DISABLED -- a WARN log plus the
//!   `waddles_bundle_storage_capability_provisioned` gauge at 0;
//! - all five set: capability PROVISIONED (gauge 1);
//! - some set, some missing/empty, or the endpoint is rejected: startup FAILS.

use std::fmt;
use std::sync::Arc;

use bundle_host_storage::pg_audit::{self, AuditConnectConfig};
use bundle_host_storage::s3::{S3Backend, S3Config};
use bundle_host_storage::{AuditSink, PgAuditSink, StorageHost, TracingAuditSink};

use crate::capabilities::ObjectStorageWiring;
use crate::config::Secret;
use crate::license::BundleObjectStorageCapabilityGate;

/// The five variables the chart renders, in a stable order.
pub const REQUIRED_ENV: [&str; 5] = [
    "BUNDLE_STORAGE_ENDPOINT",
    "BUNDLE_STORAGE_BUCKET",
    "BUNDLE_STORAGE_REGION",
    "BUNDLE_STORAGE_ACCESS_KEY_ID",
    "BUNDLE_STORAGE_SECRET_ACCESS_KEY",
];

/// Optional password of the INSERT-only `waddles_bundle_storage_audit` role. Unset
/// falls back to the structured-tracing audit sink (logged, not durable).
pub const AUDIT_PASSWORD_ENV: &str = "BUNDLE_STORAGE_AUDIT_PASSWORD";

/// Default INSERT-only audit role (`alembic/versions/0080_bundle_storage_audit.py`).
pub const AUDIT_ROLE: &str = "waddles_bundle_storage_audit";

/// Parsed, validated `BUNDLE_STORAGE_*` settings. Credentials are redacted in `Debug`.
#[derive(Clone)]
pub struct StorageSettings {
    pub endpoint: String,
    pub bucket: String,
    pub region: String,
    pub access_key_id: Secret,
    pub secret_access_key: Secret,
}

impl fmt::Debug for StorageSettings {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("StorageSettings")
            .field("endpoint", &self.endpoint)
            .field("bucket", &self.bucket)
            .field("region", &self.region)
            .field("access_key_id", &self.access_key_id)
            .field("secret_access_key", &self.secret_access_key)
            .finish()
    }
}

/// Why startup must abort.
#[derive(Debug, PartialEq, Eq)]
pub enum StorageConfigError {
    /// Some of the five variables are set, others missing or empty.
    Partial { missing: Vec<&'static str> },
    /// All five are set but the S3 client or audit sink rejects them.
    Invalid(String),
}

impl fmt::Display for StorageConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Partial { missing } => write!(
                f,
                "bundle object storage is partially configured; missing or empty: {} \
                 (set all of {} or none)",
                missing.join(", "),
                REQUIRED_ENV.join(", ")
            ),
            Self::Invalid(why) => write!(f, "bundle object storage config rejected: {why}"),
        }
    }
}

impl std::error::Error for StorageConfigError {}

/// Classifies the environment: `Ok(None)` = fully unset (disabled), `Ok(Some)` =
/// complete, `Err(Partial)` = some but not all. Empty counts as unset. `get` is
/// injected so tests need no process-env mutation.
pub fn parse_settings(
    get: impl Fn(&str) -> Option<String>,
) -> Result<Option<StorageSettings>, StorageConfigError> {
    let vals: Vec<Option<String>> = REQUIRED_ENV
        .iter()
        .map(|k| {
            get(k)
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
        })
        .collect();
    if vals.iter().all(Option::is_none) {
        return Ok(None);
    }
    let missing: Vec<&'static str> = REQUIRED_ENV
        .iter()
        .zip(&vals)
        .filter(|(_, v)| v.is_none())
        .map(|(k, _)| *k)
        .collect();
    if !missing.is_empty() {
        return Err(StorageConfigError::Partial { missing });
    }
    let mut it = vals.into_iter().flatten();
    let mut next = || it.next().unwrap_or_default();
    let settings = StorageSettings {
        endpoint: next(),
        bucket: next(),
        region: next(),
        access_key_id: Secret::new(next()),
        secret_access_key: Secret::new(next()),
    };
    // Bundle data is sensitive (security.md Encryption): TLS only, mirroring the chart's
    // own `https://` guard for external storage.
    if !settings.endpoint.starts_with("https://") {
        return Err(StorageConfigError::Invalid(
            "BUNDLE_STORAGE_ENDPOINT must be https://".to_string(),
        ));
    }
    // No network call: fails on a bad endpoint/CA (S3_CA_FILE).
    build_backend(&settings)?;
    Ok(Some(settings))
}

fn build_backend(s: &StorageSettings) -> Result<S3Backend, StorageConfigError> {
    S3Backend::new(&S3Config {
        endpoint: s.endpoint.clone(),
        bucket: s.bucket.clone(),
        region: s.region.clone(),
        access_key_id: s.access_key_id.expose().to_string(),
        secret_access_key: s.secret_access_key.expose().to_string(),
    })
    .map_err(|e| StorageConfigError::Invalid(e.to_string()))
}

/// Startup gate: logs the disabled state (WARN + gauge 0) or validates the config.
/// Called once at the top of `run_with_shutdown` so a partial config aborts the pod
/// instead of leaving the capability silently "not provisioned".
pub fn validate_startup() -> Result<(), StorageConfigError> {
    validate_with(|k| std::env::var(k).ok())
}

/// [`validate_startup`] over an injected variable source.
fn validate_with(get: impl Fn(&str) -> Option<String>) -> Result<(), StorageConfigError> {
    match parse_settings(get)? {
        None => {
            record_provisioned(false);
            tracing::warn!(
                "BUNDLE_STORAGE_* not set; object-storage capability DISABLED (every \
                 object_storage host-call will report not_implemented until provisioned)"
            );
        }
        Some(s) => tracing::info!(
            endpoint = %s.endpoint,
            bucket = %s.bucket,
            region = %s.region,
            "bundle object-storage configuration validated"
        ),
    }
    Ok(())
}

fn record_provisioned(on: bool) {
    metrics::gauge!("waddles_bundle_storage_capability_provisioned").set(if on {
        1.0
    } else {
        0.0
    });
}

/// Builds the production wiring from `settings`. The audit sink is the durable
/// Postgres one when `audit_password` is given (connect failure is an error, never a
/// silent downgrade), else the tracing sink with a WARN.
pub async fn build_wiring(
    settings: &StorageSettings,
    audit_cfg: &AuditConnectConfig,
    audit_password: Option<&Secret>,
    license_client: &Arc<penguin_licensing::LicenseClient>,
) -> Result<ObjectStorageWiring, StorageConfigError> {
    let backend = build_backend(settings)?;
    let audit: Arc<dyn AuditSink> = match audit_password {
        Some(pw) => {
            let conn = pg_audit::connect(audit_cfg, pw.expose())
                .await
                .map_err(|e| {
                    StorageConfigError::Invalid(format!("audit connection failed: {e}"))
                })?;
            Arc::new(PgAuditSink::new(conn))
        }
        None => {
            tracing::warn!(
                "{AUDIT_PASSWORD_ENV} not set; object-storage audit falls back to structured \
                 tracing events (NOT durable) -- provision the audit role for production"
            );
            Arc::new(TracingAuditSink)
        }
    };
    Ok(ObjectStorageWiring {
        host: Arc::new(StorageHost::new(Arc::new(backend), audit)),
        flag: Arc::new(BundleObjectStorageCapabilityGate::new(Arc::clone(
            license_client,
        ))),
    })
}

/// Process-start entry used at both stage-spawn sites: `None` when disabled, the
/// wiring when provisioned. Config-shaped errors already failed the pod in
/// [`validate_startup`]; the runtime-only failure (audit DB connect) is ERROR-logged
/// with the gauge at 0 so an outage never reads as an opt-out.
pub async fn try_build_wiring(
    cli: &crate::config::CliConfig,
    license_client: &Arc<penguin_licensing::LicenseClient>,
) -> Option<ObjectStorageWiring> {
    try_build_with(|k| std::env::var(k).ok(), cli, license_client).await
}

/// [`try_build_wiring`] over an injected variable source.
async fn try_build_with(
    get: impl Fn(&str) -> Option<String>,
    cli: &crate::config::CliConfig,
    license_client: &Arc<penguin_licensing::LicenseClient>,
) -> Option<ObjectStorageWiring> {
    let settings = match parse_settings(&get) {
        Ok(Some(s)) => s,
        Ok(None) => return None,
        Err(err) => {
            tracing::error!(error = %err, "object-storage capability unavailable");
            return None;
        }
    };
    let audit_password = get(AUDIT_PASSWORD_ENV)
        .filter(|s| !s.is_empty())
        .map(Secret::new);
    let audit_cfg = AuditConnectConfig {
        host: cli.bundle_db_host.clone(),
        port: cli.bundle_db_port,
        name: cli.bundle_db_name.clone(),
        user: AUDIT_ROLE.to_string(),
    };
    match build_wiring(
        &settings,
        &audit_cfg,
        audit_password.as_ref(),
        license_client,
    )
    .await
    {
        Ok(w) => {
            record_provisioned(true);
            tracing::info!(bucket = %settings.bucket, "object-storage capability provisioned");
            Some(w)
        }
        Err(err) => {
            record_provisioned(false);
            tracing::error!(
                error = %err,
                "object-storage capability: configured but could not be built -- \
                 misconfiguration or outage, not an opt-out"
            );
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |k| m.get(k).cloned()
    }

    fn full() -> Vec<(&'static str, &'static str)> {
        vec![
            (
                "BUNDLE_STORAGE_ENDPOINT",
                "https://seaweedfs-s3.waddlebot.svc:8333",
            ),
            ("BUNDLE_STORAGE_BUCKET", "waddles-bundle-storage"),
            ("BUNDLE_STORAGE_REGION", "us-east-1"),
            ("BUNDLE_STORAGE_ACCESS_KEY_ID", "AKIDTEST"),
            ("BUNDLE_STORAGE_SECRET_ACCESS_KEY", "s3cr3t-value"),
        ]
    }

    fn test_license_client() -> Arc<penguin_licensing::LicenseClient> {
        let cfg = penguin_licensing::LicenseConfig::new("svc-process-test-bundle-storage")
            .expect("default LicenseConfig::new never fails");
        penguin_licensing::LicenseClient::new(cfg).expect("valid default config")
    }

    fn audit_cfg() -> AuditConnectConfig {
        AuditConnectConfig {
            host: "127.0.0.1".into(),
            port: 1,
            name: "waddlebot".into(),
            user: AUDIT_ROLE.into(),
        }
    }

    #[test]
    fn unset_is_disabled_not_error() {
        assert!(parse_settings(env(&[])).unwrap().is_none());
    }

    #[test]
    fn empty_values_count_as_unset() {
        let pairs: Vec<_> = REQUIRED_ENV.iter().map(|k| (*k, "  ")).collect();
        assert!(parse_settings(env(&pairs)).unwrap().is_none());
    }

    #[test]
    fn complete_env_is_provisioned() {
        let s = parse_settings(env(&full())).unwrap().expect("settings");
        assert_eq!(s.bucket, "waddles-bundle-storage");
        assert_eq!(s.secret_access_key.expose(), "s3cr3t-value");
    }

    #[test]
    fn partial_env_fails_loud_naming_every_missing_var() {
        let mut p = full();
        p.retain(|(k, _)| {
            *k != "BUNDLE_STORAGE_SECRET_ACCESS_KEY" && *k != "BUNDLE_STORAGE_REGION"
        });
        match parse_settings(env(&p)) {
            Err(StorageConfigError::Partial { missing }) => assert_eq!(
                missing,
                vec!["BUNDLE_STORAGE_REGION", "BUNDLE_STORAGE_SECRET_ACCESS_KEY"]
            ),
            other => panic!("expected Partial, got {other:?}"),
        }
    }

    #[test]
    fn single_var_is_partial() {
        let p = vec![("BUNDLE_STORAGE_BUCKET", "b")];
        assert!(matches!(
            parse_settings(env(&p)),
            Err(StorageConfigError::Partial { .. })
        ));
    }

    #[test]
    fn plaintext_remote_endpoint_is_rejected() {
        let mut p = full();
        p[0] = ("BUNDLE_STORAGE_ENDPOINT", "http://s3.example.com");
        assert!(matches!(
            parse_settings(env(&p)),
            Err(StorageConfigError::Invalid(_))
        ));
    }

    #[test]
    fn debug_and_error_never_leak_credentials() {
        let s = parse_settings(env(&full())).unwrap().unwrap();
        let dbg = format!("{s:?}");
        assert!(!dbg.contains("s3cr3t-value") && !dbg.contains("AKIDTEST"));
        let err = StorageConfigError::Partial {
            missing: vec!["BUNDLE_STORAGE_BUCKET"],
        };
        assert!(!err.to_string().contains("s3cr3t-value"));
    }

    #[tokio::test]
    async fn build_wiring_provisions_with_tracing_audit_fallback() {
        let s = parse_settings(env(&full())).unwrap().unwrap();
        let lc = test_license_client();
        let w = build_wiring(&s, &audit_cfg(), None, &lc)
            .await
            .expect("wiring");
        assert!(
            !w.flag.enabled().await,
            "flag defaults OFF without the license server"
        );
    }

    #[tokio::test]
    async fn unreachable_audit_db_fails_loud_not_silent() {
        let s = parse_settings(env(&full())).unwrap().unwrap();
        let lc = test_license_client();
        let pw = Secret::new("x");
        let r = build_wiring(&s, &audit_cfg(), Some(&pw), &lc).await;
        assert!(
            matches!(r, Err(StorageConfigError::Invalid(m)) if m.contains("audit connection failed"))
        );
    }

    fn cli() -> crate::config::CliConfig {
        use clap::Parser as _;
        crate::config::CliConfig::parse_from(["svc-process"])
    }

    #[test]
    fn startup_validation_passes_when_disabled_or_complete_and_fails_when_partial() {
        assert!(validate_with(env(&[])).is_ok());
        assert!(validate_with(env(&full())).is_ok());
        let partial = vec![("BUNDLE_STORAGE_BUCKET", "b")];
        assert!(matches!(
            validate_with(env(&partial)),
            Err(StorageConfigError::Partial { .. })
        ));
    }

    #[tokio::test]
    async fn try_build_is_none_when_disabled() {
        assert!(try_build_with(env(&[]), &cli(), &test_license_client())
            .await
            .is_none());
    }

    #[tokio::test]
    async fn try_build_provisions_when_env_complete() {
        assert!(try_build_with(env(&full()), &cli(), &test_license_client())
            .await
            .is_some());
    }

    #[tokio::test]
    async fn try_build_is_none_on_partial_env_never_a_half_wired_host() {
        let partial = vec![("BUNDLE_STORAGE_BUCKET", "b")];
        assert!(
            try_build_with(env(&partial), &cli(), &test_license_client())
                .await
                .is_none()
        );
    }

    #[tokio::test]
    async fn try_build_is_none_when_configured_audit_db_is_unreachable() {
        let mut p = full();
        p.push((AUDIT_PASSWORD_ENV, "pw"));
        assert!(try_build_with(env(&p), &cli(), &test_license_client())
            .await
            .is_none());
    }
}
