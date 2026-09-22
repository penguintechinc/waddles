//! Environment-driven configuration.
//!
//! Non-secret operational settings are parsed via `clap` (CLI flags with an
//! `env` fallback) for operability, matching `core/svc_streaming/src/
//! config.rs`. This skeleton only carries the settings the health/metrics/
//! telemetry surface needs; the full intake configuration table from
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` S4.1
//! (`TWITCH_EVENTSUB_MODE`, `INTAKE_RATE_LIMIT_*`, `WADDLES_INGEST_TRUSTED_PROXIES`,
//! per-source HMAC secrets, etc.) lands with the connector/intake work --
//! see the `TODO(M5)` seam in `src/lib.rs`. Per that spec section, ingest
//! has no database, so unlike `svc_process`/`svc_action` there is no
//! `DB_PASSWORD`-style required secret here yet.

use std::fmt;
use std::net::IpAddr;

use clap::Parser;
use thiserror::Error;

/// Errors that can occur while loading configuration.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum ConfigError {
    /// A value was present but failed validation.
    #[error("invalid value for {field}: {reason}")]
    InvalidValue { field: &'static str, reason: String },
}

/// CLI/env-configurable operational settings. Every field has an `env`
/// fallback so Helm/Docker deployments never need CLI args.
#[derive(Parser, Debug, Clone)]
#[command(
    name = "svc-ingest",
    version,
    about = "Waddles chat/event data-plane ingest service"
)]
pub struct CliConfig {
    /// Control-plane HTTP port (axum router: /health, /healthz). Default
    /// fixed to 8200 to match `pipeline.svcIngest.port` in the Helm chart --
    /// see spec S4.1's "Read at runtime -- fixes the current Dockerfile's
    /// hardcoded 8210 vs chart 8200 conflict" note (that conflict belongs
    /// to the still-deployed Python service; this Rust binary starts from
    /// the corrected default directly).
    #[arg(long, env = "MODULE_PORT", default_value_t = 8200)]
    pub http_port: u16,

    /// Prometheus `/metrics` exposition port.
    #[arg(long, env = "METRICS_PORT", default_value_t = 9090)]
    pub metrics_port: u16,

    /// Address the HTTP/metrics listeners bind to.
    #[arg(long, env = "BIND_ADDR", default_value = "0.0.0.0")]
    pub bind_addr: IpAddr,

    /// This runner instance's own tenant scope -- security.md Tenant
    /// Isolation: fixed at deploy time, never widened at request time.
    /// Not yet consumed by any handler in this skeleton (no tenant-scoped
    /// routes exist until the generic intake work lands).
    #[arg(long, env = "RUNNER_TENANT_SLUG", default_value = "global")]
    pub runner_tenant_slug: String,

    /// Base URL of hub-api's distribution endpoint this service will poll
    /// once the connector/intake work lands (`GET
    /// {HUB_API_URL}/api/v1/distribution/bundles?stage=process`, spec
    /// S4.1). Only used today to report a configuration snapshot on
    /// `/health` -- no outbound call is made by this skeleton.
    #[arg(long, env = "HUB_API_URL", default_value = "http://hub-api:8204")]
    pub hub_api_url: String,
}

impl CliConfig {
    /// Validates cross-field invariants `clap`'s per-arg parsing can't
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

/// Fully-loaded runtime configuration. No secrets are required by this
/// skeleton (no database, no signed-JWT distribution poll yet), so unlike
/// `svc_streaming::config::Config` there is nothing beyond the CLI/env
/// settings today -- kept as its own type regardless so a future secret
/// (e.g. the distribution poll's service-JWT signing key) has a home
/// without changing every call site.
#[derive(Clone)]
pub struct Config {
    pub cli: CliConfig,
}

impl fmt::Debug for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Config").field("cli", &self.cli).finish()
    }
}

impl Config {
    /// Loads configuration from CLI args + environment.
    pub fn load() -> Result<Self, ConfigError> {
        let cli = CliConfig::parse();
        Self::from_cli(cli)
    }

    /// Builds a [`Config`] from an already-parsed [`CliConfig`]. Split out
    /// from [`Self::load`] so tests can supply CLI args explicitly without
    /// depending on process argv.
    pub fn from_cli(cli: CliConfig) -> Result<Self, ConfigError> {
        cli.validate()?;
        Ok(Self { cli })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_parse_from_empty_args() {
        let cli = CliConfig::parse_from(["svc-ingest"]);
        assert_eq!(cli.http_port, 8200);
        assert_eq!(cli.metrics_port, 9090);
        assert_eq!(cli.runner_tenant_slug, "global");
        cli.validate().expect("defaults must be valid");
    }

    #[test]
    fn cli_flag_overrides_default() {
        let cli = CliConfig::parse_from(["svc-ingest", "--http-port", "9000"]);
        assert_eq!(cli.http_port, 9000);
    }

    #[test]
    fn zero_http_port_fails_validation() {
        let cli = CliConfig::parse_from(["svc-ingest", "--http-port", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn zero_metrics_port_fails_validation() {
        let cli = CliConfig::parse_from(["svc-ingest", "--metrics-port", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn load_succeeds_with_defaults() {
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).expect("no required secrets in this skeleton");
        assert_eq!(cfg.cli.hub_api_url, "http://hub-api:8204");
    }

    #[test]
    fn debug_impl_does_not_panic() {
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).unwrap();
        let rendered = format!("{cfg:?}");
        assert!(rendered.contains("Config"));
    }
}
