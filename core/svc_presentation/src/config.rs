//! Environment-driven configuration.
//!
//! Non-secret operational settings are parsed via `clap` (CLI flags with an
//! `env` fallback) for operability. Anything secret (the DB/cache
//! passwords) is read directly from the environment only and is never
//! exposed as a CLI flag -- per `rules/critical-rules.md` Token & Secret
//! Hygiene ("Pass as CLI args" is never allowed for secrets). Secrets are
//! never `Debug`/`Display`-printed.
//!
//! Unlike `core/svc_streaming/src/config.rs`, there is no static
//! `SERVICE_API_KEY`/`JWT_HMAC_SECRET` here: the overlay PUSH credential
//! (`overlay_auth::require_push_credential`) is a hub-api-issued machine
//! JWT verified against hub-api's JWKS endpoint (`PUSH_JWKS_URL` below),
//! replacing the Python alpha's static `PRESENTATION_PUSH_TOKEN` bearer
//! secret entirely -- see `core/overlay_auth/src/lib.rs` module doc.

use std::fmt;
use std::net::IpAddr;

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
    name = "svc-presentation",
    version,
    about = "Waddles overlay/presentation service"
)]
pub struct CliConfig {
    /// Control-plane HTTP port. `8207` matches the Python alpha's existing
    /// `MODULE_PORT` default (`core/svc_presentation/config.py`) so
    /// Helm/K8s Service port wiring doesn't change when this service cuts
    /// over.
    #[arg(long, env = "MODULE_PORT", default_value_t = 8207)]
    pub http_port: u16,

    /// Prometheus `/metrics` exposition port.
    #[arg(long, env = "METRICS_PORT", default_value_t = 9090)]
    pub metrics_port: u16,

    /// Address the HTTP/metrics listeners bind to.
    #[arg(long, env = "BIND_ADDR", default_value = "0.0.0.0")]
    pub bind_addr: IpAddr,

    /// Postgres/sqlite host (per-service DB account, never a shared
    /// credential -- `rules/backend-database.md` Per-Service Database
    /// Accounts).
    #[arg(long, env = "DB_HOST", default_value = "localhost")]
    pub db_host: String,
    #[arg(long, env = "DB_PORT", default_value_t = 5432)]
    pub db_port: u16,
    #[arg(long, env = "DB_NAME", default_value = "waddlebot")]
    pub db_name: String,
    /// Matches the Python alpha's existing `DB_USER` default
    /// (`core/svc_presentation/config.py`).
    #[arg(long, env = "DB_USER", default_value = "svc-presentation-rw")]
    pub db_user: String,

    /// Cache (Redis/Valkey-compatible) host -- reserved for the live
    /// overlay SSE/websocket fan-out P2/P3 add; unused by this scaffold.
    #[arg(long, env = "CACHE_HOST", default_value = "localhost")]
    pub cache_host: String,
    #[arg(long, env = "CACHE_PORT", default_value_t = 6379)]
    pub cache_port: u16,

    /// hub-api's JWKS endpoint -- `overlay::push_trust::AppPushTrustSource`
    /// verifies every PUSH credential's signature against the keys this
    /// returns (`service_auth::JwksTrustBundle`). No static shared secret
    /// is configured anywhere in this service.
    #[arg(
        long,
        env = "PUSH_JWKS_URL",
        default_value = "http://hub-api/.well-known/jwks.json"
    )]
    pub push_jwks_url: String,

    /// Expected `aud` claim on an inbound PUSH credential.
    #[arg(long, env = "PUSH_AUDIENCE", default_value = "waddlebot-internal")]
    pub push_audience: String,

    /// Expected `iss` claim on an inbound PUSH credential -- in practice
    /// always `hub-api` (see `overlay_auth::push::PushTrustSource::
    /// trusted_issuers`'s doc comment on why this is a list).
    #[arg(long, env = "PUSH_TRUSTED_ISSUER", default_value = "hub-api")]
    pub push_trusted_issuer: String,

    /// SeaweedFS/S3-compatible endpoint for overlay image assets (P6/P9).
    /// `IMAGE_BUCKET_*` (not `S3_*`/`RECORDINGS_BUCKET`, `core/svc_streaming`'s
    /// convention) -- a distinct, service-scoped env namespace, same
    /// precedent `core/bundle_executor`'s own `BUNDLE_BUCKET_*` convention
    /// sets, so running both services in the same cluster never risks one
    /// misreading the other's bucket/credential pair.
    #[arg(
        long,
        env = "IMAGE_BUCKET_ENDPOINT",
        default_value = "http://infra-seaweedfs:8333"
    )]
    pub image_bucket_endpoint: String,

    /// Bucket name -- defaults to the same `waddlebot-assets` bucket the
    /// existing Python hub avatar/logo uploads use (`docs/guides/
    /// seaweedfs-object-storage.md`), but under `image_bucket_prefix`'s own
    /// key prefix below so overlay image assets are never reachable at a
    /// flat/public avatar-style path.
    #[arg(long, env = "IMAGE_BUCKET_NAME", default_value = "waddlebot-assets")]
    pub image_bucket_name: String,

    #[arg(long, env = "IMAGE_BUCKET_REGION", default_value = "us-east-1")]
    pub image_bucket_region: String,

    /// Key prefix every overlay image object is stored under
    /// (`{prefix}/{community_id}/{asset_id}.{ext}`) -- deliberately NOT
    /// `avatars/`/`community-logos/` (those are public-read by design,
    /// `docs/guides/seaweedfs-object-storage.md`'s "Public Read Access").
    /// Overlay image assets are served only via `crate::images::render`'s
    /// signed, scoped, expiring presigned URL -- see that module's doc for
    /// the full rationale.
    #[arg(long, env = "IMAGE_BUCKET_PREFIX", default_value = "overlay-images")]
    pub image_bucket_prefix: String,

    /// Maximum accepted upload size, bytes. Default 8 MiB -- generous for
    /// a PNG/JPEG/WebP overlay graphic, small enough that
    /// `crate::images::upload` never buffers an unbounded body.
    #[arg(long, env = "IMAGE_MAX_BYTES", default_value_t = 8 * 1024 * 1024)]
    pub image_max_bytes: u64,

    /// TTL for a presigned GET URL `crate::images::render` issues. Short
    /// enough that a leaked overlay-client URL stops working soon after;
    /// long enough that a 60s-interval OBS browser-source poll/reconnect
    /// doesn't need to re-fetch a render just to get a fresh link.
    #[arg(long, env = "IMAGE_SIGNED_URL_TTL_SECONDS", default_value_t = 300)]
    pub image_signed_url_ttl_seconds: u64,

    /// How long (seconds) an overlay-code -> community mapping is cached
    /// (`overlay::code`). This is also the worst-case delay before a rotated
    /// (leaked) overlay code stops resolving on this replica; absent codes are
    /// cached for at most 5s regardless. `0` disables caching (every request
    /// reads the database).
    #[arg(long, env = "OVERLAY_CODE_CACHE_TTL_SECONDS", default_value_t = 30)]
    pub overlay_code_cache_ttl_seconds: u64,

    /// Local-development mode: relaxes the fail-fast requirement for
    /// `IMAGE_BUCKET_ACCESS_KEY_ID`/`IMAGE_BUCKET_SECRET_ACCESS_KEY`.
    /// Off by default -- every deployed (non-dev) process must have a
    /// bucket credential pair or refuse to start.
    #[arg(long, env = "SVC_PRESENTATION_DEV_MODE", default_value_t = false)]
    pub dev_mode: bool,

    /// hub-api's internal gRPC endpoint (`waddles.hub.internal.v1`,
    /// `core/hub_client::HubClient::connect`'s `endpoint`), e.g.
    /// `https://waddlebot-hub-api-v3:50204`. Overlay output resolves
    /// `{user:<uuid>}` references to display names through it
    /// (`overlay::detok`). Empty (the default) means "not configured": the
    /// service then refuses to start unless detokenization was explicitly
    /// switched off ([`Self::pii_detokenization_enabled_override`]) -- never
    /// a silent degrade to "Unknown User" everywhere. Same env var names as
    /// `core/svc_action`/`core/svc_process`.
    #[arg(long, env = "HUB_API_GRPC_ENDPOINT", default_value = "")]
    pub hub_api_grpc_endpoint: String,

    /// hub-api's machine-JWT bootstrap endpoint
    /// (`POST /internal/service-token`, `service_auth::MachineJwtClient`'s
    /// `token_endpoint`). Empty means "not configured" -- same fail-loud
    /// contract as [`Self::hub_api_grpc_endpoint`].
    #[arg(long, env = "SERVICE_JWT_TOKEN_ENDPOINT", default_value = "")]
    pub service_jwt_token_endpoint: String,

    /// Path to this pod's projected Kubernetes ServiceAccount token, read by
    /// `service_auth::MachineJwtClient` to bootstrap a machine JWT (the same
    /// default every machine-JWT bootstrap in this repo uses).
    #[arg(
        long,
        env = "SERVICE_JWT_SA_TOKEN_PATH",
        default_value = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )]
    pub service_jwt_sa_token_path: String,

    /// Path to the PEM CA bundle that signed hub-api's internal gRPC server
    /// certificate, passed as `HubClient::connect`'s `ca_cert_path`. Empty
    /// falls back to the system/webpki trust store (never correct against
    /// this chart's internal CA; fine for local runs against a
    /// publicly-rooted endpoint).
    #[arg(long, env = "HUB_API_GRPC_CA_FILE", default_value = "")]
    pub hub_api_grpc_ca_file: String,

    /// Explicit operator escape hatch for deployments with no working
    /// hub-api gRPC connection yet (dev, air-gapped, alpha without an
    /// in-cluster PostHog). `Some(false)` (`PII_DETOKENIZATION_ENABLED=false`)
    /// lets the service start WITHOUT a hub client: every push is still
    /// HTML-escaped and every `{user:<uuid>}` renders as the neutral label --
    /// no name is ever resolved, and nothing raw is ever emitted. Any other
    /// state (unset / `true`) keeps the default: detokenization on, and a
    /// missing/unreachable hub-api is a hard startup error. Loudly logged at
    /// startup, never a silent bypass; mirrors `core/svc_action`'s identical
    /// flag.
    #[arg(long, env = "PII_DETOKENIZATION_ENABLED")]
    pub pii_detokenization_enabled_override: Option<bool>,
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
    /// `IMAGE_BUCKET_ACCESS_KEY_ID`/`IMAGE_BUCKET_SECRET_ACCESS_KEY` --
    /// optional (unlike `db_password`): a deployment that never enables
    /// `crate::flags::IMAGE_UPLOAD_FLAG` need not configure a bucket at
    /// all, so a missing credential here is not a startup error. Absent ⇒
    /// `crate::images::store::ObjectStoreImageStore::from_config` fails
    /// loudly at first use (upload/render time), never silently -- see
    /// that function's own doc.
    pub image_bucket_access_key_id: Option<Secret>,
    pub image_bucket_secret_access_key: Option<Secret>,
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
            .field(
                "image_bucket_access_key_id",
                &self
                    .image_bucket_access_key_id
                    .as_ref()
                    .map(|_| Secret::new("")),
            )
            .field(
                "image_bucket_secret_access_key",
                &self
                    .image_bucket_secret_access_key
                    .as_ref()
                    .map(|_| Secret::new("")),
            )
            .finish()
    }
}

impl Config {
    /// Loads configuration from CLI args + environment. `DB_PASSWORD` is a
    /// required secret; a missing value is a hard startup error rather
    /// than a silently-insecure default.
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
        let non_blank = |name: &str| {
            std::env::var(name)
                .ok()
                .filter(|v| !v.trim().is_empty())
                .map(Secret::new)
        };
        let image_bucket_access_key_id = non_blank("IMAGE_BUCKET_ACCESS_KEY_ID");
        let image_bucket_secret_access_key = non_blank("IMAGE_BUCKET_SECRET_ACCESS_KEY");
        if !cli.dev_mode {
            if image_bucket_access_key_id.is_none() {
                return Err(ConfigError::MissingEnv("IMAGE_BUCKET_ACCESS_KEY_ID"));
            }
            if image_bucket_secret_access_key.is_none() {
                return Err(ConfigError::MissingEnv("IMAGE_BUCKET_SECRET_ACCESS_KEY"));
            }
        }
        Ok(Self {
            cli,
            db_password,
            cache_password,
            image_bucket_access_key_id,
            image_bucket_secret_access_key,
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
            "IMAGE_BUCKET_ACCESS_KEY_ID",
            "IMAGE_BUCKET_SECRET_ACCESS_KEY",
        ] {
            // SAFETY: serialized by ENV_LOCK, no concurrent readers/writers
            // of these specific variables within the test process.
            unsafe { std::env::remove_var(var) };
        }
    }

    #[test]
    fn defaults_parse_from_empty_args() {
        let cli = CliConfig::parse_from(["svc-presentation"]);
        assert_eq!(cli.http_port, 8207);
        assert_eq!(cli.metrics_port, 9090);
        assert_eq!(cli.db_user, "svc-presentation-rw");
        assert_eq!(cli.push_trusted_issuer, "hub-api");
        cli.validate().expect("defaults must be valid");
    }

    #[test]
    fn hub_api_grpc_settings_default_to_unconfigured() {
        let _guard = ENV_LOCK.lock().unwrap();
        for var in [
            "HUB_API_GRPC_ENDPOINT",
            "SERVICE_JWT_TOKEN_ENDPOINT",
            "HUB_API_GRPC_CA_FILE",
            "PII_DETOKENIZATION_ENABLED",
        ] {
            // SAFETY: serialized by ENV_LOCK.
            unsafe { std::env::remove_var(var) };
        }
        let cli = CliConfig::parse_from(["svc-presentation"]);
        assert!(cli.hub_api_grpc_endpoint.is_empty());
        assert!(cli.service_jwt_token_endpoint.is_empty());
        assert!(cli.hub_api_grpc_ca_file.is_empty());
        assert_eq!(
            cli.service_jwt_sa_token_path,
            "/var/run/secrets/kubernetes.io/serviceaccount/token"
        );
        assert_eq!(cli.pii_detokenization_enabled_override, None);
    }

    #[test]
    fn hub_api_grpc_settings_parse_from_flags() {
        let cli = CliConfig::parse_from([
            "svc-presentation",
            "--hub-api-grpc-endpoint",
            "https://hub:50204",
            "--service-jwt-token-endpoint",
            "http://hub:8204/internal/service-token",
            "--hub-api-grpc-ca-file",
            "/ca.pem",
            "--pii-detokenization-enabled-override",
            "false",
        ]);
        assert_eq!(cli.hub_api_grpc_endpoint, "https://hub:50204");
        assert_eq!(
            cli.service_jwt_token_endpoint,
            "http://hub:8204/internal/service-token"
        );
        assert_eq!(cli.hub_api_grpc_ca_file, "/ca.pem");
        assert_eq!(cli.pii_detokenization_enabled_override, Some(false));
    }

    #[test]
    fn cli_flag_overrides_default() {
        let cli = CliConfig::parse_from(["svc-presentation", "--http-port", "9000"]);
        assert_eq!(cli.http_port, 9000);
    }

    #[test]
    fn zero_port_fails_validation() {
        let cli = CliConfig::parse_from(["svc-presentation", "--http-port", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn load_fails_without_required_secrets() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-presentation"]);
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
        let cli = CliConfig::parse_from(["svc-presentation", "--dev-mode"]);
        let cfg = Config::from_cli(cli).expect("secret is set");
        assert_eq!(cfg.db_password.expose(), "test-db-pass");
        assert!(cfg.cache_password.is_none());
        clear_secret_env();
    }

    #[test]
    fn non_dev_startup_fails_fast_on_blank_bucket_credentials() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("DB_PASSWORD", "db");
            std::env::set_var("IMAGE_BUCKET_ACCESS_KEY_ID", "   ");
            std::env::set_var("IMAGE_BUCKET_SECRET_ACCESS_KEY", "sk");
        }
        let err = Config::from_cli(CliConfig::parse_from(["svc-presentation"])).unwrap_err();
        assert_eq!(err, ConfigError::MissingEnv("IMAGE_BUCKET_ACCESS_KEY_ID"));
        unsafe {
            std::env::set_var("IMAGE_BUCKET_ACCESS_KEY_ID", "ak");
            std::env::set_var("IMAGE_BUCKET_SECRET_ACCESS_KEY", "");
        }
        let err = Config::from_cli(CliConfig::parse_from(["svc-presentation"])).unwrap_err();
        assert_eq!(
            err,
            ConfigError::MissingEnv("IMAGE_BUCKET_SECRET_ACCESS_KEY")
        );
        unsafe {
            std::env::set_var("IMAGE_BUCKET_SECRET_ACCESS_KEY", "sk");
        }
        Config::from_cli(CliConfig::parse_from(["svc-presentation"])).expect("both creds set");
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
        }
        let cli = CliConfig::parse_from(["svc-presentation", "--dev-mode"]);
        let cfg = Config::from_cli(cli).expect("secret is set");
        let rendered = format!("{cfg:?}");
        assert!(!rendered.contains("super-secret-db-pass"));
        clear_secret_env();
    }
}
