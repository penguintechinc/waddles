//! Environment-driven configuration.
//!
//! Non-secret operational settings are parsed via `clap` (CLI flags with an
//! `env` fallback) for operability. Anything secret (DB password, the
//! service-to-service API key) is read directly from the environment only
//! and is never exposed as a CLI flag -- per `rules/critical-rules.md`
//! Token & Secret Hygiene ("Pass as CLI args" is never allowed for
//! secrets). Secrets are never `Debug`/`Display`-printed.
//!
//! Mirrors `core/svc_streaming/src/config.rs` (the M4 reference template).
//! Fields here cover only what this skeleton actually serves
//! (`/health`, `/healthz`, `/metrics`) plus the settings the spec names as
//! this stage's eventual dependencies (distribution-bundles poll, Postgres,
//! Valkey) so later chunks extend rather than re-plumb this file. See
//! `// TODO(M4)` markers in `crate::lib` for what is deliberately not wired
//! yet: penguin-spine stream consumption, the executor host API, and the
//! built-ins that read `db_*`/`cache_*` for real.

use std::fmt;
use std::net::IpAddr;
use std::path::PathBuf;

use clap::Parser;
use thiserror::Error;

/// Errors that can occur while loading configuration.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum ConfigError {
    /// A required secret environment variable was not set.
    #[error("missing required environment variable: {0}")]
    MissingEnv(&'static str),
    /// A value was present but failed validation.
    #[error("invalid value for {field}: {reason}")]
    InvalidValue { field: &'static str, reason: String },
}

/// A secret value whose `Debug` implementation never prints the underlying
/// bytes -- guards against accidental exposure via `tracing::debug!(?cfg)`
/// or a panic message.
#[derive(Clone, PartialEq, Eq)]
pub struct Secret(String);

impl Secret {
    /// Wraps a raw string as a redacted secret.
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    /// Returns the underlying secret value. Callers must not log this.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret(***redacted***)")
    }
}

/// CLI/env-configurable operational settings (non-secret). Every field has
/// an `env` fallback so Helm/Docker deployments never need CLI args.
#[derive(Parser, Debug, Clone)]
#[command(
    name = "svc-process",
    version,
    about = "Waddles process-stage data-plane service"
)]
pub struct CliConfig {
    /// Control-plane HTTP port (axum router: /health, /healthz).
    #[arg(long, env = "MODULE_PORT", default_value_t = 8201)]
    pub http_port: u16,

    /// Prometheus `/metrics` exposition port.
    #[arg(long, env = "METRICS_PORT", default_value_t = 9090)]
    pub metrics_port: u16,

    /// Address the HTTP/metrics listeners bind to.
    #[arg(long, env = "BIND_ADDR", default_value = "0.0.0.0")]
    pub bind_addr: IpAddr,

    /// hub-api base URL, source of the `GET /api/v1/distribution/bundles
    /// ?stage=process` activation poll.
    /// TODO(M4): the poll loop itself is blocked on M2's compiler/executor
    /// and penguin-spine landing; this field is read by config validation
    /// only today.
    #[arg(long, env = "HUB_API_URL", default_value = "http://hub-api:8204")]
    pub hub_api_url: String,

    /// Distribution-bundles poll interval, in seconds.
    #[arg(long, env = "POLL_INTERVAL_S", default_value_t = 5.0)]
    pub poll_interval_s: f64,

    /// Postgres host (per-service DB account, never a shared credential) --
    /// SeaORM connects here for the built-ins' own tables and to serve
    /// bundles' `db` host calls (SS4.2). Not yet connected in this
    /// skeleton; see `/healthz`.
    #[arg(long, env = "DB_HOST", default_value = "localhost")]
    pub db_host: String,
    #[arg(long, env = "DB_PORT", default_value_t = 5432)]
    pub db_port: u16,
    #[arg(long, env = "DB_NAME", default_value = "waddlebot")]
    pub db_name: String,
    #[arg(long, env = "DB_USER", default_value = "svc_process")]
    pub db_user: String,

    /// Valkey host, the transport for granted ingest-source streams
    /// (`penguin-spine`, `XREADGROUP`/`XADD`). Not yet connected in this
    /// skeleton; see `/healthz`.
    #[arg(long, env = "CACHE_HOST", default_value = "localhost")]
    pub cache_host: String,
    #[arg(long, env = "CACHE_PORT", default_value_t = 6379)]
    pub cache_port: u16,

    /// The bundle `app_id` this process-stage instance drains granted
    /// ingest-source streams for (`penguin_spine::Stage::Process`'s
    /// consumer group name, spec Sec5.2). Empty (the default) means "no
    /// bundle assigned yet" -- `crate::run_with_shutdown` does not spawn
    /// the spine drain loop in that case. Multi-bundle-per-instance
    /// scheduling, and populating this from a real activation rather than
    /// a static env var, are themselves blocked on M2's
    /// distribution-bundles poll (SS4.2), which is what would normally
    /// supply this value plus the instance's granted-stream list.
    #[arg(long, env = "PROCESS_APP_ID", default_value = "")]
    pub process_app_id: String,

    /// The mTLS host-API listener the `svc-process-executor` deployment
    /// dials (spec SS6.6/SS7.1) -- `:8301` for this stage.
    #[arg(long, env = "HOST_API_PORT", default_value_t = 8301)]
    pub host_api_port: u16,
    /// PEM server certificate for the host-API mTLS listener.
    #[arg(long, env = "HOST_API_SERVER_CERT_FILE")]
    pub host_api_server_cert_file: Option<PathBuf>,
    /// PEM server private key, file-only (never inline) per Token & Secret
    /// Hygiene.
    #[arg(long, env = "HOST_API_SERVER_KEY_FILE")]
    pub host_api_server_key_file: Option<PathBuf>,
    /// PEM CA bundle used to verify the executor's client certificate
    /// (mutual TLS -- spec SS6.6: "Both peers present certificates").
    #[arg(long, env = "HOST_API_CLIENT_CA_FILE")]
    pub host_api_client_ca_file: Option<PathBuf>,
    /// Whether this pod expects its executor to be running under the
    /// gVisor `RuntimeClass` (spec SS12.2, D32 -- default `false`: "this is
    /// the default posture, not a fallback").
    #[arg(long, env = "WADDLES_SANDBOX_GVISOR", default_value_t = false)]
    pub sandbox_gvisor: bool,
    /// Per-call wall-clock budget handed to the executor on every `invoke`
    /// (spec SS7.3).
    #[arg(long, env = "EXECUTOR_CALL_TIMEOUT_MS", default_value_t = 2000)]
    pub executor_call_timeout_ms: u64,

    /// Interim, env-driven substitute for the `GET /api/v1/distribution/
    /// bundles?stage=process` grant list (spec SS4.2/SS6.7) -- **TODO(M4+)**:
    /// replace with the real poll once it lands. When set together with
    /// [`Self::process_ingest_source_id`], the drain loop is granted
    /// exactly one ingest-source stream to read; when either is unset, the
    /// grant list stays empty (the loop connects and blocks, reading
    /// nothing -- identical to the pre-M4-runtime skeleton's behavior).
    #[arg(long, env = "PROCESS_INGEST_PLATFORM", default_value = "")]
    pub process_ingest_platform: String,
    /// See [`Self::process_ingest_platform`].
    #[arg(long, env = "PROCESS_INGEST_SOURCE_ID", default_value = "")]
    pub process_ingest_source_id: String,
    /// Interim, env-driven substitute for the digest/keys a real
    /// distribution-poll `load` would supply (spec SS6.6) -- **TODO(M4+)**.
    /// Empty disables the executor `load`/`invoke` path entirely (the drain
    /// loop still runs and can dead-letter on `executor_unavailable`).
    #[arg(long, env = "PROCESS_BUNDLE_DIGEST", default_value = "")]
    pub process_bundle_digest: String,
    #[arg(long, env = "PROCESS_BUNDLE_VERSION", default_value = "1")]
    pub process_bundle_version: String,
    #[arg(long, env = "PROCESS_BUNDLE_COMPONENT_KEY", default_value = "")]
    pub process_bundle_component_key: String,
    #[arg(long, env = "PROCESS_BUNDLE_SIDECAR_KEY", default_value = "")]
    pub process_bundle_sidecar_key: String,
    /// Interim, env-driven substitute for a real `app_install_approvals`/
    /// `routes_to` (spec SS5.9/SS6.9) lookup -- **TODO(M4+)**. Shape:
    /// `target_app_id1:tenant1,target_app_id2:tenant2`. See
    /// `crate::builtins::parse_approved_targets`/`resolve_cross_app_route`.
    #[arg(long, env = "PROCESS_ROUTES_TO_APPROVED", default_value = "")]
    pub process_routes_to_approved: String,
}

impl CliConfig {
    /// Validates cross-field invariants that `clap`'s per-arg parsing can't
    /// express.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.http_port == 0 || self.metrics_port == 0 {
            return Err(ConfigError::InvalidValue {
                field: "http_port/metrics_port",
                reason: "port 0 is not a valid bind port".to_string(),
            });
        }
        if self.poll_interval_s <= 0.0 {
            return Err(ConfigError::InvalidValue {
                field: "poll_interval_s",
                reason: "must be positive".to_string(),
            });
        }
        if self.host_api_port == 0 {
            return Err(ConfigError::InvalidValue {
                field: "host_api_port",
                reason: "port 0 is not a valid bind port".to_string(),
            });
        }
        if self.host_api_server_cert_file.is_some() != self.host_api_server_key_file.is_some() {
            return Err(ConfigError::InvalidValue {
                field: "host_api_server_cert_file/host_api_server_key_file",
                reason: "must be set together".to_string(),
            });
        }
        Ok(())
    }
}

/// Fully-loaded runtime configuration: operational settings plus secrets
/// pulled directly from the environment (never via CLI flag).
#[derive(Clone)]
pub struct Config {
    pub cli: CliConfig,
    pub db_password: Secret,
    pub cache_password: Option<Secret>,
    pub service_api_key: Secret,
    /// Raw `ENVELOPE_BINDING_KEYS` value (spec SS5.11): `kid:hexkey[,kid:hexkey...]`
    /// -- the symmetric HMAC key material `penguin_spine::KeyRing` parses
    /// (`crate::hop`). `None` when unset; the process loop refuses to start
    /// hop verification without it rather than silently accepting every
    /// envelope (a missing keyring must never fail open).
    pub envelope_binding_keys: Option<Secret>,
}

impl fmt::Debug for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Config")
            .field("cli", &self.cli)
            .field("db_password", &Secret::new(""))
            .field(
                "cache_password",
                &self.cache_password.as_ref().map(|_| Secret::new("")),
            )
            .field("service_api_key", &Secret::new(""))
            .field(
                "envelope_binding_keys",
                &self.envelope_binding_keys.as_ref().map(|_| "<redacted>"),
            )
            .finish()
    }
}

impl Config {
    /// Loads configuration from CLI args + environment. `DB_PASSWORD` and
    /// `SERVICE_API_KEY` are required secrets; a missing value is a hard
    /// startup error rather than a silently-insecure default.
    pub fn load() -> Result<Self, ConfigError> {
        let cli = CliConfig::parse();
        Self::from_cli(cli)
    }

    /// Builds a [`Config`] from an already-parsed [`CliConfig`], reading
    /// secrets from the environment. Split out from [`Self::load`] so tests
    /// can supply CLI args explicitly without depending on process argv.
    pub fn from_cli(cli: CliConfig) -> Result<Self, ConfigError> {
        cli.validate()?;
        let db_password = Secret::new(env_required("DB_PASSWORD")?);
        let cache_password = std::env::var("CACHE_PASSWORD").ok().map(Secret::new);
        let service_api_key = Secret::new(env_required("SERVICE_API_KEY")?);
        let envelope_binding_keys = std::env::var("ENVELOPE_BINDING_KEYS").ok().map(Secret::new);
        Ok(Self {
            cli,
            db_password,
            cache_password,
            service_api_key,
            envelope_binding_keys,
        })
    }
}

fn env_required(name: &'static str) -> Result<String, ConfigError> {
    std::env::var(name).map_err(|_| ConfigError::MissingEnv(name))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variables.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn clear_secret_env() {
        for var in [
            "DB_PASSWORD",
            "CACHE_PASSWORD",
            "SERVICE_API_KEY",
            "ENVELOPE_BINDING_KEYS",
        ] {
            // SAFETY: serialized by ENV_LOCK, no concurrent readers/writers
            // of these specific variables within the test process.
            unsafe { std::env::remove_var(var) };
        }
    }

    #[test]
    fn defaults_parse_from_empty_args() {
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.http_port, 8201);
        assert_eq!(cli.metrics_port, 9090);
        assert_eq!(cli.poll_interval_s, 5.0);
        assert_eq!(cli.process_app_id, "");
        cli.validate().expect("defaults must be valid");
    }

    #[test]
    fn process_app_id_flag_overrides_default() {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--process-app-id",
            "waddles.bot.commands.default",
        ]);
        assert_eq!(cli.process_app_id, "waddles.bot.commands.default");
    }

    #[test]
    fn cli_flag_overrides_default() {
        let cli = CliConfig::parse_from(["svc-process", "--http-port", "9000"]);
        assert_eq!(cli.http_port, 9000);
    }

    #[test]
    fn zero_port_fails_validation() {
        let cli = CliConfig::parse_from(["svc-process", "--http-port", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn zero_host_api_port_fails_validation() {
        let cli = CliConfig::parse_from(["svc-process", "--host-api-port", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn host_api_cert_without_key_fails_validation() {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--host-api-server-cert-file",
            "/tmp/cert.pem",
        ]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn host_api_key_without_cert_fails_validation() {
        let cli =
            CliConfig::parse_from(["svc-process", "--host-api-server-key-file", "/tmp/key.pem"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn host_api_cert_and_key_together_is_valid() {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--host-api-server-cert-file",
            "/tmp/cert.pem",
            "--host-api-server-key-file",
            "/tmp/key.pem",
        ]);
        assert!(cli.validate().is_ok());
    }

    #[test]
    fn host_api_port_default_is_8301() {
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.host_api_port, 8301);
    }

    #[test]
    fn sandbox_gvisor_defaults_to_false_per_d32() {
        let cli = CliConfig::parse_from(["svc-process"]);
        assert!(!cli.sandbox_gvisor);
    }

    #[test]
    fn interim_ingest_grant_and_bundle_fields_default_empty() {
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.process_ingest_platform, "");
        assert_eq!(cli.process_ingest_source_id, "");
        assert_eq!(cli.process_bundle_digest, "");
        assert_eq!(cli.process_bundle_component_key, "");
        assert_eq!(cli.process_bundle_sidecar_key, "");
    }

    #[test]
    fn non_positive_poll_interval_fails_validation() {
        let cli = CliConfig::parse_from(["svc-process", "--poll-interval-s", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn load_fails_without_required_secrets() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-process"]);
        let err = Config::from_cli(cli).unwrap_err();
        assert_eq!(err, ConfigError::MissingEnv("DB_PASSWORD"));
    }

    #[test]
    fn load_succeeds_with_required_secrets_set() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
            std::env::set_var("SERVICE_API_KEY", "test-api-key");
        }
        let cli = CliConfig::parse_from(["svc-process"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert_eq!(cfg.db_password.expose(), "test-db-pass");
        assert_eq!(cfg.service_api_key.expose(), "test-api-key");
        assert!(cfg.cache_password.is_none());
        assert!(cfg.envelope_binding_keys.is_none());
        clear_secret_env();
    }

    #[test]
    fn envelope_binding_keys_loaded_from_env_when_present() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
            std::env::set_var("SERVICE_API_KEY", "test-api-key");
            std::env::set_var("ENVELOPE_BINDING_KEYS", "k1:aabbcc");
        }
        let cli = CliConfig::parse_from(["svc-process"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert_eq!(
            cfg.envelope_binding_keys.as_ref().map(Secret::expose),
            Some("k1:aabbcc")
        );
        clear_secret_env();
    }

    #[test]
    fn debug_never_prints_secret_bytes() {
        let secret = Secret::new("super-secret-value");
        let rendered = format!("{secret:?}");
        assert!(!rendered.contains("super-secret-value"));
        assert!(rendered.contains("redacted"));
    }

    #[test]
    fn config_debug_never_prints_secret_bytes() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("DB_PASSWORD", "super-secret-db-pass");
            std::env::set_var("SERVICE_API_KEY", "super-secret-api-key");
        }
        let cli = CliConfig::parse_from(["svc-process"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        let rendered = format!("{cfg:?}");
        assert!(!rendered.contains("super-secret-db-pass"));
        assert!(!rendered.contains("super-secret-api-key"));
        clear_secret_env();
    }
}
