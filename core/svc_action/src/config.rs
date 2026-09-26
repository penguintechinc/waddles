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

    /// Interim, env-driven substitute for the `GET /api/v1/distribution/
    /// bundles?stage=action` poll actually resolving a loadable digest
    /// (spec §6.7) -- defense-in-depth fallback, mirrors `core/svc_process`'s
    /// own `PROCESS_BUNDLE_DIGEST` (`config::CliConfig` there) field-for-
    /// field. The distribution poll (`crate::distribution`) remains the
    /// primary/eventual source: `crate::resolve_initial_bundle` only falls
    /// back to this value when the catalog never resolves a digest for
    /// `ACTION_APP_ID` within its retry window (hub-api empty/unreachable),
    /// and `crate::try_start_env_bundle_loader` sends this bundle's `load`
    /// frame directly -- independent of hub-api reachability -- so the
    /// action stage can invoke without ever having polled a live hub-api.
    /// Empty (default) disables the fallback entirely, leaving today's
    /// catalog-only behavior unchanged. Expected shape: `sha256:<64 hex>`
    /// -- the executor's own digest validator (`bundle_executor::invoke::
    /// verify_digest`) rejects a bare hex string with `MalformedDigest`, so
    /// the chart must set this with the prefix already included.
    #[arg(long, env = "ACTION_BUNDLE_DIGEST", default_value = "")]
    pub action_bundle_digest: String,
    /// See [`Self::action_bundle_digest`]. The `load` frame's `version`
    /// field (spec §6.6) -- distinct from the digest, echoed back by the
    /// executor's `loaded` reply.
    #[arg(long, env = "ACTION_BUNDLE_VERSION", default_value = "1")]
    pub action_bundle_version: String,
    /// See [`Self::action_bundle_digest`]. The bucket key `load` asks the
    /// executor to fetch the compiled component from (spec §7.6 step 3's
    /// naming convention -- `crate::distribution::bucket_keys` derives the
    /// same shape from a real distribution row; this fallback requires the
    /// operator to supply it directly since there is no row to derive it
    /// from).
    #[arg(long, env = "ACTION_BUNDLE_COMPONENT_KEY", default_value = "")]
    pub action_bundle_component_key: String,
    /// See [`Self::action_bundle_digest`]. The bucket key for the bundle's
    /// manifest sidecar, same convention as
    /// [`Self::action_bundle_component_key`].
    #[arg(long, env = "ACTION_BUNDLE_SIDECAR_KEY", default_value = "")]
    pub action_bundle_sidecar_key: String,

    /// hub-api base URL, source of the `GET /api/v1/distribution/bundles
    /// ?stage=action` poll (spec §6.7) `crate::distribution` drives --
    /// same field name/default `core/svc_process`'s own M4 config carries
    /// for the identical poll, kept consistent across the two stages.
    #[arg(long, env = "HUB_API_URL", default_value = "http://hub-api:8204")]
    pub hub_api_url: String,
    /// Distribution-bundles poll interval, in seconds (spec §6.7: "Polling
    /// behaviour is unchanged ... every `POLL_INTERVAL_S` (5.0 s)").
    #[arg(long, env = "POLL_INTERVAL_S", default_value_t = 5.0)]
    pub poll_interval_s: f64,
    /// `iss` claim on the service JWT `crate::service_jwt` mints for the
    /// distribution poll -- matches `libs/flask_core/flask_core/
    /// auth.py::DEFAULT_JWT_ISSUER`'s own default exactly, so this pod's
    /// tokens verify against the same platform-wide `verify_jwt_token`
    /// hub-api (and every other flask_core-based service) already runs.
    /// Not currently overridden anywhere in `k8s/helm/waddlebot` -- both
    /// sides rely on this identical hardcoded default.
    #[arg(long, env = "JWT_ISSUER", default_value = "waddlebot")]
    pub jwt_issuer: String,
    /// `aud` claim -- matches `DEFAULT_JWT_AUDIENCE`'s own default. See
    /// `jwt_issuer` above.
    #[arg(long, env = "JWT_AUDIENCE", default_value = "waddlebot-services")]
    pub jwt_audience: String,
    /// `tenant` claim on the minted service JWT -- same env var name and
    /// `"global"` default `core/svc_process`/`core/svc_ingest`'s Python
    /// stage-runner `Config.RUNNER_TENANT_SLUG` already uses for this exact
    /// purpose (`libs/flask_core/flask_core/stage_runner.py`'s
    /// `jwt_provider`, wired from `RUNNER_TENANT_SLUG` in each service's
    /// `app.py`).
    #[arg(long, env = "RUNNER_TENANT_SLUG", default_value = "global")]
    pub runner_tenant_slug: String,

    /// Lifts the private-address half of the bundle `http` capability's
    /// SSRF guard (spec §8.2 step 6 / §8.5) for hosts already on that
    /// bundle's manifest `egress` allowlist -- loopback, link-local,
    /// unspecified, multicast and the cloud-metadata addresses stay
    /// blocked regardless of this setting.
    #[arg(long, env = "EGRESS_ALLOW_PRIVATE_HOSTS", default_value_t = false)]
    pub egress_allow_private_hosts: bool,
    /// Default per-bundle egress token-bucket rate (spec §7.3), overridden
    /// per bundle by `manifest.limits.egress_rps` when present.
    #[arg(long, env = "EGRESS_RATE_LIMIT_RPS", default_value_t = 10)]
    pub egress_rate_limit_rps: u32,
    /// Egress token-bucket burst size (spec §8.2 step 8).
    #[arg(long, env = "EGRESS_RATE_LIMIT_BURST", default_value_t = 20)]
    pub egress_rate_limit_burst: u32,
    /// Total wall-clock budget for one `http.send` call, including
    /// redirects (spec §8.2 step 12).
    #[arg(long, env = "EGRESS_TIMEOUT_MS", default_value_t = 5000)]
    pub egress_timeout_ms: u64,
    /// Maximum redirect hops a bundle `http.send` call follows, each
    /// re-validated against the full guard (spec §8.2 step 10).
    #[arg(long, env = "EGRESS_MAX_REDIRECTS", default_value_t = 3)]
    pub egress_max_redirects: u8,
    /// Response bodies larger than this are truncated, never denied (spec
    /// §8.2 step 11).
    #[arg(long, env = "EGRESS_MAX_RESPONSE_BYTES", default_value_t = 1_048_576)]
    pub egress_max_response_bytes: usize,
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
    /// `SECRET_KEY` (env-only, see [`Config::from_cli`]) -- the shared
    /// HS256 signing key `crate::service_jwt` uses to mint the distribution
    /// poll's service JWT. The exact same secret hub-api (and every other
    /// flask_core-based service) verifies incoming bearer tokens against
    /// (`libs/flask_core/flask_core/secrets.py::require_secret_key`,
    /// default env var `SECRET_KEY`) -- already present on this pod today
    /// via the Helm chart's blanket `envFrom: secretRef` (`templates/
    /// secrets.yaml`'s `SECRET_KEY` key, shared with `hub-api.yaml`), no
    /// chart change required. Required at startup, same fail-closed
    /// treatment as `db_password` above -- there is no insecure-placeholder
    /// fallback here (unlike the Python `require_secret_key()` helper this
    /// mirrors, which tolerates an unset value outside production): this
    /// binary has no equivalent "is this a dev/test process" signal to key
    /// that leniency off of, so requiring the value unconditionally is the
    /// safe default.
    pub secret_key: Secret,
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
            .field("secret_key", &Secret::new(""))
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
        let secret_key = Secret::new(env_required("SECRET_KEY")?);
        Ok(Self {
            cli,
            db_password,
            envelope_binding_keys,
            secret_key,
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
        // these specific variables within the test process.
        unsafe {
            std::env::remove_var("DB_PASSWORD");
            std::env::remove_var("SECRET_KEY");
        }
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
    fn distribution_poll_defaults_match_spec_6_7() {
        let cli = CliConfig::parse_from(["svc-action"]);
        assert_eq!(cli.hub_api_url, "http://hub-api:8204");
        assert_eq!(cli.poll_interval_s, 5.0);
    }

    #[test]
    fn action_bundle_env_override_defaults_to_disabled() {
        let cli = CliConfig::parse_from(["svc-action"]);
        assert_eq!(cli.action_bundle_digest, "");
        assert_eq!(cli.action_bundle_version, "1");
        assert_eq!(cli.action_bundle_component_key, "");
        assert_eq!(cli.action_bundle_sidecar_key, "");
    }

    #[test]
    fn action_bundle_env_override_flags_are_honored() {
        let cli = CliConfig::parse_from([
            "svc-action",
            "--action-bundle-digest",
            "sha256:aa",
            "--action-bundle-version",
            "2.0.0",
            "--action-bundle-component-key",
            "bundles/app/2.0.0/aa.wasm",
            "--action-bundle-sidecar-key",
            "bundles/app/2.0.0/aa.json",
        ]);
        assert_eq!(cli.action_bundle_digest, "sha256:aa");
        assert_eq!(cli.action_bundle_version, "2.0.0");
        assert_eq!(cli.action_bundle_component_key, "bundles/app/2.0.0/aa.wasm");
        assert_eq!(cli.action_bundle_sidecar_key, "bundles/app/2.0.0/aa.json");
    }

    #[test]
    fn egress_defaults_match_spec_7_3_and_8_2() {
        let cli = CliConfig::parse_from(["svc-action"]);
        assert!(!cli.egress_allow_private_hosts);
        assert_eq!(cli.egress_rate_limit_rps, 10);
        assert_eq!(cli.egress_rate_limit_burst, 20);
        assert_eq!(cli.egress_timeout_ms, 5000);
        assert_eq!(cli.egress_max_redirects, 3);
        assert_eq!(cli.egress_max_response_bytes, 1_048_576);
    }

    #[test]
    fn egress_allow_private_hosts_flag_override_is_honored() {
        let cli = CliConfig::parse_from(["svc-action", "--egress-allow-private-hosts"]);
        assert!(cli.egress_allow_private_hosts);
    }

    #[test]
    fn load_fails_without_required_secrets() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-action"]);
        let err = Config::from_cli(cli).unwrap_err();
        assert_eq!(err, ConfigError::MissingEnv("DB_PASSWORD"));
    }

    /// `SECRET_KEY` is checked after `DB_PASSWORD` (see `Config::from_cli`)
    /// -- with `DB_PASSWORD` set and `SECRET_KEY` absent, the missing-secret
    /// error must name `SECRET_KEY` specifically, not silently succeed or
    /// report the wrong variable.
    #[test]
    fn load_fails_without_secret_key() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let err = Config::from_cli(cli).unwrap_err();
        assert_eq!(err, ConfigError::MissingEnv("SECRET_KEY"));
        clear_secret_env();
    }

    #[test]
    fn load_succeeds_with_required_secrets_set() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
            std::env::set_var("SECRET_KEY", "test-jwt-signing-secret");
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert_eq!(cfg.db_password.expose(), "test-db-pass");
        assert_eq!(cfg.secret_key.expose(), "test-jwt-signing-secret");
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
            std::env::set_var("SECRET_KEY", "test-jwt-signing-secret");
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
            std::env::set_var("SECRET_KEY", "super-secret-jwt-signing-key");
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        let rendered = format!("{cfg:?}");
        assert!(!rendered.contains("super-secret-db-pass"));
        assert!(!rendered.contains("super-secret-jwt-signing-key"));
        clear_secret_env();
    }

    #[test]
    fn jwt_issuer_audience_and_runner_tenant_slug_defaults_match_flask_core() {
        let cli = CliConfig::parse_from(["svc-action"]);
        // Matches `libs/flask_core/flask_core/auth.py`'s
        // `DEFAULT_JWT_ISSUER`/`DEFAULT_JWT_AUDIENCE` and
        // `core/svc_process/config.py`'s `RUNNER_TENANT_SLUG` default
        // exactly -- hub-api verifies the minted service JWT against these
        // same defaults.
        assert_eq!(cli.jwt_issuer, "waddlebot");
        assert_eq!(cli.jwt_audience, "waddlebot-services");
        assert_eq!(cli.runner_tenant_slug, "global");
    }

    #[test]
    fn jwt_issuer_audience_and_runner_tenant_slug_env_overrides_are_honored() {
        let cli = CliConfig::parse_from([
            "svc-action",
            "--jwt-issuer",
            "custom-issuer",
            "--jwt-audience",
            "custom-audience",
            "--runner-tenant-slug",
            "acme",
        ]);
        assert_eq!(cli.jwt_issuer, "custom-issuer");
        assert_eq!(cli.jwt_audience, "custom-audience");
        assert_eq!(cli.runner_tenant_slug, "acme");
    }
}
