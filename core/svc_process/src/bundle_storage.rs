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
//!
//! Object bodies are sealed per tenant (`bundle_host_storage::seal`, tenant DEK from hub-api's
//! `GetTenantDek` broker, shared with the kv sealing). Sealing fails CLOSED and has no ledger:
//! there is no plaintext write path (no usable key or the
//! `waddles.disable-tenant-at-rest-encryption` kill-switch refuses the write), and legacy
//! pre-encryption plaintext is only READ, only for objects last modified before the operator's
//! `BUNDLE_STORAGE_LEGACY_PLAINTEXT_UNTIL` cutoff (unset = never). Without the hub-api key service
//! configured the capability is not provisioned at all (ERROR + gauge 0), never unsealed.

use std::fmt;
use std::sync::Arc;

use bundle_host_kv::grpc_wiring::GrpcDekProvider;
use bundle_host_storage::pg_audit::{self, AuditConnectConfig};
use bundle_host_storage::s3::{S3Backend, S3Config};
use bundle_host_storage::{
    AuditSink, ObjectBackend, ObjectSealing, PgAuditSink, StorageHost, TracingAuditSink,
};

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

/// Password of the INSERT-only `waddles_bundle_storage_audit` role. REQUIRED whenever the
/// capability is provisioned: the durable Postgres audit sink is the only audit sink
/// outside local development (`DEPLOYMENT_TIER=local` may fall back to tracing events).
pub const AUDIT_PASSWORD_ENV: &str = "BUNDLE_STORAGE_AUDIT_PASSWORD";

/// The only tier allowed to run without the durable audit sink.
pub const LOCAL_TIER: &str = "local";

/// Resolves the audit credential policy for a PROVISIONED capability: `Ok(Some)` =
/// durable Postgres audit, `Ok(None)` = local-dev tracing fallback, `Err` = missing
/// credential outside local dev (startup must fail, never silently downgrade).
pub fn resolve_audit_password(
    get: &impl Fn(&str) -> Option<String>,
) -> Result<Option<Secret>, StorageConfigError> {
    if let Some(pw) = get(AUDIT_PASSWORD_ENV).filter(|s| !s.trim().is_empty()) {
        return Ok(Some(Secret::new(pw)));
    }
    let tier = get("DEPLOYMENT_TIER");
    if tier.as_deref().map(str::trim) == Some(LOCAL_TIER) {
        return Ok(None);
    }
    let tier = tier.unwrap_or_else(|| "<unset>".to_string());
    Err(StorageConfigError::Invalid(format!(
        "{AUDIT_PASSWORD_ENV} is required when object storage is provisioned (the durable \
         Postgres audit sink is mandatory outside DEPLOYMENT_TIER={LOCAL_TIER}; \
         got tier {tier:?})"
    )))
}

/// Default INSERT-only audit role (`alembic/versions/0080_bundle_storage_audit.py`).
pub const AUDIT_ROLE: &str = "waddles_bundle_storage_audit";

/// Unix-seconds cutoff for serving legacy (pre-encryption) plaintext objects: only objects whose
/// server-side last-modified is BEFORE it are read. Record the instant sealing was first enabled.
/// Unset/blank = no plaintext object is ever served.
pub const LEGACY_PLAINTEXT_UNTIL_ENV: &str = "BUNDLE_STORAGE_LEGACY_PLAINTEXT_UNTIL";

/// Clock-skew allowance when checking that the cutoff is not in the future.
const CUTOFF_SKEW_SECS: u64 = 300;

fn now_unix() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

/// Parses [`LEGACY_PLAINTEXT_UNTIL_ENV`]: `Ok(None)` when unset/blank. A malformed value or one in
/// the future is an error -- a future cutoff would make plaintext an attacker writes NOW readable
/// until then, so the window may only close at or before the present.
pub fn resolve_legacy_cutoff(
    get: &impl Fn(&str) -> Option<String>,
    now_unix: u64,
) -> Result<Option<u64>, StorageConfigError> {
    let Some(raw) = get(LEGACY_PLAINTEXT_UNTIL_ENV)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
    else {
        return Ok(None);
    };
    let cutoff: u64 = raw.parse().map_err(|_| {
        StorageConfigError::Invalid(format!(
            "{LEGACY_PLAINTEXT_UNTIL_ENV} must be unix seconds (digits only)"
        ))
    })?;
    if cutoff > now_unix.saturating_add(CUTOFF_SKEW_SECS) {
        return Err(StorageConfigError::Invalid(format!(
            "{LEGACY_PLAINTEXT_UNTIL_ENV} is in the future; the legacy-plaintext window must close \
             at or before now (a future cutoff would serve plaintext written after sealing began)"
        )));
    }
    Ok(Some(cutoff))
}

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
    let mut it = vals.into_iter();
    let mut next = || {
        it.next().flatten().ok_or_else(|| {
            StorageConfigError::Invalid(
                "BUNDLE_STORAGE_* value vanished after presence check".to_string(),
            )
        })
    };
    let settings = StorageSettings {
        endpoint: next()?,
        bucket: next()?,
        region: next()?,
        access_key_id: Secret::new(next()?),
        secret_access_key: Secret::new(next()?),
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
    match parse_settings(&get)? {
        None => {
            record_provisioned(false);
            tracing::warn!(
                "BUNDLE_STORAGE_* not set; object-storage capability DISABLED (every \
                 object_storage host-call will report not_implemented until provisioned)"
            );
        }
        Some(s) => {
            // Provisioned: the durable audit credential is mandatory (local dev excepted).
            resolve_audit_password(&get)?;
            resolve_legacy_cutoff(&get, now_unix())?;
            tracing::info!(
                endpoint = %s.endpoint,
                bucket = %s.bucket,
                region = %s.region,
                "bundle object-storage configuration validated"
            );
        }
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
/// silent downgrade), else the tracing sink with a WARN. `sealing` is mandatory: every host
/// built here seals object bodies per tenant, with no plaintext write path.
pub async fn build_wiring(
    settings: &StorageSettings,
    audit_cfg: &AuditConnectConfig,
    audit_password: Option<&Secret>,
    license_client: &Arc<penguin_licensing::LicenseClient>,
    sealing: ObjectSealing,
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
    Ok(assemble(Arc::new(backend), audit, license_client, sealing))
}

/// The one place a [`StorageHost`] is built: always with sealing attached.
fn assemble(
    backend: Arc<dyn ObjectBackend>,
    audit: Arc<dyn AuditSink>,
    license_client: &Arc<penguin_licensing::LicenseClient>,
    sealing: ObjectSealing,
) -> ObjectStorageWiring {
    ObjectStorageWiring {
        host: Arc::new(StorageHost::new(backend, audit).with_sealing(sealing)),
        flag: Arc::new(BundleObjectStorageCapabilityGate::new(Arc::clone(
            license_client,
        ))),
    }
}

/// Process-start entry used at both stage-spawn sites: `None` when disabled, the
/// wiring when provisioned. Config-shaped errors already failed the pod in
/// [`validate_startup`]; the runtime-only failure (audit DB connect) is ERROR-logged
/// with the gauge at 0 so an outage never reads as an opt-out.
pub async fn try_build_wiring(
    cli: &crate::config::CliConfig,
    license_client: &Arc<penguin_licensing::LicenseClient>,
    dek_cache: &GrpcDekProvider,
) -> Option<ObjectStorageWiring> {
    try_build_with(|k| std::env::var(k).ok(), cli, license_client, dek_cache).await
}

/// [`try_build_wiring`] over an injected variable source.
async fn try_build_with(
    get: impl Fn(&str) -> Option<String>,
    cli: &crate::config::CliConfig,
    license_client: &Arc<penguin_licensing::LicenseClient>,
    dek_cache: &GrpcDekProvider,
) -> Option<ObjectStorageWiring> {
    let settings = match parse_settings(&get) {
        Ok(Some(s)) => s,
        Ok(None) => return None,
        Err(err) => {
            tracing::error!(error = %err, "object-storage capability unavailable");
            return None;
        }
    };
    let audit_password = match resolve_audit_password(&get) {
        Ok(pw) => pw,
        Err(err) => {
            record_provisioned(false);
            tracing::error!(error = %err, "object-storage capability unavailable");
            return None;
        }
    };
    // Sealing fails closed at build time too: with no hub-api key service there is no tenant
    // DEK, so every put/get would refuse -- say so once, loudly, and do not provision.
    if crate::kv_sealing::key_endpoint(cli).is_none() {
        record_provisioned(false);
        tracing::error!(
            "object-storage capability unavailable: hub-api key service not configured \
             (HUB_API_GRPC_ENDPOINT / SERVICE_JWT_TOKEN_ENDPOINT); bodies are never stored unsealed"
        );
        return None;
    }
    let legacy_cutoff = match resolve_legacy_cutoff(&get, now_unix()) {
        Ok(cutoff) => cutoff,
        Err(err) => {
            record_provisioned(false);
            tracing::error!(error = %err, "object-storage capability unavailable");
            return None;
        }
    };
    if let Some(cutoff) = legacy_cutoff {
        tracing::warn!(
            cutoff_unix = cutoff,
            "legacy plaintext object READS enabled for objects modified before the cutoff \
             (writes are always sealed); unset {LEGACY_PLAINTEXT_UNTIL_ENV} once migrated"
        );
    }
    let sealing = crate::kv_sealing::object_sealing(dek_cache, Some(license_client), legacy_cutoff);
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
        sealing,
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
        let w = build_wiring(&s, &audit_cfg(), None, &lc, test_sealing())
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
        let r = build_wiring(&s, &audit_cfg(), Some(&pw), &lc, test_sealing()).await;
        assert!(
            matches!(r, Err(StorageConfigError::Invalid(m)) if m.contains("audit connection failed"))
        );
    }

    /// A CLI with the hub-api key service configured (sealing needs it).
    fn cli() -> crate::config::CliConfig {
        use clap::Parser as _;
        crate::config::CliConfig::parse_from([
            "svc-process",
            "--hub-api-grpc-endpoint",
            "https://hub-api:50051",
            "--service-jwt-token-endpoint",
            "https://hub-api:8443/token",
        ])
    }

    /// A CLI WITHOUT the key service: sealing cannot work, so nothing may be provisioned.
    fn cli_without_key_service() -> crate::config::CliConfig {
        use clap::Parser as _;
        crate::config::CliConfig::parse_from(["svc-process"])
    }

    /// The (lazy, unconnected) DEK cache the stage would build from `cli()`.
    fn dek() -> GrpcDekProvider {
        bundle_host_crypto::grpc::tenant_crypto_over_grpc(
            crate::kv_sealing::key_endpoint(&cli()),
            crate::SERVICE_NAME,
        )
        .1
    }

    fn test_sealing() -> ObjectSealing {
        crate::kv_sealing::object_sealing(&dek(), None, None)
    }

    #[test]
    fn startup_validation_passes_when_disabled_or_complete_and_fails_when_partial() {
        assert!(validate_with(env(&[])).is_ok());
        assert!(validate_with(env(&local())).is_ok());
        let partial = vec![("BUNDLE_STORAGE_BUCKET", "b")];
        assert!(matches!(
            validate_with(env(&partial)),
            Err(StorageConfigError::Partial { .. })
        ));
    }

    #[tokio::test]
    async fn try_build_is_none_when_disabled() {
        assert!(
            try_build_with(env(&[]), &cli(), &test_license_client(), &dek())
                .await
                .is_none()
        );
    }

    #[tokio::test]
    async fn try_build_provisions_when_env_complete() {
        assert!(
            try_build_with(env(&local()), &cli(), &test_license_client(), &dek())
                .await
                .is_some()
        );
    }

    fn local() -> Vec<(&'static str, &'static str)> {
        let mut p = full();
        p.push(("DEPLOYMENT_TIER", "local"));
        p
    }

    #[test]
    fn audit_password_is_required_outside_local_dev() {
        for tier in ["alpha", "beta", "production", ""] {
            let mut p = full();
            p.push(("DEPLOYMENT_TIER", tier));
            assert!(
                matches!(validate_with(env(&p)), Err(StorageConfigError::Invalid(m)) if m.contains(AUDIT_PASSWORD_ENV)),
                "tier {tier:?} must fail loud without the audit credential"
            );
        }
        // Unset tier is not local either.
        assert!(validate_with(env(&full())).is_err());
    }

    #[test]
    fn audit_password_present_passes_in_any_tier_and_blank_counts_as_missing() {
        let mut p = full();
        p.push(("DEPLOYMENT_TIER", "production"));
        p.push((AUDIT_PASSWORD_ENV, "pw"));
        assert!(validate_with(env(&p)).is_ok());
        let mut blank = full();
        blank.push(("DEPLOYMENT_TIER", "production"));
        blank.push((AUDIT_PASSWORD_ENV, "  "));
        assert!(validate_with(env(&blank)).is_err());
    }

    #[tokio::test]
    async fn try_build_refuses_tracing_fallback_outside_local() {
        let mut p = full();
        p.push(("DEPLOYMENT_TIER", "alpha"));
        assert!(
            try_build_with(env(&p), &cli(), &test_license_client(), &dek())
                .await
                .is_none()
        );
    }

    #[tokio::test]
    async fn try_build_is_none_on_partial_env_never_a_half_wired_host() {
        let partial = vec![("BUNDLE_STORAGE_BUCKET", "b")];
        assert!(
            try_build_with(env(&partial), &cli(), &test_license_client(), &dek())
                .await
                .is_none()
        );
    }

    #[tokio::test]
    async fn try_build_is_none_when_configured_audit_db_is_unreachable() {
        let mut p = full();
        p.push((AUDIT_PASSWORD_ENV, "pw"));
        assert!(
            try_build_with(env(&p), &cli(), &test_license_client(), &dek())
                .await
                .is_none()
        );
    }

    // ---- legacy-plaintext cutoff ----------------------------------------------------------

    #[test]
    fn legacy_cutoff_unset_blank_valid_malformed_and_future() {
        const NOW: u64 = 2_000_000_000;
        let none = env(&[]);
        assert_eq!(resolve_legacy_cutoff(&none, NOW), Ok(None));
        let blank = env(&[(LEGACY_PLAINTEXT_UNTIL_ENV, "  ")]);
        assert_eq!(resolve_legacy_cutoff(&blank, NOW), Ok(None));
        let ok = env(&[(LEGACY_PLAINTEXT_UNTIL_ENV, " 1999999999 ")]);
        assert_eq!(resolve_legacy_cutoff(&ok, NOW), Ok(Some(1_999_999_999)));
        let at_now = env(&[(LEGACY_PLAINTEXT_UNTIL_ENV, "2000000000")]);
        assert_eq!(resolve_legacy_cutoff(&at_now, NOW), Ok(Some(NOW)));
        let skew = env(&[(LEGACY_PLAINTEXT_UNTIL_ENV, "2000000300")]);
        assert_eq!(resolve_legacy_cutoff(&skew, NOW), Ok(Some(NOW + 300)));
        for bad in ["soon", "-5", "12.5", "0x10", "20000000001234567890123"] {
            let g = env(&[(LEGACY_PLAINTEXT_UNTIL_ENV, bad)]);
            assert!(
                matches!(
                    resolve_legacy_cutoff(&g, NOW),
                    Err(StorageConfigError::Invalid(_))
                ),
                "{bad:?} must be rejected"
            );
        }
        let future = env(&[(LEGACY_PLAINTEXT_UNTIL_ENV, "2000000301")]);
        assert!(matches!(
            resolve_legacy_cutoff(&future, NOW),
            Err(StorageConfigError::Invalid(m)) if m.contains("future")
        ));
    }

    #[test]
    fn startup_validation_rejects_a_bad_cutoff_only_when_provisioned() {
        let mut p = local();
        p.push((LEGACY_PLAINTEXT_UNTIL_ENV, "tomorrow"));
        assert!(matches!(
            validate_with(env(&p)),
            Err(StorageConfigError::Invalid(m)) if m.contains(LEGACY_PLAINTEXT_UNTIL_ENV)
        ));
        // not provisioned: the setting is irrelevant
        assert!(validate_with(env(&[(LEGACY_PLAINTEXT_UNTIL_ENV, "tomorrow")])).is_ok());
        let mut ok = local();
        ok.push((LEGACY_PLAINTEXT_UNTIL_ENV, "1700000000"));
        assert!(validate_with(env(&ok)).is_ok());
    }

    // ---- fail-closed provisioning ---------------------------------------------------------

    #[tokio::test]
    async fn without_the_key_service_nothing_is_provisioned() {
        assert!(try_build_with(
            env(&local()),
            &cli_without_key_service(),
            &test_license_client(),
            &dek()
        )
        .await
        .is_none());
    }

    #[tokio::test]
    async fn a_malformed_or_future_cutoff_provisions_nothing() {
        for bad in ["soon", "99999999999"] {
            let mut p = local();
            p.push((LEGACY_PLAINTEXT_UNTIL_ENV, bad));
            assert!(
                try_build_with(env(&p), &cli(), &test_license_client(), &dek())
                    .await
                    .is_none(),
                "{bad:?}"
            );
        }
    }

    #[tokio::test]
    async fn a_valid_cutoff_still_provisions() {
        let mut p = local();
        p.push((LEGACY_PLAINTEXT_UNTIL_ENV, "1700000000"));
        assert!(
            try_build_with(env(&p), &cli(), &test_license_client(), &dek())
                .await
                .is_some()
        );
    }

    // ---- the assembled host really seals --------------------------------------------------

    mod assembled_host {
        use super::*;
        use bundle_host_crypto::{Dek, DekProvider, DekUnavailableError, TenantCrypto, MAGIC};
        use bundle_host_storage::key::object_key;
        use bundle_host_storage::seal::BoxFuture as SealFuture;
        use bundle_host_storage::{GrantSet, MemoryBackend, SealGate, StorageError, StorageScope};
        use bytes::Bytes;
        use std::sync::atomic::{AtomicBool, Ordering};
        use zeroize::Zeroizing;

        const APP: &str = "waddles.harness.wiring";
        const PLAIN: &[u8] = b"tenant confidential clip metadata";
        const CUTOFF: u64 = 2_000_000_000;

        /// DEK provider double: one fixed key per tenant, switchable to "broker down".
        struct Keys(Arc<AtomicBool>);

        impl DekProvider for Keys {
            async fn get_dek(&self, tenant: &str) -> Result<(Dek, u32), DekUnavailableError> {
                self.get_dek_version(tenant, 1).await.map(|d| (d, 1))
            }

            async fn get_dek_version(
                &self,
                tenant: &str,
                version: u32,
            ) -> Result<Dek, DekUnavailableError> {
                if !self.0.load(Ordering::SeqCst) || version != 1 {
                    return Err(DekUnavailableError {
                        tenant_id: tenant.to_string(),
                        dek_version: Some(version),
                        reason: "test: unavailable".into(),
                    });
                }
                Ok(Zeroizing::new([tenant.len() as u8 + 9; 32]))
            }
        }

        struct Off;
        impl SealGate for Off {
            fn seal_writes(&self) -> SealFuture<'_, bool> {
                Box::pin(async { false })
            }
        }

        struct Rig {
            wiring: ObjectStorageWiring,
            backend: Arc<MemoryBackend>,
            key_up: Arc<AtomicBool>,
        }

        fn rig(configure: impl FnOnce(ObjectSealing) -> ObjectSealing, cutoff: Option<u64>) -> Rig {
            let backend = Arc::new(MemoryBackend::new());
            let key_up = Arc::new(AtomicBool::new(true));
            let crypto = TenantCrypto::new(Keys(key_up.clone()));
            let sealing = configure(crate::kv_sealing::object_sealing_with(crypto, None, cutoff));
            let wiring = assemble(
                backend.clone(),
                Arc::new(TracingAuditSink),
                &test_license_client(),
                sealing,
            );
            Rig {
                wiring,
                backend,
                key_up,
            }
        }

        fn scope() -> StorageScope {
            StorageScope {
                app_id: APP.into(),
                tenant: 1,
                community: 7,
                grants: GrantSet::from_permission_ids(std::iter::empty::<&str>()).expect("grants"),
                enabled: true,
                request_id: "req-wiring".into(),
            }
        }

        async fn put(r: &Rig, key: &str) -> Result<(), StorageError> {
            r.wiring
                .host
                .put(
                    &scope(),
                    None,
                    key,
                    Bytes::from_static(PLAIN),
                    "text/plain",
                    &[],
                )
                .await
                .map(|_| ())
        }

        async fn stored(r: &Rig, key: &str) -> Option<bundle_host_storage::StoredObject> {
            r.backend
                .get(&object_key(APP, 1, 7, key))
                .await
                .expect("backend get")
        }

        #[tokio::test]
        async fn a_wired_host_stores_ciphertext_and_reads_it_back() {
            let r = rig(|s| s, None);
            put(&r, "clip").await.expect("put");
            let body = stored(&r, "clip").await.expect("stored").body;
            assert_eq!(&body[..4], &MAGIC, "the bucket holds an envelope");
            assert!(!body.windows(PLAIN.len()).any(|w| w == PLAIN));
            let (_, read) = r
                .wiring
                .host
                .get(&scope(), None, "clip")
                .await
                .expect("get");
            assert_eq!(&read[..], PLAIN);
        }

        #[tokio::test]
        async fn the_kill_switch_refuses_writes_and_never_stores_plaintext() {
            let r = rig(|s| s.with_gate(Arc::new(Off)), None);
            assert!(put(&r, "clip").await.is_err());
            assert!(
                stored(&r, "clip").await.is_none(),
                "nothing written, sealed or not"
            );
        }

        #[tokio::test]
        async fn without_a_tenant_key_nothing_is_written_and_nothing_opens() {
            let r = rig(|s| s, None);
            put(&r, "kept")
                .await
                .expect("put while the key is available");
            r.key_up.store(false, Ordering::SeqCst);
            assert_eq!(
                put(&r, "new").await.unwrap_err(),
                StorageError::Backend("tenant data key unavailable".into())
            );
            assert!(stored(&r, "new").await.is_none());
            assert!(r.wiring.host.get(&scope(), None, "kept").await.is_err());
        }

        #[tokio::test]
        async fn an_unconfigured_key_service_fails_closed_through_the_production_sealing() {
            // `object_sealing` over a DEK cache with no reachable broker: every put refuses.
            let backend = Arc::new(MemoryBackend::new());
            let wiring = assemble(
                backend.clone(),
                Arc::new(TracingAuditSink),
                &test_license_client(),
                crate::kv_sealing::object_sealing(
                    &bundle_host_crypto::grpc::tenant_crypto_over_grpc(None, crate::SERVICE_NAME).1,
                    None,
                    None,
                ),
            );
            let r = Rig {
                wiring,
                backend,
                key_up: Arc::new(AtomicBool::new(false)),
            };
            assert!(put(&r, "clip").await.is_err());
            assert!(stored(&r, "clip").await.is_none());
        }

        fn seed_legacy(r: &Rig, key: &str, modified: u64) {
            r.backend
                .put_with_modified(
                    &object_key(APP, 1, 7, key),
                    Bytes::from_static(PLAIN),
                    "text/plain",
                    &[
                        "waddles-tenant=1".to_string(),
                        "waddles-community=7".to_string(),
                    ],
                    modified,
                )
                .expect("seed");
        }

        #[tokio::test]
        async fn legacy_plaintext_is_read_only_inside_the_operator_cutoff() {
            let never = rig(|s| s, None);
            seed_legacy(&never, "old", 1);
            assert!(
                never.wiring.host.get(&scope(), None, "old").await.is_err(),
                "no cutoff configured: no plaintext is ever served"
            );

            let windowed = rig(|s| s, Some(CUTOFF));
            seed_legacy(&windowed, "older", CUTOFF - 1);
            seed_legacy(&windowed, "newer", CUTOFF);
            let (_, body) = windowed
                .wiring
                .host
                .get(&scope(), None, "older")
                .await
                .expect("inside the window");
            assert_eq!(&body[..], PLAIN);
            assert!(windowed
                .wiring
                .host
                .get(&scope(), None, "newer")
                .await
                .is_err());
            // and writes stay sealed even while legacy reads are open
            put(&windowed, "fresh").await.expect("put");
            let fresh = stored(&windowed, "fresh").await.expect("stored").body;
            assert_eq!(&fresh[..4], &MAGIC);
        }
    }
}
