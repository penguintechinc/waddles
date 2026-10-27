//! Environment-driven configuration.
//!
//! Non-secret operational settings are parsed via `clap` (CLI flags with an
//! `env` fallback) for operability. Anything secret (DB/cache passwords,
//! the service-to-service API key, JWT signing material) is read directly
//! from the environment only and is never exposed as a CLI flag -- per
//! `rules/critical-rules.md` Token & Secret Hygiene ("Pass as CLI args" is
//! never allowed for secrets). Secrets are never `Debug`/`Display`-printed.

use std::fmt;
use std::net::IpAddr;
use std::path::PathBuf;

use clap::Parser;
use thiserror::Error;

use crate::pipeline::encoder::EncoderPreference;

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
    name = "svc-streaming",
    version,
    about = "Waddles A/V data-plane service"
)]
pub struct CliConfig {
    /// Control-plane HTTP port (axum router: /health, /readyz, /api/v1/*).
    #[arg(long, env = "MODULE_PORT", default_value_t = 8208)]
    pub http_port: u16,

    /// Prometheus `/metrics` exposition port.
    #[arg(long, env = "METRICS_PORT", default_value_t = 9090)]
    pub metrics_port: u16,

    /// Address the HTTP/metrics listeners bind to.
    #[arg(long, env = "BIND_ADDR", default_value = "0.0.0.0")]
    pub bind_addr: IpAddr,

    /// RTMP ingest listener port (owned by `ingest::rtmp`, not yet started).
    #[arg(long, env = "RTMP_PORT", default_value_t = 1935)]
    pub rtmp_port: u16,

    /// SRT ingest listener port (owned by `ingest::srt`, not yet started).
    #[arg(long, env = "SRT_PORT", default_value_t = 9000)]
    pub srt_port: u16,

    /// Inclusive UDP port range reserved for WebRTC/WHIP ICE candidates,
    /// formatted `"<start>-<end>"`.
    #[arg(long, env = "WEBRTC_UDP_RANGE", default_value = "40000-40100")]
    pub webrtc_udp_range: String,

    /// Local filesystem root for recordings/segments before they are
    /// pushed to `object_store`-backed remote storage.
    #[arg(
        long,
        env = "STREAM_DATA_DIR",
        default_value = "/var/lib/svc-streaming"
    )]
    pub stream_data_dir: PathBuf,

    /// Path to the `ffmpeg` binary this service shells out to.
    #[arg(long, env = "FFMPEG_PATH", default_value = "/usr/bin/ffmpeg")]
    pub ffmpeg_path: PathBuf,

    /// Video encoder preference: `auto` (a GPU encoder per codec when a
    /// startup trial encode proves it works, else the CPU encoder), `cpu`,
    /// `nvenc` or `vaapi`. Every value falls back to the CPU encoder, per
    /// codec, when its GPU path is unusable -- the CPU path works
    /// everywhere.
    #[arg(long, env = "STREAM_ENCODER", default_value = "auto")]
    pub stream_encoder: EncoderPreference,

    /// VA-API render node used for GPU encoding (e.g.
    /// `/dev/dri/renderD128`); the lowest-numbered `renderD*` node is
    /// auto-discovered when unset.
    #[arg(long, env = "STREAM_VAAPI_DEVICE")]
    pub stream_vaapi_device: Option<PathBuf>,

    /// Externally-reachable base URL used to build playback/webhook links.
    #[arg(long, env = "PUBLIC_BASE_URL", default_value = "http://localhost:8208")]
    pub public_base_url: String,

    /// Postgres/sqlite host (per-service DB account, never a shared credential).
    #[arg(long, env = "DB_HOST", default_value = "localhost")]
    pub db_host: String,
    #[arg(long, env = "DB_PORT", default_value_t = 5432)]
    pub db_port: u16,
    #[arg(long, env = "DB_NAME", default_value = "waddlebot")]
    pub db_name: String,
    #[arg(long, env = "DB_USER", default_value = "svc_streaming")]
    pub db_user: String,

    /// Cache (Redis-compatible) host, used for pipeline/session coordination.
    #[arg(long, env = "CACHE_HOST", default_value = "localhost")]
    pub cache_host: String,
    #[arg(long, env = "CACHE_PORT", default_value_t = 6379)]
    pub cache_port: u16,

    /// Expected JWT `iss` claim.
    #[arg(
        long,
        env = "JWT_ISSUER",
        default_value = "https://auth.penguintech.io"
    )]
    pub jwt_issuer: String,
    /// Expected JWT `aud` claim.
    #[arg(long, env = "JWT_AUDIENCE", default_value = "svc-streaming")]
    pub jwt_audience: String,
    /// JWKS endpoint for RS256 verification; when unset, HS256 with
    /// `JWT_HMAC_SECRET` (env-only, see [`Config::load`]) is used instead.
    #[arg(long, env = "JWT_JWKS_URL")]
    pub jwt_jwks_url: Option<String>,

    /// Base URL of hub-api's token ledger (`POST
    /// /api/v1/marketplace/communities/{id}/tokens/debit`) -- see
    /// `crate::billing::token_ledger` for the TRANSCODE admission check
    /// this backs. Not a secret: it's a plain service address.
    #[arg(long, env = "HUB_API_URL", default_value = "http://hub-api:8204")]
    pub hub_api_url: String,

    /// Tokens debited per transcode-admission attempt. Mirrors the Python
    /// alpha's `Config.transcode_token_cost`.
    #[arg(long, env = "TRANSCODE_TOKEN_COST", default_value_t = 5)]
    pub transcode_token_cost: i64,

    /// hub-api marketplace product key billed for a transcode-admission
    /// debit. Mirrors the Python alpha's `Config.transcode_product_key`.
    #[arg(
        long,
        env = "TRANSCODE_PRODUCT_KEY",
        default_value = "transcoding_minutes"
    )]
    pub transcode_product_key: String,
}

impl CliConfig {
    /// Validates cross-field invariants that `clap`'s per-arg parsing can't
    /// express (e.g. the `WEBRTC_UDP_RANGE` bounds).
    pub fn validate(&self) -> Result<(), ConfigError> {
        parse_udp_range(&self.webrtc_udp_range)?;
        if self.http_port == 0 || self.metrics_port == 0 {
            return Err(ConfigError::InvalidValue {
                field: "http_port/metrics_port",
                reason: "port 0 is not a valid bind port".to_string(),
            });
        }
        Ok(())
    }
}

/// Parses `"<start>-<end>"` into an inclusive `(u16, u16)` range, validating
/// that `start <= end`.
pub fn parse_udp_range(raw: &str) -> Result<(u16, u16), ConfigError> {
    let (start, end) = raw
        .split_once('-')
        .ok_or_else(|| ConfigError::InvalidValue {
            field: "webrtc_udp_range",
            reason: format!("expected \"<start>-<end>\", got {raw:?}"),
        })?;
    let start: u16 = start
        .trim()
        .parse()
        .map_err(|_| ConfigError::InvalidValue {
            field: "webrtc_udp_range",
            reason: format!("{start:?} is not a valid port"),
        })?;
    let end: u16 = end.trim().parse().map_err(|_| ConfigError::InvalidValue {
        field: "webrtc_udp_range",
        reason: format!("{end:?} is not a valid port"),
    })?;
    if start > end {
        return Err(ConfigError::InvalidValue {
            field: "webrtc_udp_range",
            reason: format!("start {start} is greater than end {end}"),
        });
    }
    Ok((start, end))
}

/// Fully-loaded runtime configuration: operational settings plus secrets
/// pulled directly from the environment (never via CLI flag).
#[derive(Clone)]
pub struct Config {
    pub cli: CliConfig,
    pub db_password: Secret,
    pub cache_password: Option<Secret>,
    pub service_api_key: Secret,
    pub jwt_hmac_secret: Option<Secret>,
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
                "jwt_hmac_secret",
                &self.jwt_hmac_secret.as_ref().map(|_| Secret::new("")),
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
        let jwt_hmac_secret = std::env::var("JWT_HMAC_SECRET").ok().map(Secret::new);
        Ok(Self {
            cli,
            db_password,
            cache_password,
            service_api_key,
            jwt_hmac_secret,
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
            "JWT_HMAC_SECRET",
        ] {
            // SAFETY: serialized by ENV_LOCK, no concurrent readers/writers
            // of these specific variables within the test process.
            unsafe { std::env::remove_var(var) };
        }
    }

    #[test]
    fn defaults_parse_from_empty_args() {
        let cli = CliConfig::parse_from(["svc-streaming"]);
        assert_eq!(cli.http_port, 8208);
        assert_eq!(cli.metrics_port, 9090);
        assert_eq!(cli.rtmp_port, 1935);
        assert_eq!(cli.srt_port, 9000);
        assert_eq!(cli.webrtc_udp_range, "40000-40100");
        assert_eq!(cli.hub_api_url, "http://hub-api:8204");
        assert_eq!(cli.transcode_token_cost, 5);
        assert_eq!(cli.transcode_product_key, "transcoding_minutes");
        cli.validate().expect("defaults must be valid");
    }

    #[test]
    fn cli_flag_overrides_default() {
        let cli = CliConfig::parse_from(["svc-streaming", "--http-port", "9000"]);
        assert_eq!(cli.http_port, 9000);
    }

    #[test]
    fn parse_udp_range_accepts_valid_range() {
        assert_eq!(parse_udp_range("40000-40100"), Ok((40000, 40100)));
    }

    #[test]
    fn parse_udp_range_rejects_missing_dash() {
        assert!(parse_udp_range("40000").is_err());
    }

    #[test]
    fn parse_udp_range_rejects_inverted_range() {
        assert!(parse_udp_range("40100-40000").is_err());
    }

    #[test]
    fn parse_udp_range_rejects_non_numeric() {
        assert!(parse_udp_range("abc-def").is_err());
    }

    #[test]
    fn zero_port_fails_validation() {
        let cli = CliConfig::parse_from(["svc-streaming", "--http-port", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn load_fails_without_required_secrets() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-streaming"]);
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
        let cli = CliConfig::parse_from(["svc-streaming"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert_eq!(cfg.db_password.expose(), "test-db-pass");
        assert_eq!(cfg.service_api_key.expose(), "test-api-key");
        assert!(cfg.cache_password.is_none());
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
        let cli = CliConfig::parse_from(["svc-streaming"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        let rendered = format!("{cfg:?}");
        assert!(!rendered.contains("super-secret-db-pass"));
        assert!(!rendered.contains("super-secret-api-key"));
        clear_secret_env();
    }
}
