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
    /// Host-API heartbeat interval: how often the stage sends `ping` to
    /// each connected executor session (fix/executor-link-heartbeat, alpha
    /// 2026-10-02 incident: a rolled svc pod left the executor bound to a
    /// terminated peer with no liveness signal at all). A session is
    /// dropped after [`HEARTBEAT_MISSED_LIMIT`] consecutive missed `pong`s.
    #[arg(long, env = "HEARTBEAT_INTERVAL_MS", default_value_t = 5000)]
    pub heartbeat_interval_ms: u64,
    /// Threshold for `crate::host_api::run_zero_executor_watchdog`'s
    /// periodic ERROR log: how long zero live executor sessions must
    /// persist before the watchdog starts logging loudly on its fixed
    /// cadence. Deliberately NOT wired into `/readyz`/`/healthz`/`/health`
    /// -- regression: readiness gated on executor connection deadlocked
    /// rollouts (alpha 2026-10-02): a bundle-executor dials this service
    /// through its ClusterIP Service, which only routes to Ready pods, so
    /// gating readiness/liveness on executor presence meant a freshly
    /// rolled pod could never become Ready (no executor would ever reach
    /// it) and the rollout stalled forever.
    #[arg(long, env = "EXECUTOR_GRACE_SECONDS", default_value_t = 60)]
    pub executor_grace_seconds: u64,

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

    /// The legacy bundle-selection path's digest source (`crate::
    /// try_start_env_bundle_loader`/`crate::resolve_initial_bundle`) --
    /// active only when `crate::resolve_db_path_active` finds the DB-driven
    /// path (below) inactive at startup (mutual exclusion, `crate::lib`'s
    /// top doc). `try_start_env_bundle_loader` sends this bundle's `load`
    /// frame directly over the host-API connection; `resolve_initial_bundle`
    /// uses the same value for the dispatch loop's `deps.digest`. Empty
    /// (default) disables this path entirely -- the dispatch loop then
    /// starts with an empty digest until either this is set or the DB path
    /// becomes active. Expected shape: `sha256:<64 hex>` -- the executor's
    /// own digest validator (`bundle_executor::invoke::verify_digest`)
    /// rejects a bare hex string with `MalformedDigest`, so the chart must
    /// set this with the prefix already included.
    #[arg(long, env = "ACTION_BUNDLE_DIGEST", default_value = "")]
    pub action_bundle_digest: String,
    /// See [`Self::action_bundle_digest`]. The `load` frame's `version`
    /// field (spec §6.6) -- distinct from the digest, echoed back by the
    /// executor's `loaded` reply.
    #[arg(long, env = "ACTION_BUNDLE_VERSION", default_value = "1")]
    pub action_bundle_version: String,
    /// See [`Self::action_bundle_digest`]. The bucket key `load` asks the
    /// executor to fetch the compiled component from (spec §7.6 step 3's
    /// naming convention) -- this fallback requires the operator to supply
    /// it directly since there is no distribution row to derive it from.
    #[arg(long, env = "ACTION_BUNDLE_COMPONENT_KEY", default_value = "")]
    pub action_bundle_component_key: String,
    /// See [`Self::action_bundle_digest`]. The bucket key for the bundle's
    /// manifest sidecar, same convention as
    /// [`Self::action_bundle_component_key`].
    #[arg(long, env = "ACTION_BUNDLE_SIDECAR_KEY", default_value = "")]
    pub action_bundle_sidecar_key: String,

    /// Interim, config-sourced instance-wide private-IP egress policy
    /// (`bundle_host_http::egress::InstanceEgressPolicy`, Justin's
    /// decision: "private-ip is also subject to the INSTANCE policy --
    /// default deny, global-admin opt-in"). PR #428/#432's capability-gate
    /// grant snapshot is the eventual live source this field is a stopgap
    /// for; until that lands, an operator flips it at the pod level.
    /// Supersedes the removed `EGRESS_ALLOW_PRIVATE_HOSTS` flag, which
    /// never actually gated anything once the three-category grant model
    /// landed (security review finding, PR #468, MEDIUM) -- see
    /// [`CliConfig::validate`] for the startup error if it's still set.
    #[arg(
        long,
        env = "INSTANCE_EGRESS_ALLOW_PRIVATE_IP",
        default_value_t = false
    )]
    pub instance_egress_allow_private_ip: bool,
    /// Comma-separated CIDR blocks (IPv4/IPv6, `bundle_host_http::egress::
    /// ClusterCidrDenylist`) covering this deployment's own pod, Service,
    /// and node ranges -- never liftable by any egress grant or by
    /// `instance_egress_allow_private_ip` (Justin's decision: "a CONFIGURED
    /// denylist of the cluster's own pod, service and node CIDRs"). Empty
    /// (the default) is tolerated only when [`Self::deployment_tier`] is
    /// `alpha`/`local` -- [`CliConfig::validate`] hard-fails startup
    /// otherwise (security review finding, PR #468, MEDIUM: an empty
    /// denylist in beta/gamma/production leaves this stage's own cluster
    /// network reachable via bundle-initiated SSRF).
    #[arg(long, env = "EGRESS_CLUSTER_CIDR_DENYLIST", default_value = "")]
    pub egress_cluster_cidr_denylist: String,
    /// Deployment tier -- already set chart-wide via the shared ConfigMap's
    /// `DEPLOYMENT_TIER` key (`templates/configmap.yaml`, `global.
    /// deploymentTier`, which itself defaults to `"production"` and is
    /// overridden to `"alpha"` only by `values-alpha.yaml`). The only thing
    /// this crate consults it for today is [`Self::
    /// egress_cluster_cidr_denylist`]'s fail-closed check. Defaults to
    /// `"alpha"` here (CLI/test default, matching this struct's other
    /// dev-permissive defaults e.g. `sandbox_gvisor`) -- every non-alpha/
    /// local Helm deployment sets `DEPLOYMENT_TIER` explicitly via the
    /// ConfigMap regardless, so this default only governs a bare local
    /// binary run or an un-updated test fixture, never a real beta/gamma/
    /// production pod.
    #[arg(long, env = "DEPLOYMENT_TIER", default_value = "alpha")]
    pub deployment_tier: String,
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

    // -- DB-driven active-bundle loader (spec: hub-api is the sole writer,
    // this stage reads ACTIVE, APPROVED bundle config from a READ-ONLY
    // Postgres and hot-swaps in/out with no pod restart) -- ENABLED by
    // default; opt out via the `waddles.core.disable-db-bundle-config`
    // kill-switch (`crate::flags`). Mutually exclusive with the legacy
    // `ACTION_BUNDLE_*` env override -- `crate::resolve_db_path_active`
    // picks exactly one, once, at startup (`crate::lib`'s top doc). See
    // `crate::bundle_loader`.
    // Field shapes/defaults mirror `core/svc_process::config::CliConfig`'s
    // identical additions exactly -- same env var names across both
    // services, kept consistent per that crate's own doc rationale.
    /// Reader-endpoint Postgres host for the DB-driven loader -- separate
    /// from `DB_HOST` (the primary, read-write connection above); defaults
    /// to the primary host in alpha (no replica yet).
    #[arg(long, env = "DB_READER_HOST", default_value = "localhost")]
    pub db_reader_host: String,
    #[arg(long, env = "DB_READER_PORT", default_value_t = 5432)]
    pub db_reader_port: u16,
    #[arg(long, env = "DB_READER_NAME", default_value = "waddlebot")]
    pub db_reader_name: String,
    /// A distinct, SELECT-only Postgres role -- never the primary `DB_USER`
    /// account. See `bundle_active_set`'s crate-root doc for the exact
    /// grants this role needs.
    #[arg(long, env = "DB_READER_USER", default_value = "svc_action_ro")]
    pub db_reader_user: String,
    // **`BUNDLE_SCOPE_TENANT_ID`/`BUNDLE_SCOPE_COMMUNITY_ID` REMOVED**
    // (dataplane scale design, user requirement: "every svc_process/
    // svc_action pod serves ALL tenants") -- this loader now discovers and
    // serves every `(tenant_id, community_id)` scope in the database
    // itself (`bundle_active_set::read_active_set_all`), never a single
    // operator-configured scope. There is no replacement env var.
    /// Poll interval, in whole seconds, for the change-log consumer's
    /// incremental tick. Clamped to a 5s floor by
    /// [`CliConfig::bundle_config_poll_interval`] so a misconfigured
    /// `0`/negative value can never hot-loop against the reader database.
    /// Semantics unchanged from the retired single-tenant watermark loader
    /// this replaces -- same env var, same floor.
    #[arg(long, env = "BUNDLE_CONFIG_POLL_SECONDS", default_value_t = 300)]
    pub bundle_config_poll_seconds: i64,
    /// Periodic full-reconcile interval, in whole minutes (dataplane scale
    /// design §7), default `15`. Clamped to a 1m floor by
    /// [`CliConfig::full_reconcile_interval`].
    #[arg(
        long,
        env = "BUNDLE_CONFIG_FULL_RECONCILE_MINUTES",
        default_value_t = 15
    )]
    pub bundle_config_full_reconcile_minutes: i64,
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
        if self.heartbeat_interval_ms == 0 {
            return Err(ConfigError::InvalidValue {
                field: "heartbeat_interval_ms",
                reason: "must be positive".to_string(),
            });
        }
        if self.executor_grace_seconds == 0 {
            return Err(ConfigError::InvalidValue {
                field: "executor_grace_seconds",
                reason: "must be positive".to_string(),
            });
        }
        if self.action_base_backoff_ms > self.action_max_backoff_ms {
            return Err(ConfigError::InvalidValue {
                field: "action_base_backoff_ms/action_max_backoff_ms",
                reason: "base backoff must not exceed the max backoff".to_string(),
            });
        }
        let denylist = self.cluster_cidr_denylist()?;
        if denylist.is_empty() && !matches!(self.deployment_tier.as_str(), "alpha" | "local") {
            return Err(ConfigError::InvalidValue {
                field: "egress_cluster_cidr_denylist",
                reason: format!(
                    "EGRESS_CLUSTER_CIDR_DENYLIST must be set (non-empty) when \
                     DEPLOYMENT_TIER={:?} -- only alpha/local tolerate an empty \
                     cluster CIDR denylist",
                    self.deployment_tier
                ),
            });
        }
        Ok(())
    }

    /// Parses [`Self::egress_cluster_cidr_denylist`] into the guard's own
    /// type. Fails closed on a malformed entry (comma-separated, empty
    /// segments ignored) -- see `bundle_host_http::egress::
    /// ClusterCidrDenylist::parse`'s doc.
    pub fn cluster_cidr_denylist(
        &self,
    ) -> Result<bundle_host_http::egress::ClusterCidrDenylist, ConfigError> {
        let entries: Vec<&str> = self
            .egress_cluster_cidr_denylist
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect();
        bundle_host_http::egress::ClusterCidrDenylist::parse(entries).map_err(|reason| {
            ConfigError::InvalidValue {
                field: "egress_cluster_cidr_denylist",
                reason,
            }
        })
    }

    /// The interim, config-sourced [`bundle_host_http::egress::
    /// InstanceEgressPolicy`] snapshot -- see
    /// [`Self::instance_egress_allow_private_ip`]'s doc.
    pub fn instance_egress_policy(&self) -> bundle_host_http::egress::InstanceEgressPolicy {
        bundle_host_http::egress::InstanceEgressPolicy {
            allow_private_ip_egress: self.instance_egress_allow_private_ip,
        }
    }

    /// [`Self::bundle_config_poll_seconds`] clamped to a 5s floor -- a
    /// misconfigured `0`/negative `BUNDLE_CONFIG_POLL_SECONDS` must never
    /// hot-loop the watermark check against the reader database.
    pub fn bundle_config_poll_interval(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.bundle_config_poll_seconds.max(5) as u64)
    }

    /// [`Self::bundle_config_full_reconcile_minutes`] clamped to a 1-minute
    /// floor -- a misconfigured `0`/negative value must never hot-loop the
    /// full active-set re-read against the reader database.
    pub fn full_reconcile_interval(&self) -> std::time::Duration {
        std::time::Duration::from_secs(
            (self.bundle_config_full_reconcile_minutes.max(1) as u64) * 60,
        )
    }

    /// [`Self::heartbeat_interval_ms`] as a [`std::time::Duration`] --
    /// `validate` already rejects `0`, but this is also used before
    /// `validate` runs in a couple of test helpers, so floor at 1ms rather
    /// than panicking on a zero-length sleep/timeout.
    pub fn heartbeat_interval(&self) -> std::time::Duration {
        std::time::Duration::from_millis(self.heartbeat_interval_ms.max(1))
    }

    /// [`Self::executor_grace_seconds`] as a [`std::time::Duration`].
    pub fn executor_grace(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.executor_grace_seconds.max(1))
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
    /// `DISCORD_BOT_TOKEN` (env-only, optional): the bot token
    /// `crate::capabilities::StageCapabilities::with_discord` authenticates
    /// the Discord relay send with (`Authorization: Bot <token>`). This is
    /// the **same secret key** `templates/secrets.yaml` already renders for
    /// the legacy Python `svc-action` and for `svc-ingest-rust`'s Discord
    /// Gateway connection (`.Values.oauth.discord.botToken` -> the
    /// `DISCORD_BOT_TOKEN` key in the shared `{fullname}-secrets` Secret) --
    /// wiring the Discord relay provider onto this pod needs only a new env
    /// mount in `templates/svc-action-rust.yaml` pointing at that existing
    /// key, no new chart secret. `None` when unset -- not every deployment
    /// enables Discord, and a relay send for "discord" is then refused
    /// `relay_unavailable` (graceful degradation, not a startup failure;
    /// mirrors `envelope_binding_keys` above).
    pub discord_bot_token: Option<Secret>,
    /// `DB_READER_PASSWORD` for the DB-driven active-bundle loader's
    /// read-only Postgres role (`crate::bundle_loader`). Deliberately
    /// `Option`, unlike `db_password`: this loader is enabled by default
    /// (opt out via `waddles.core.disable-db-bundle-config`), so a fresh
    /// alpha deployment that hasn't provisioned the RO role yet must not
    /// fail startup over it -- `crate::resolve_db_path_active` treats this
    /// unset the same as a genuinely-disabled DB path, falling back to the
    /// legacy `ACTION_BUNDLE_*` env override (mutual exclusion, `crate::lib`'s
    /// top doc), the same graceful-degradation contract as
    /// `envelope_binding_keys`/`discord_bot_token` above.
    pub db_reader_password: Option<Secret>,
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
            .field(
                "discord_bot_token",
                &self.discord_bot_token.as_ref().map(|_| "<redacted>"),
            )
            .field(
                "db_reader_password",
                &self.db_reader_password.as_ref().map(|_| Secret::new("")),
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
        let discord_bot_token = std::env::var("DISCORD_BOT_TOKEN").ok().map(Secret::new);
        // Security review fix: Helm always renders the DB_READER_PASSWORD
        // secret key (`templates/secrets.yaml`), defaulting to "" until the
        // RO Postgres role is actually provisioned -- so the env var is
        // always *set*, just empty. Without `.filter(|s| !s.is_empty())`
        // this would be `Some(Secret::new(""))`, never `None`, and
        // `try_start_db_bundle_loader`'s documented "DB_READER_PASSWORD
        // unset -> loader disabled" branch could never fire; an empty
        // value must be treated the same as unset.
        let db_reader_password = std::env::var("DB_READER_PASSWORD")
            .ok()
            .filter(|s| !s.is_empty())
            .map(Secret::new);
        Ok(Self {
            cli,
            db_password,
            envelope_binding_keys,
            discord_bot_token,
            db_reader_password,
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
            std::env::remove_var("DISCORD_BOT_TOKEN");
            std::env::remove_var("DB_READER_PASSWORD");
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
        assert!(!cli.instance_egress_allow_private_ip);
        assert_eq!(cli.egress_rate_limit_rps, 10);
        assert_eq!(cli.egress_rate_limit_burst, 20);
        assert_eq!(cli.egress_timeout_ms, 5000);
        assert_eq!(cli.egress_max_redirects, 3);
        assert_eq!(cli.egress_max_response_bytes, 1_048_576);
    }

    #[test]
    fn instance_egress_allow_private_ip_flag_override_is_honored() {
        let cli = CliConfig::parse_from(["svc-action", "--instance-egress-allow-private-ip"]);
        assert!(cli.instance_egress_allow_private_ip);
        assert!(cli.instance_egress_policy().allow_private_ip_egress);
    }

    /// Security review fix (PR #468, MEDIUM): an empty cluster CIDR
    /// denylist is a hard startup error outside alpha/local -- the default
    /// `deployment_tier` in tests/CLI defaults is `"alpha"`, so this must be
    /// set explicitly to prove the gate actually fires.
    /// regression: `heartbeat_interval_ms: 0` would otherwise sleep on a
    /// zero-length interval -- `validate()` rejects it as a hard startup
    /// error rather than letting the heartbeat loop hot-loop.
    #[test]
    fn heartbeat_interval_ms_zero_is_rejected() {
        let cli = CliConfig::parse_from(["svc-action", "--heartbeat-interval-ms", "0"]);
        assert_eq!(
            cli.validate().unwrap_err(),
            ConfigError::InvalidValue {
                field: "heartbeat_interval_ms",
                reason: "must be positive".to_string(),
            }
        );
    }

    /// regression: `executor_grace_seconds: 0` would make the zero-executor
    /// watchdog fire the instant any executor session blips, even
    /// transiently -- `validate()` rejects it as a hard startup error.
    #[test]
    fn executor_grace_seconds_zero_is_rejected() {
        let cli = CliConfig::parse_from(["svc-action", "--executor-grace-seconds", "0"]);
        assert_eq!(
            cli.validate().unwrap_err(),
            ConfigError::InvalidValue {
                field: "executor_grace_seconds",
                reason: "must be positive".to_string(),
            }
        );
    }

    #[test]
    fn empty_cluster_cidr_denylist_is_rejected_outside_alpha_local() {
        let cli = CliConfig::parse_from(["svc-action", "--deployment-tier", "beta"]);
        let err = cli.validate().unwrap_err();
        assert_eq!(
            err,
            ConfigError::InvalidValue {
                field: "egress_cluster_cidr_denylist",
                reason: "EGRESS_CLUSTER_CIDR_DENYLIST must be set (non-empty) when \
                     DEPLOYMENT_TIER=\"beta\" -- only alpha/local tolerate an empty \
                     cluster CIDR denylist"
                    .to_string(),
            }
        );
    }

    #[test]
    fn empty_cluster_cidr_denylist_is_tolerated_in_alpha_and_local() {
        for tier in ["alpha", "local"] {
            let cli = CliConfig::parse_from(["svc-action", "--deployment-tier", tier]);
            cli.validate()
                .unwrap_or_else(|e| panic!("tier {tier:?} should tolerate an empty denylist: {e}"));
        }
    }

    #[test]
    fn cluster_cidr_denylist_is_required_and_parsed_outside_alpha_local() {
        let cli = CliConfig::parse_from([
            "svc-action",
            "--deployment-tier",
            "production",
            "--egress-cluster-cidr-denylist",
            "10.42.0.0/16, 10.43.0.0/16 ,192.168.0.0/16",
        ]);
        cli.validate().expect("a populated denylist passes");
        let denylist = cli.cluster_cidr_denylist().unwrap();
        assert!(!denylist.is_empty());
    }

    #[test]
    fn malformed_cluster_cidr_denylist_entry_is_rejected() {
        let cli = CliConfig::parse_from([
            "svc-action",
            "--deployment-tier",
            "alpha",
            "--egress-cluster-cidr-denylist",
            "not-a-cidr",
        ]);
        let err = cli.cluster_cidr_denylist().unwrap_err();
        assert!(matches!(
            err,
            ConfigError::InvalidValue {
                field: "egress_cluster_cidr_denylist",
                ..
            }
        ));
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
    fn discord_bot_token_is_none_when_unset() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert!(cfg.discord_bot_token.is_none());
        clear_secret_env();
    }

    #[test]
    fn discord_bot_token_loaded_from_env_when_present() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
            std::env::set_var("DISCORD_BOT_TOKEN", "test-discord-bot-token");
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert_eq!(
            cfg.discord_bot_token.as_ref().map(Secret::expose),
            Some("test-discord-bot-token")
        );
        clear_secret_env();
    }

    /// Security review fix: Helm always renders `DB_READER_PASSWORD` (empty
    /// by default until the RO role is provisioned, `templates/
    /// secrets.yaml`) -- an empty value must load as `None`, the same as
    /// truly unset, so `try_start_db_bundle_loader`'s documented
    /// "DB_READER_PASSWORD unset -> loader disabled" branch actually fires
    /// for Helm's real rendered output, not just for a genuinely-absent
    /// env var.
    #[test]
    fn db_reader_password_empty_string_loads_as_none() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
            std::env::set_var("DB_READER_PASSWORD", "");
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert!(
            cfg.db_reader_password.is_none(),
            "an empty DB_READER_PASSWORD must load as None, not Some(\"\")"
        );
        clear_secret_env();
    }

    #[test]
    fn db_reader_password_nonempty_string_loads_as_some() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_secret_env();
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
            std::env::set_var("DB_READER_PASSWORD", "real-ro-password");
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        assert_eq!(
            cfg.db_reader_password.as_ref().map(Secret::expose),
            Some("real-ro-password")
        );
        clear_secret_env();
    }

    /// Removal regression (dataplane scale design, multi-tenant): the
    /// retired `BUNDLE_SCOPE_TENANT_ID`/`--bundle-scope-tenant-id` flag must
    /// no longer be a recognized CLI arg -- proves it was actually removed
    /// from `CliConfig`, not merely stopped being read.
    #[test]
    fn bundle_scope_tenant_id_flag_no_longer_exists() {
        let result = CliConfig::try_parse_from(["svc-action", "--bundle-scope-tenant-id", "42"]);
        assert!(
            result.is_err(),
            "BUNDLE_SCOPE_TENANT_ID must be fully removed, not just unused"
        );
    }

    #[test]
    fn bundle_config_full_reconcile_minutes_defaults_to_fifteen() {
        let cli = CliConfig::parse_from(["svc-action"]);
        assert_eq!(cli.bundle_config_full_reconcile_minutes, 15);
        assert_eq!(
            cli.full_reconcile_interval(),
            std::time::Duration::from_secs(15 * 60)
        );
    }

    #[test]
    fn full_reconcile_interval_is_clamped_to_a_one_minute_floor() {
        let cli =
            CliConfig::parse_from(["svc-action", "--bundle-config-full-reconcile-minutes", "0"]);
        assert_eq!(
            cli.full_reconcile_interval(),
            std::time::Duration::from_secs(60)
        );
    }

    #[test]
    fn bundle_config_poll_interval_is_clamped_to_a_five_second_floor() {
        let cli = CliConfig::parse_from(["svc-action", "--bundle-config-poll-seconds", "0"]);
        assert_eq!(
            cli.bundle_config_poll_interval(),
            std::time::Duration::from_secs(5)
        );
    }

    #[test]
    fn heartbeat_interval_reflects_the_configured_value() {
        let cli = CliConfig::parse_from(["svc-action", "--heartbeat-interval-ms", "250"]);
        assert_eq!(
            cli.heartbeat_interval(),
            std::time::Duration::from_millis(250)
        );
    }

    /// `validate()` already rejects `0` (`heartbeat_interval_ms_zero_is_
    /// rejected` above), but `heartbeat_interval()` is also called by a
    /// couple of test helpers before `validate()` runs -- floors at 1ms
    /// rather than panicking on a zero-length sleep/timeout.
    #[test]
    fn heartbeat_interval_floors_at_one_millisecond_pre_validation() {
        let cli = CliConfig::parse_from(["svc-action", "--heartbeat-interval-ms", "0"]);
        assert_eq!(
            cli.heartbeat_interval(),
            std::time::Duration::from_millis(1)
        );
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
            std::env::set_var("DISCORD_BOT_TOKEN", "super-secret-discord-bot-token");
        }
        let cli = CliConfig::parse_from(["svc-action"]);
        let cfg = Config::from_cli(cli).expect("secrets are set");
        let rendered = format!("{cfg:?}");
        assert!(!rendered.contains("super-secret-db-pass"));
        assert!(!rendered.contains("super-secret-discord-bot-token"));
        clear_secret_env();
    }
}
