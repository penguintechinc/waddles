//! Environment-driven configuration.
//!
//! Non-secret operational settings are parsed via `clap` (CLI flags with an
//! `env` fallback) for operability, matching `core/svc_streaming/src/
//! config.rs`. Platform credentials (`TWITCH_IRC_OAUTH_TOKEN`,
//! `DISCORD_BOT_TOKEN`, `ENVELOPE_BINDING_KEYS`) are read directly from the
//! environment only, never as a CLI flag, per `rules/critical-rules.md`
//! Token & Secret Hygiene. The full intake configuration table from
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` S4.1
//! (`TWITCH_EVENTSUB_MODE`, `INTAKE_RATE_LIMIT_*`,
//! `WADDLES_INGEST_TRUSTED_PROXIES`, per-source HMAC secrets, etc.) lands
//! with the generic-webhook/JWT intake work -- see the `TODO(M5)` seam in
//! `src/lib.rs`. Per that spec section, ingest has no database, so unlike
//! `svc_process`/`svc_action` there is no `DB_PASSWORD`-style required
//! secret here.

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

/// A secret value whose `Debug` implementation never prints the underlying
/// bytes -- guards against accidental exposure via `tracing::debug!(?cfg)`
/// or a panic message. Mirrors `core/svc_process/src/config.rs::Secret`.
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
    /// Feeds `penguin_spine::Scope` for every fixed-platform receiver
    /// (`Config::ingest_scope`); not yet consumed by any HTTP handler
    /// (no tenant-scoped routes exist until the generic intake work lands).
    #[arg(long, env = "RUNNER_TENANT_SLUG", default_value = "global")]
    pub runner_tenant_slug: String,

    /// This runner instance's community scope, empty meaning tenant-wide
    /// (`penguin_spine::Scope`'s `_tenant` segment) -- see
    /// `Config::ingest_scope`.
    #[arg(long, env = "RUNNER_COMMUNITY", default_value = "")]
    pub runner_community: String,

    /// Base URL of hub-api's REST surface. Used by the dynamic Twitch
    /// connections resolver (`crate::connections`, flag
    /// `waddles.ingest-dynamic-connections`) for the internal
    /// provider-connections routes, and reported as a configuration snapshot
    /// on `/health`. hub-api serves this port as plain in-cluster HTTP (same
    /// listener as the machine-JWT token endpoint); an `https://` base is
    /// supported together with [`Self::hub_api_rest_ca_file`].
    #[arg(long, env = "HUB_API_URL", default_value = "http://hub-api:8204")]
    pub hub_api_url: String,

    /// PEM CA bundle trusted for an `https://` [`Self::hub_api_url`] -- the
    /// ONLY trust anchor when set (system roots disabled), mirroring
    /// `core/svc_action`'s `HUB_API_GRPC_CA_FILE`. Empty (default) for the
    /// plain in-cluster HTTP base; setting it with an `http://` URL is a
    /// startup error (it would imply TLS protection that is not there).
    #[arg(long, env = "HUB_API_REST_CA_FILE", default_value = "")]
    pub hub_api_rest_ca_file: String,

    /// Deployment tier -- set chart-wide via the shared ConfigMap's
    /// `DEPLOYMENT_TIER` key (`global.deploymentTier`). Consulted only by the
    /// dynamic-connections transport policy
    /// ([`crate::connections::client::enforce_transport_policy`]): plaintext
    /// `http://` to hub-api is tolerated for `alpha`/`local` only. Defaults to
    /// `production` so an unset value **fails closed** (a bare binary run never
    /// silently gets the alpha allowance).
    #[arg(long, env = "DEPLOYMENT_TIER", default_value = "production")]
    pub deployment_tier: String,

    /// Explicit acknowledgement that a plaintext `http://` [`Self::hub_api_url`]
    /// is acceptable OUTSIDE alpha/local (e.g. a CNI that encrypts all pod
    /// traffic). Without it a non-alpha deployment with the dynamic-connections
    /// flag ON refuses to start: the bearer machine JWT goes out, and the
    /// decrypted OAuth token comes back, in cleartext. Prefer an `https://`
    /// base + `HUB_API_REST_CA_FILE`.
    #[arg(long, env = "HUB_API_ALLOW_INSECURE_HTTP", default_value_t = false)]
    pub hub_api_allow_insecure_http: bool,

    /// hub-api's machine-JWT bootstrap endpoint (`POST /internal/
    /// service-token`, `service_auth::MachineJwtClient`), e.g.
    /// `http://waddlebot-hub-api-v3:8204/internal/service-token`. Required
    /// when `waddles.ingest-dynamic-connections` is ON -- startup fails loud
    /// without it (no silent fallback to the static path). Same env var name
    /// as `core/svc_action`.
    #[arg(long, env = "SERVICE_JWT_TOKEN_ENDPOINT", default_value = "")]
    pub service_jwt_token_endpoint: String,

    /// Path to this pod's audience-scoped projected ServiceAccount token,
    /// exchanged for a machine JWT (`connections:resolve`). Same default and
    /// env var name as `core/svc_action`.
    #[arg(
        long,
        env = "SERVICE_JWT_SA_TOKEN_PATH",
        default_value = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )]
    pub service_jwt_sa_token_path: String,

    /// Seconds between dynamic-connection reconcile passes (list bindings, diff
    /// against running bots). 5..=3600.
    #[arg(long, env = "INGEST_CONNECTIONS_POLL_SECS", default_value_t = 60)]
    pub connections_poll_secs: u64,

    /// Page size for hub-api's cursor-paged binding list. 1..=1000 (hub-api's
    /// own `MAX_PAGE_SIZE`).
    #[arg(long, env = "INGEST_CONNECTIONS_PAGE_SIZE", default_value_t = 500)]
    pub connections_page_size: u32,

    /// Per-request timeout, in seconds, for hub-api list/resolve calls.
    /// 1..=120.
    #[arg(
        long,
        env = "INGEST_CONNECTIONS_HTTP_TIMEOUT_SECS",
        default_value_t = 10
    )]
    pub connections_http_timeout_secs: u64,

    /// How a dynamic binding maps to a spine scope: `binding` (default; use the
    /// binding's `tenantSlug`/`communityName`, skip bindings without them --
    /// fail closed) or `runner` (publish everything under
    /// `RUNNER_TENANT_SLUG`/`RUNNER_COMMUNITY`, single-tenant deployments only
    /// -- an explicit opt-in, see `crate::connections::supervisor::ScopeMode`).
    #[arg(long, env = "INGEST_DYNAMIC_SCOPE_MODE", default_value = "binding")]
    pub dynamic_scope_mode: String,

    /// The `kid` every newly-minted `binding.mac` is stamped with
    /// (`penguin_spine::compute_binding_mac`'s `kid` argument) -- must name
    /// a key present in `ENVELOPE_BINDING_KEYS`. Non-secret (a key
    /// *version* label, not key material).
    #[arg(long, env = "ENVELOPE_BINDING_ACTIVE_KID", default_value = "")]
    pub binding_active_kid: String,

    /// Twitch IRC server host. Twitch's own default; overridable for tests
    /// against a local fake server.
    #[arg(long, env = "TWITCH_IRC_HOST", default_value = "irc.chat.twitch.tv")]
    pub twitch_irc_host: String,

    /// Twitch IRC server port (`6697` TLS, `6667` plaintext).
    #[arg(long, env = "TWITCH_IRC_PORT", default_value_t = 6697)]
    pub twitch_irc_port: u16,

    /// The bot's own IRC nick. Empty (the default) means the Twitch IRC
    /// receiver is not started -- see `Config::twitch_irc_enabled`.
    #[arg(long, env = "TWITCH_IRC_NICK", default_value = "")]
    pub twitch_irc_nick: String,

    /// The channel to join. Empty (the default) means the Twitch IRC
    /// receiver is not started -- see `Config::twitch_irc_enabled`.
    #[arg(long, env = "TWITCH_IRC_CHANNEL", default_value = "")]
    pub twitch_irc_channel: String,

    /// Whether to wrap the Twitch IRC socket in TLS. Twitch requires this
    /// on port `6697`; only disabled for tests against a local plaintext
    /// fake server.
    #[arg(long, env = "TWITCH_IRC_USE_TLS", default_value_t = true)]
    pub twitch_irc_use_tls: bool,

    /// Override for the Discord Gateway WebSocket URL. Empty (the default)
    /// means `penguin_connector_discord::gateway::GatewayConfig::new`'s own
    /// production default is used; only overridden for tests against a
    /// local fake gateway (`crate::lib::try_start_discord`).
    #[arg(long, env = "DISCORD_GATEWAY_URL", default_value = "")]
    pub discord_gateway_url: String,

    /// Cap on the exponential backoff between spine-connect retries
    /// (`crate::lib::connect_spine_with_retry`) -- 1s/2s/4s/.../capped here,
    /// never unboundedly long. Regression: a one-shot Valkey probe used to
    /// permanently disable the Discord/Twitch receivers on a fresh-install
    /// cluster where Valkey wasn't up yet (alpha 2026-10-02).
    #[arg(long, env = "SPINE_CONNECT_BACKOFF_MAX_SECS", default_value_t = 30)]
    pub spine_connect_backoff_max_secs: u64,

    /// How long a receiver keeps retrying its spine connect before this
    /// process gives up and exits non-zero so Kubernetes restarts it
    /// visibly (`crate::lib::connect_spine_with_retry`) -- a receiver that
    /// never connects must never leave the pod looking healthy forever.
    #[arg(long, env = "SPINE_CONNECT_GRACE_SECS", default_value_t = 120)]
    pub spine_connect_grace_secs: u64,
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
        if !(5..=3600).contains(&self.connections_poll_secs) {
            return Err(ConfigError::InvalidValue {
                field: "connections_poll_secs",
                reason: format!(
                    "INGEST_CONNECTIONS_POLL_SECS must be 5..=3600, got {}",
                    self.connections_poll_secs
                ),
            });
        }
        if !(1..=1000).contains(&self.connections_page_size) {
            return Err(ConfigError::InvalidValue {
                field: "connections_page_size",
                reason: format!(
                    "INGEST_CONNECTIONS_PAGE_SIZE must be 1..=1000, got {}",
                    self.connections_page_size
                ),
            });
        }
        if !(1..=120).contains(&self.connections_http_timeout_secs) {
            return Err(ConfigError::InvalidValue {
                field: "connections_http_timeout_secs",
                reason: format!(
                    "INGEST_CONNECTIONS_HTTP_TIMEOUT_SECS must be 1..=120, got {}",
                    self.connections_http_timeout_secs
                ),
            });
        }
        if !matches!(
            self.dynamic_scope_mode.trim().to_ascii_lowercase().as_str(),
            "binding" | "runner"
        ) {
            return Err(ConfigError::InvalidValue {
                field: "dynamic_scope_mode",
                reason: format!(
                    "INGEST_DYNAMIC_SCOPE_MODE must be `binding` or `runner`, got {:?}",
                    self.dynamic_scope_mode
                ),
            });
        }
        Ok(())
    }
}

/// Fully-loaded runtime configuration: CLI/env operational settings plus
/// platform credentials read directly from the environment (never a CLI
/// flag). No secret here is *required* -- a service with none configured
/// still starts and serves `/health`/`/healthz`/`/metrics`; each
/// credential's absence independently disables exactly the receiver that
/// needs it (`Config::twitch_irc_enabled`/`discord_enabled`/
/// `binding_keyring`), the same graceful-degradation contract
/// `core/svc_process`'s `try_start_spine_drain` uses for a missing
/// `SpineConfig`.
#[derive(Clone)]
pub struct Config {
    pub cli: CliConfig,
    /// `TWITCH_IRC_OAUTH_TOKEN` -- the raw OAuth token
    /// `crate::ingest::twitch::irc_config` prefixes with `oauth:` before
    /// sending it as the IRC `PASS`. Also the outbound relay sender's own
    /// credential (`crate::outbound`) -- see that module's "Credential
    /// resolution" doc.
    pub twitch_irc_oauth_token: Option<Secret>,
    /// `DISCORD_BOT_TOKEN` -- sent verbatim in the Gateway `IDENTIFY`
    /// (`crate::lib::try_start_discord`).
    pub discord_bot_token: Option<Secret>,
    /// `ENVELOPE_BINDING_KEYS`, the `KeyRing::parse` wire shape
    /// (`kid1:hexkey1,kid2:hexkey2`, spec S5.11/S12.3).
    pub envelope_binding_keys: Option<Secret>,
    /// `TWITCH_EVENTSUB_SECRET` -- the fallback/default EventSub HMAC
    /// secret consumed by `ingest::twitch_eventsub::EnvSecretResolver`.
    /// **Alpha/test-only**: a single-tenant, env-backed
    /// `SubscriptionSecretResolver` impl behind that trait -- the
    /// credential-broker-backed impl (per-connection secret keyed by
    /// subscription/broadcaster, docs/superpowers/specs/2026-09-28-
    /// connections-credentials-design.md §2) lands in a later increment.
    pub twitch_eventsub_secret: Option<Secret>,
}

impl fmt::Debug for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Config")
            .field("cli", &self.cli)
            .field(
                "twitch_irc_oauth_token",
                &self
                    .twitch_irc_oauth_token
                    .as_ref()
                    .map(|_| Secret::new("")),
            )
            .field(
                "discord_bot_token",
                &self.discord_bot_token.as_ref().map(|_| Secret::new("")),
            )
            .field(
                "envelope_binding_keys",
                &self.envelope_binding_keys.as_ref().map(|_| Secret::new("")),
            )
            .field(
                "twitch_eventsub_secret",
                &self
                    .twitch_eventsub_secret
                    .as_ref()
                    .map(|_| Secret::new("")),
            )
            .finish()
    }
}

impl Config {
    /// Loads configuration from CLI args + environment.
    pub fn load() -> Result<Self, ConfigError> {
        let cli = CliConfig::parse();
        Self::from_cli(cli)
    }

    /// Builds a [`Config`] from an already-parsed [`CliConfig`], reading
    /// optional secrets from the environment. Split out from [`Self::load`]
    /// so tests can supply CLI args explicitly without depending on process
    /// argv.
    pub fn from_cli(cli: CliConfig) -> Result<Self, ConfigError> {
        cli.validate()?;
        Ok(Self {
            cli,
            twitch_irc_oauth_token: std::env::var("TWITCH_IRC_OAUTH_TOKEN")
                .ok()
                .map(Secret::new),
            discord_bot_token: std::env::var("DISCORD_BOT_TOKEN").ok().map(Secret::new),
            envelope_binding_keys: std::env::var("ENVELOPE_BINDING_KEYS").ok().map(Secret::new),
            twitch_eventsub_secret: std::env::var("TWITCH_EVENTSUB_SECRET")
                .ok()
                .map(Secret::new),
        })
    }

    /// The `penguin_spine::Scope` every fixed-platform receiver mints
    /// envelopes under -- `runner_community` empty renders as
    /// `Scope`'s tenant-wide segment (`_tenant`), never an empty-string
    /// community.
    pub fn ingest_scope(&self) -> penguin_spine::Scope {
        let community = if self.cli.runner_community.is_empty() {
            None
        } else {
            Some(self.cli.runner_community.clone())
        };
        penguin_spine::Scope::new(self.cli.runner_tenant_slug.clone(), community)
    }

    /// True when enough configuration is present for a Twitch IRC receiver
    /// to start (a nick, a channel, and an OAuth token all set) --
    /// `crate::lib::try_start_twitch_irc`.
    pub fn twitch_irc_enabled(&self) -> bool {
        !self.cli.twitch_irc_nick.is_empty()
            && !self.cli.twitch_irc_channel.is_empty()
            && self.twitch_irc_oauth_token.is_some()
    }

    /// True when a Discord bot token is configured --
    /// `crate::lib::try_start_discord`.
    pub fn discord_enabled(&self) -> bool {
        self.discord_bot_token.is_some()
    }

    /// Builds the D30 binding keyring from `ENVELOPE_BINDING_KEYS`, or
    /// `None` if that variable is unset -- distinguishing "not configured"
    /// (a receiver simply won't start) from "configured but invalid" (a
    /// `KeyRing::parse` error), so callers can log each case distinctly,
    /// same as `core/svc_process::try_start_spine_drain`'s two-reason
    /// no-start contract.
    pub fn binding_keyring(
        &self,
    ) -> Option<Result<penguin_spine::KeyRing, penguin_spine::BindingError>> {
        self.envelope_binding_keys
            .as_ref()
            .map(|s| penguin_spine::KeyRing::parse(s.expose()))
    }
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
            "TWITCH_IRC_OAUTH_TOKEN",
            "DISCORD_BOT_TOKEN",
            "ENVELOPE_BINDING_KEYS",
        ] {
            // SAFETY: serialized by ENV_LOCK, no concurrent readers/writers
            // of these specific variables within the test process.
            unsafe { std::env::remove_var(var) };
        }
    }

    #[test]
    fn defaults_parse_from_empty_args() {
        let cli = CliConfig::parse_from(["svc-ingest"]);
        assert_eq!(cli.http_port, 8200);
        assert_eq!(cli.metrics_port, 9090);
        assert_eq!(cli.runner_tenant_slug, "global");
        assert_eq!(cli.runner_community, "");
        assert_eq!(cli.twitch_irc_host, "irc.chat.twitch.tv");
        assert_eq!(cli.twitch_irc_port, 6697);
        assert!(cli.twitch_irc_use_tls);
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
    fn dynamic_connections_defaults_are_valid_and_conservative() {
        let cli = CliConfig::parse_from(["svc-ingest"]);
        assert_eq!(cli.connections_poll_secs, 60);
        assert_eq!(cli.connections_page_size, 500);
        assert_eq!(cli.connections_http_timeout_secs, 10);
        assert_eq!(cli.dynamic_scope_mode, "binding", "fail-closed default");
        assert_eq!(cli.hub_api_rest_ca_file, "");
        assert_eq!(cli.deployment_tier, "production", "unset tier fails closed");
        assert!(
            !cli.hub_api_allow_insecure_http,
            "insecure-ack defaults off"
        );
        assert_eq!(cli.service_jwt_token_endpoint, "");
        assert_eq!(
            cli.service_jwt_sa_token_path,
            "/var/run/secrets/kubernetes.io/serviceaccount/token"
        );
        cli.validate().expect("defaults valid");
    }

    #[test]
    fn dynamic_connections_bounds_are_enforced() {
        for args in [
            ["--connections-poll-secs", "4"],
            ["--connections-poll-secs", "3601"],
            ["--connections-page-size", "0"],
            ["--connections-page-size", "1001"],
            ["--connections-http-timeout-secs", "0"],
            ["--connections-http-timeout-secs", "121"],
            ["--dynamic-scope-mode", "auto"],
        ] {
            let cli = CliConfig::parse_from(["svc-ingest", args[0], args[1]]);
            assert!(cli.validate().is_err(), "{args:?} must fail validation");
        }
        let ok = CliConfig::parse_from(["svc-ingest", "--dynamic-scope-mode", "Runner"]);
        assert!(ok.validate().is_ok(), "mode is case-insensitive");
    }

    #[test]
    fn zero_metrics_port_fails_validation() {
        let cli = CliConfig::parse_from(["svc-ingest", "--metrics-port", "0"]);
        assert!(cli.validate().is_err());
    }

    #[test]
    fn load_succeeds_with_defaults() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).expect("no required secrets in this service");
        assert_eq!(cfg.cli.hub_api_url, "http://hub-api:8204");
        assert!(cfg.twitch_irc_oauth_token.is_none());
        assert!(cfg.discord_bot_token.is_none());
        assert!(cfg.envelope_binding_keys.is_none());
    }

    #[test]
    fn debug_impl_does_not_panic() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).unwrap();
        let rendered = format!("{cfg:?}");
        assert!(rendered.contains("Config"));
    }

    #[test]
    fn debug_never_prints_secret_bytes() {
        let secret = Secret::new("super-secret-oauth-token");
        let rendered = format!("{secret:?}");
        assert!(!rendered.contains("super-secret-oauth-token"));
        assert!(rendered.contains("redacted"));
    }

    #[test]
    fn config_debug_never_prints_secret_bytes() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("TWITCH_IRC_OAUTH_TOKEN", "super-secret-oauth-token");
            std::env::set_var("DISCORD_BOT_TOKEN", "super-secret-bot-token");
            std::env::set_var("ENVELOPE_BINDING_KEYS", "k1:aabbcc");
        }
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).unwrap();
        let rendered = format!("{cfg:?}");
        assert!(!rendered.contains("super-secret-oauth-token"));
        assert!(!rendered.contains("super-secret-bot-token"));
        assert!(!rendered.contains("aabbcc"));
        clear_secret_env();
    }

    #[test]
    fn ingest_scope_renders_empty_community_as_none() {
        let cli = CliConfig::parse_from(["svc-ingest", "--runner-tenant-slug", "acme"]);
        let cfg = Config {
            cli,
            twitch_irc_oauth_token: None,
            discord_bot_token: None,
            envelope_binding_keys: None,
            twitch_eventsub_secret: None,
        };
        let scope = cfg.ingest_scope();
        assert_eq!(scope.tenant, "acme");
        assert_eq!(scope.community, None);
    }

    #[test]
    fn ingest_scope_carries_a_non_empty_community() {
        let cli = CliConfig::parse_from([
            "svc-ingest",
            "--runner-tenant-slug",
            "acme",
            "--runner-community",
            "main",
        ]);
        let cfg = Config {
            cli,
            twitch_irc_oauth_token: None,
            discord_bot_token: None,
            envelope_binding_keys: None,
            twitch_eventsub_secret: None,
        };
        let scope = cfg.ingest_scope();
        assert_eq!(scope.community.as_deref(), Some("main"));
    }

    #[test]
    fn twitch_irc_enabled_requires_nick_channel_and_token() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).unwrap();
        assert!(!cfg.twitch_irc_enabled(), "nothing configured");

        let cli = CliConfig::parse_from([
            "svc-ingest",
            "--twitch-irc-nick",
            "waddlebot",
            "--twitch-irc-channel",
            "somechannel",
        ]);
        let cfg = Config::from_cli(cli).unwrap();
        assert!(
            !cfg.twitch_irc_enabled(),
            "nick+channel without an oauth token must not enable the receiver"
        );

        // SAFETY: serialized by ENV_LOCK above.
        unsafe { std::env::set_var("TWITCH_IRC_OAUTH_TOKEN", "test-token") };
        let cli = CliConfig::parse_from([
            "svc-ingest",
            "--twitch-irc-nick",
            "waddlebot",
            "--twitch-irc-channel",
            "somechannel",
        ]);
        let cfg = Config::from_cli(cli).unwrap();
        assert!(cfg.twitch_irc_enabled());
        clear_secret_env();
    }

    #[test]
    fn discord_enabled_requires_a_bot_token() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).unwrap();
        assert!(!cfg.discord_enabled());

        // SAFETY: serialized by ENV_LOCK above.
        unsafe { std::env::set_var("DISCORD_BOT_TOKEN", "test-token") };
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).unwrap();
        assert!(cfg.discord_enabled());
        clear_secret_env();
    }

    #[test]
    fn binding_keyring_is_none_when_unconfigured() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).unwrap();
        assert!(cfg.binding_keyring().is_none());
    }

    #[test]
    fn binding_keyring_parses_a_valid_keys_string() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var(
                "ENVELOPE_BINDING_KEYS",
                "k1:0102030405060708090a0b0c0d0e0f10",
            );
        }
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).unwrap();
        let ring = cfg
            .binding_keyring()
            .expect("configured")
            .expect("valid keys string");
        // KeyRing has no public accessor beyond compute_binding_mac itself;
        // proving it parsed is proving a mac can be computed under "k1".
        assert!(penguin_spine::compute_binding_mac(
            &ring, "k1", "acme", None, "ws-1", "evt-1", None
        )
        .is_ok());
        clear_secret_env();
    }

    #[test]
    fn binding_keyring_surfaces_a_malformed_keys_string_as_configured_but_invalid() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe { std::env::set_var("ENVELOPE_BINDING_KEYS", "not-a-valid-entry") };
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let cfg = Config::from_cli(cli).unwrap();
        assert!(cfg.binding_keyring().expect("configured").is_err());
        clear_secret_env();
    }
}
