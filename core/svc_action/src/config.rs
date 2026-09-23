//! Environment-driven configuration.
//!
//! Non-secret operational settings are parsed via `clap` (CLI flags with an
//! `env` fallback) for operability. Anything secret (the DB password) is
//! read directly from the environment only and is never exposed as a CLI
//! flag -- per `rules/critical-rules.md` Token & Secret Hygiene ("Pass as
//! CLI args" is never allowed for secrets). Secrets are never
//! `Debug`/`Display`-printed.
//!
//! Executor integration (host-API/retry/binding/Valkey settings) landed in
//! M3 -- see `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`
//! §4.3 (retry defaults), §6.6 (host-API port/TLS), §5.11 (binding keys),
//! §12.7 (spine tuning, delegated to `penguin_spine::SpineConfig::from_env`
//! rather than re-declared here).

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
    name = "svc-action",
    version,
    about = "Waddles ACTION stage-runner (pipeline terminal stage)"
)]
pub struct CliConfig {
    /// Control-plane HTTP port (axum router: /health, /healthz).
    #[arg(long, env = "MODULE_PORT", default_value_t = 8202)]
    pub http_port: u16,

    /// Prometheus `/metrics` exposition port.
    #[arg(long, env = "METRICS_PORT", default_value_t = 9090)]
    pub metrics_port: u16,

    /// Address the HTTP/metrics listeners bind to.
    #[arg(long, env = "BIND_ADDR", default_value = "0.0.0.0")]
    pub bind_addr: IpAddr,

    /// Postgres/sqlite host (per-service DB account, never a shared
    /// credential) -- owns `action_dispatch_log` only.
    #[arg(long, env = "DB_HOST", default_value = "localhost")]
    pub db_host: String,
    #[arg(long, env = "DB_PORT", default_value_t = 5432)]
    pub db_port: u16,
    #[arg(long, env = "DB_NAME", default_value = "waddlebot")]
    pub db_name: String,
    #[arg(long, env = "DB_USER", default_value = "svc_action")]
    pub db_user: String,

    /// The mTLS host-API listener the `svc-action-executor` deployment
    /// dials (spec §6.6/§7.1) -- `:8302` for this stage.
    #[arg(long, env = "HOST_API_PORT", default_value_t = 8302)]
    pub host_api_port: u16,
    /// PEM server certificate for the host-API mTLS listener.
    #[arg(long, env = "HOST_API_SERVER_CERT_FILE")]
    pub host_api_server_cert_file: Option<PathBuf>,
    /// PEM server private key, file-only (never inline) per Token & Secret
    /// Hygiene.
    #[arg(long, env = "HOST_API_SERVER_KEY_FILE")]
    pub host_api_server_key_file: Option<PathBuf>,
    /// PEM CA bundle used to verify the executor's client certificate
    /// (mutual TLS -- spec §6.6: "Both peers present certificates").
    #[arg(long, env = "HOST_API_CLIENT_CA_FILE")]
    pub host_api_client_ca_file: Option<PathBuf>,
    /// Whether this pod expects its executor to be running under the
    /// gVisor `RuntimeClass` (spec §12.2, D32 -- default `false`: "this is
    /// the default posture, not a fallback").
    #[arg(long, env = "WADDLES_SANDBOX_GVISOR", default_value_t = false)]
    pub sandbox_gvisor: bool,
    /// Per-call wall-clock budget handed to the executor on every `invoke`
    /// (spec §7.3).
    #[arg(long, env = "EXECUTOR_CALL_TIMEOUT_MS", default_value_t = 2000)]
    pub executor_call_timeout_ms: u64,

    /// Maximum dispatch attempts before a retryable failure is recorded
    /// terminal (spec §4.3).
    #[arg(long, env = "ACTION_MAX_RETRIES", default_value_t = 3)]
    pub action_max_retries: u32,
    /// Full-jitter exponential backoff base (spec §4.3).
    #[arg(long, env = "ACTION_BASE_BACKOFF_MS", default_value_t = 250)]
    pub action_base_backoff_ms: u64,
    /// Backoff ceiling; also caps a bundle-supplied `retry-after-ms` (spec
    /// §4.3).
    #[arg(long, env = "ACTION_MAX_BACKOFF_MS", default_value_t = 8000)]
    pub action_max_backoff_ms: u64,

    /// The active `binding.mac` key version this pod mints new MACs under
    /// (spec §5.11). Verification additionally accepts any `kid` present
    /// in `ENVELOPE_BINDING_KEYS` inside the rotation-overlap window.
    #[arg(long, env = "ENVELOPE_BINDING_ACTIVE_KID")]
    pub envelope_binding_active_kid: Option<String>,
    /// The single bundle `app_id` this pod's dispatch loop drains (spec
    /// §5.9's consumer group name). Empty (default) disables the drain
    /// loop entirely -- multi-bundle scheduling is itself blocked on the
    /// same distribution-poll gap `core/svc_process`'s M4 skeleton
    /// documents (`PROCESS_APP_ID`), mirrored here as `ACTION_APP_ID`.
    #[arg(long, env = "ACTION_APP_ID", default_value = "")]
    pub action_app_id: String,
    /// A `kid` still inside this window (seconds) verifies even when it is
    /// not the active kid (spec §5.11 `security.envelopeBinding.
    /// rotationOverlapSeconds`) -- purely advisory at this layer: `kid`
    /// acceptance is actually driven by which keys `ENVELOPE_BINDING_KEYS`
    /// (env-only, see [`Config::envelope_binding_keys`]) supplies, this
    /// value is surfaced for operator visibility/future key-expiry pruning.
    #[arg(
        long,
        env = "ENVELOPE_BINDING_ROTATION_OVERLAP_S",
        default_value_t = 86_400
    )]
    pub envelope_binding_rotation_overlap_s: u64,

    /// Per-stage-replica batching interval for `waddles:usage` writes
    /// (spec §5.12/D31, `metering.flushIntervalSeconds`, default `10`) --
    /// `crate::usage::UsageBatcher` deltas accumulate in memory and are
    /// `XADD`ed at most this often, never per event, so a chatty channel
    /// never multiplies the write rate.
    #[arg(long, env = "METERING_FLUSH_INTERVAL_S", default_value_t = 10)]
    pub metering_flush_interval_s: u64,
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
        if self.action_base_backoff_ms > self.action_max_backoff_ms {
            return Err(ConfigError::InvalidValue {
                field: "action_base_backoff_ms/action_max_backoff_ms",
                reason: "base backoff must not exceed the max backoff".to_string(),
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
    /// Raw `ENVELOPE_BINDING_KEYS` value (spec §5.11): `kid:hexkey[,kid:hexkey...]`
    /// -- the symmetric HMAC key material `crate::hop::KeyRing` parses.
    /// `None` when unset; dispatch refuses to start hop verification
    /// without it rather than silently accepting every envelope (a missing
    /// keyring must never fail open).
    pub envelope_binding_keys: Option<Secret>,
}

impl fmt::Debug for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Config")
            .field("cli", &self.cli)
            .field("db_password", &Secret::new(""))
            .field(
                "envelope_binding_keys",
                &self.envelope_binding_keys.as_ref().map(|_| "<redacted>"),
            )
            .finish()
    }
}

impl Config {
    /// Loads configuration from CLI args + environment. `DB_PASSWORD` is a
    /// required secret; a missing value is a hard startup error rather than
    /// a silently-insecure default.
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
        let envelope_binding_keys = std::env::var("ENVELOPE_BINDING_KEYS").ok().map(Secret::new);
        Ok(Self {
            cli,
            db_password,
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
        // SAFETY: serialized by ENV_LOCK, no concurrent readers/writers of
        // this specific variable within the test process.
        unsafe { std::env::remove_var("DB_PASSWORD") };
    }

    #[test]
    fn defaults_parse_from_empty_args() {
        let cli = CliConfig::parse_from(["svc-action"]);
        assert_eq!(cli.http_port, 8202);
        assert_eq!(cli.metrics_port, 9090);
        assert_eq!(cli.db_port, 5432);
        assert_eq!(cli.db_name, "waddlebot");
        cli.validate().expect("defaults must be valid");
    }

    #[test]
    fn cli_flag_overrides_default() {
        let cli = CliConfig::parse_from(["svc-action", "--http-port", "9000"]);
        assert_eq!(cli.http_port, 9000);
    }

    #[test]
    fn zero_port_fails_validation() {
        let cli = CliConfig::parse_from(["svc-action", "--http-port", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn zero_host_api_port_fails_validation() {
        let cli = CliConfig::parse_from(["svc-action", "--host-api-port", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn host_api_cert_without_key_fails_validation() {
        let cli =
            CliConfig::parse_from(["svc-action", "--host-api-server-cert-file", "/tmp/cert.pem"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn host_api_key_without_cert_fails_validation() {
        let cli =
            CliConfig::parse_from(["svc-action", "--host-api-server-key-file", "/tmp/key.pem"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn host_api_cert_and_key_together_is_valid() {
        let cli = CliConfig::parse_from([
            "svc-action",
            "--host-api-server-cert-file",
            "/tmp/cert.pem",
            "--host-api-server-key-file",
            "/tmp/key.pem",
        ]);
        assert!(cli.validate().is_ok());
    }

    #[test]
    fn base_backoff_above_max_fails_validation() {
        let cli = CliConfig::parse_from([
            "svc-action",
            "--action-base-backoff-ms",
            "9000",
            "--action-max-backoff-ms",
            "8000",
        ]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn sandbox_gvisor_defaults_to_false_per_d32() {
        let cli = CliConfig::parse_from(["svc-action"]);
        assert!(!cli.sandbox_gvisor);
    }

    #[test]
    fn retry_defaults_match_spec_4_3() {
        let cli = CliConfig::parse_from(["svc-action"]);
        assert_eq!(cli.action_max_retries, 3);
        assert_eq!(cli.action_base_backoff_ms, 250);
        assert_eq!(cli.action_max_backoff_ms, 8000);
    }

    #[test]
    fn metering_flush_interval_defaults_to_10s_per_spec_5_12() {
        let cli = CliConfig::parse_from(["svc-action"]);
        assert_eq!(cli.metering_flush_interval_s, 10);
    }

    #[test]
    fn metering_flush_interval_env_override_is_honored() {
        let cli = CliConfig::parse_from(["svc-action", "--metering-flush-interval-s", "30"]);
        assert_eq!(cli.metering_flush_interval_s, 30);
    }

    #[test]
    fn host_api_port_default_is_8302() {
        let cli = CliConfig::parse_from(["svc-action"]);
        assert_eq!(cli.host_api_port, 8302);
    }

    #[test]
    fn load_fails_without_required_secrets() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-action"]);
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
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert_eq!(cfg.db_password.expose(), "test-db-pass");
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
            std::env::set_var("ENVELOPE_BINDING_KEYS", "k1:aabbcc");
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert_eq!(
            cfg.envelope_binding_keys.as_ref().map(Secret::expose),
            Some("k1:aabbcc")
        );
        clear_secret_env();
        unsafe { std::env::remove_var("ENVELOPE_BINDING_KEYS") };
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
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        let rendered = format!("{cfg:?}");
        assert!(!rendered.contains("super-secret-db-pass"));
        clear_secret_env();
    }
}
