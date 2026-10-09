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
    /// Host-API heartbeat interval: how often the stage sends `ping` to
    /// each connected executor session (fix/executor-link-heartbeat, alpha
    /// 2026-10-02 incident: a rolled svc pod left the executor bound to a
    /// terminated peer with no liveness signal at all). A session is
    /// dropped after `HEARTBEAT_MISSED_LIMIT` consecutive missed `pong`s.
    #[arg(long, env = "HEARTBEAT_INTERVAL_MS", default_value_t = 5000)]
    pub heartbeat_interval_ms: u64,
    /// Threshold for `crate::host_api::run_zero_executor_watchdog`'s
    /// periodic ERROR log: how long zero live executor sessions must
    /// persist before the watchdog starts logging loudly on its fixed
    /// cadence. Deliberately NOT wired into `/healthz`/`/health` -- see
    /// `rules/critical-rules.md` Observability and the regression noted on
    /// those handlers: readiness gated on executor connection deadlocked
    /// rollouts (alpha 2026-10-02).
    #[arg(long, env = "EXECUTOR_GRACE_SECONDS", default_value_t = 60)]
    pub executor_grace_seconds: u64,

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

    // -- Multi-tenant, change-log-driven active-bundle loader (dataplane
    // scale design rev 4, §7/§8 step 2) -- ENABLED by default; opt out via
    // the `waddles.core.disable-multi-tenant-watermark` kill-switch
    // (`crate::license::MultiTenantWatermarkGate`), combined with the
    // existing `waddles.core.disable-db-bundle-config` kill-switch
    // (`crate::license::DbBundleConfigGate`). When either kill-switch is
    // ON, or the license server is unreachable and either stays on its
    // last-known-OFF value, `PROCESS_APP_ID`/`PROCESS_BUNDLE_*` above
    // remain the sole selection mechanism. See `crate::changelog_consumer`.
    //
    // **`BUNDLE_SCOPE_TENANT_ID`/`BUNDLE_SCOPE_COMMUNITY_ID` REMOVED**
    // (dataplane scale design, user requirement: "every svc_process/
    // svc_action pod serves ALL tenants") -- this loader now discovers and
    // serves every `(tenant_id, community_id)` scope in the database
    // itself (`bundle_active_set::read_active_set_all`), never a single
    // operator-configured scope. There is no replacement env var; a pod no
    // longer has a "which tenant am I" concept for this loader at all.
    /// Reader-endpoint Postgres host for the DB-driven loader -- separate
    /// from `DB_HOST` (the primary, read-write connection above) so a read
    /// replica can be introduced later without touching the primary's own
    /// config; defaults to the primary host in alpha (no replica yet).
    #[arg(long, env = "DB_READER_HOST", default_value = "localhost")]
    pub db_reader_host: String,
    #[arg(long, env = "DB_READER_PORT", default_value_t = 5432)]
    pub db_reader_port: u16,
    #[arg(long, env = "DB_READER_NAME", default_value = "waddlebot")]
    pub db_reader_name: String,
    /// A distinct, SELECT-only Postgres role -- never the primary `DB_USER`
    /// account. See `bundle_active_set`'s crate-root doc for the exact
    /// grants this role needs.
    #[arg(long, env = "DB_READER_USER", default_value = "svc_process_ro")]
    pub db_reader_user: String,

    /// Production wiring for the bundle `db` host capability
    /// (`crate::capabilities::DbWiring`, `bundle_host_db::connect`) --
    /// connects as the least-privilege, read-write `waddles_bundle_runtime`
    /// role (`alembic/versions/0030_bundle_app_schemas.py`), distinct from
    /// both `DB_USER` (primary) and `DB_READER_USER` (read-only loader)
    /// above. Defaults match the same shared `waddles` Postgres instance
    /// those connect to -- a deployment with a dedicated bundle-db host/
    /// port/name overrides these independently.
    #[arg(long, env = "BUNDLE_DB_HOST", default_value = "localhost")]
    pub bundle_db_host: String,
    #[arg(long, env = "BUNDLE_DB_PORT", default_value_t = 5432)]
    pub bundle_db_port: u16,
    #[arg(long, env = "BUNDLE_DB_NAME", default_value = "waddlebot")]
    pub bundle_db_name: String,
    /// The role `0030_bundle_app_schemas.py` provisions -- never the
    /// primary `DB_USER` account (least privilege: DML-only on
    /// `app_core`/`app_community`, no DDL, no access to any other schema).
    #[arg(long, env = "BUNDLE_DB_USER", default_value = "waddles_bundle_runtime")]
    pub bundle_db_user: String,

    /// Production wiring for the bundle `reputation` host capability
    /// (issue #726, `bundle_host_reputation::connect`) -- the least-privilege
    /// `waddles_bundle_reputation` role (`alembic/versions/
    /// 0043_bundle_reputation_store.py`): DML on the two reputation tables +
    /// column-scoped membership SELECT, nothing else. Distinct from both
    /// `BUNDLE_DB_*` (app_core/app_community DML) and `DB_READER_*`
    /// (read-only loader).
    #[arg(long, env = "BUNDLE_REPUTATION_HOST", default_value = "localhost")]
    pub bundle_reputation_host: String,
    #[arg(long, env = "BUNDLE_REPUTATION_PORT", default_value_t = 5432)]
    pub bundle_reputation_port: u16,
    #[arg(long, env = "BUNDLE_REPUTATION_NAME", default_value = "waddlebot")]
    pub bundle_reputation_name: String,
    #[arg(
        long,
        env = "BUNDLE_REPUTATION_USER",
        default_value = "waddles_bundle_reputation"
    )]
    pub bundle_reputation_user: String,
    /// Refresh cadence, in whole seconds, of the reputation membership
    /// snapshot (`bundle_capability_gate::SnapshotMembership`). The snapshot
    /// is a read pre-filter only -- writes re-check live membership in their
    /// own transaction -- so this bounds how long a departed member stays
    /// readable, never write authority. Clamped to a 5s floor by
    /// [`CliConfig::bundle_reputation_membership_refresh`].
    #[arg(
        long,
        env = "BUNDLE_REPUTATION_MEMBERSHIP_REFRESH_S",
        default_value_t = 30
    )]
    pub bundle_reputation_membership_refresh_s: u64,

    /// Production wiring for the bundle `economy` host capability (issue
    /// #714, `bundle_host_economy::connect`) -- the least-privilege
    /// `waddles_economy_runtime` role (`alembic/versions/
    /// 0044_bundle_economy_store.py`): DML on `economy_balances` + append-only
    /// `economy_ledger` + column-scoped membership SELECT, nothing else.
    /// Distinct from `BUNDLE_DB_*`, `BUNDLE_REPUTATION_*` and `DB_READER_*`.
    #[arg(long, env = "BUNDLE_ECONOMY_HOST", default_value = "localhost")]
    pub bundle_economy_host: String,
    #[arg(long, env = "BUNDLE_ECONOMY_PORT", default_value_t = 5432)]
    pub bundle_economy_port: u16,
    #[arg(long, env = "BUNDLE_ECONOMY_NAME", default_value = "waddlebot")]
    pub bundle_economy_name: String,
    #[arg(
        long,
        env = "BUNDLE_ECONOMY_USER",
        default_value = "waddles_economy_runtime"
    )]
    pub bundle_economy_user: String,
    /// Refresh cadence, in whole seconds, of the membership snapshot the
    /// economy wiring feeds (`bundle_capability_gate::SnapshotMembership`; a
    /// read pre-filter only -- writes re-check live membership inside the
    /// write itself). Clamped to a 5s floor by
    /// [`CliConfig::bundle_economy_membership_refresh`].
    #[arg(
        long,
        env = "BUNDLE_ECONOMY_MEMBERSHIP_REFRESH_S",
        default_value_t = 30
    )]
    pub bundle_economy_membership_refresh_s: u64,
    /// Poll interval, in whole seconds, for the change-log consumer's
    /// incremental tick (`bundle_active_set::read_safe_seq`/`read_changes`)
    /// -- the full active-set re-read only runs for scopes the change-log
    /// actually names as affected, so this can stay coarse. Clamped to a
    /// 5s floor by [`CliConfig::bundle_config_poll_interval`] so a
    /// misconfigured `0`/negative value can never hot-loop against the
    /// reader database. Semantics unchanged from the retired single-tenant
    /// watermark loader this replaces -- same env var, same floor.
    #[arg(long, env = "BUNDLE_CONFIG_POLL_SECONDS", default_value_t = 300)]
    pub bundle_config_poll_seconds: i64,
    /// Periodic full-reconcile interval, in whole minutes (dataplane scale
    /// design §7: "bounds the blast radius of any change-log defect to one
    /// interval, independent of the change-log's own correctness"), default
    /// `15` per the design's own `FULL_RECONCILE_INTERVAL`. Clamped to a 1m
    /// floor by [`CliConfig::full_reconcile_interval`].
    #[arg(
        long,
        env = "BUNDLE_CONFIG_FULL_RECONCILE_MINUTES",
        default_value_t = 15
    )]
    pub bundle_config_full_reconcile_minutes: i64,

    // regression: #425 dropped cluster denylist
    /// Interim, config-sourced instance-wide private-IP egress policy
    /// (`bundle_host_http::egress::InstanceEgressPolicy`, Justin's
    /// decision: "private-ip is also subject to the INSTANCE policy --
    /// default deny, global-admin opt-in"). PR #428/#432's capability-gate
    /// grant snapshot is the eventual live source this field is a stopgap
    /// for. Field-for-field mirror of `core/svc_action::config::CliConfig`'s
    /// identical addition (security review finding, PR #468, MEDIUM).
    #[arg(
        long,
        env = "INSTANCE_EGRESS_ALLOW_PRIVATE_IP",
        default_value_t = false
    )]
    pub instance_egress_allow_private_ip: bool,
    /// Comma-separated CIDR blocks (IPv4/IPv6, `bundle_host_http::egress::
    /// ClusterCidrDenylist`) covering this deployment's own pod, Service,
    /// and node ranges -- never liftable by any egress grant or by
    /// `instance_egress_allow_private_ip`. Empty (the default) is tolerated
    /// only when [`Self::deployment_tier`] is `alpha`/`local` --
    /// [`CliConfig::validate`] hard-fails startup otherwise.
    #[arg(long, env = "EGRESS_CLUSTER_CIDR_DENYLIST", default_value = "")]
    pub egress_cluster_cidr_denylist: String,
    /// Deployment tier -- already set chart-wide via the shared ConfigMap's
    /// `DEPLOYMENT_TIER` key. Defaults to `"alpha"` here (CLI/test default,
    /// dev-permissive) -- every non-alpha/local Helm deployment sets
    /// `DEPLOYMENT_TIER` explicitly via the ConfigMap regardless.
    #[arg(long, env = "DEPLOYMENT_TIER", default_value = "alpha")]
    pub deployment_tier: String,

    /// hub-api's internal gRPC endpoint (`waddles.hub.internal.v1`,
    /// `core/hub_client::HubClient::connect`'s `endpoint`), e.g.
    /// `https://waddlebot-hub-api-v3:50204`. Empty (the default) means "not
    /// configured" -- [`crate::build_hub_client`] fails loud at startup
    /// rather than starting and silently dead-lettering every event when
    /// PII tokenization is enabled and this is unset (user requirement:
    /// "fail loud, not silent dead-letter").
    #[arg(long, env = "HUB_API_GRPC_ENDPOINT", default_value = "")]
    pub hub_api_grpc_endpoint: String,
    /// hub-api's machine-JWT bootstrap endpoint
    /// (`hub_api/blueprints/service_jwt_bp.py`'s `POST /internal/
    /// service-token`, `service_auth::MachineJwtClient`'s `token_endpoint`),
    /// e.g. `http://waddlebot-hub-api-v3:8204/internal/service-token`.
    /// Empty (the default) means "not configured" -- same fail-loud
    /// contract as [`Self::hub_api_grpc_endpoint`].
    #[arg(long, env = "SERVICE_JWT_TOKEN_ENDPOINT", default_value = "")]
    pub service_jwt_token_endpoint: String,
    /// Path to this pod's projected Kubernetes ServiceAccount token, read
    /// by [`service_auth::MachineJwtClient`] to bootstrap a machine JWT --
    /// same default every other machine-JWT bootstrap in this repo uses
    /// (`libs/flask_core/flask_core/service_jwt.py`).
    #[arg(
        long,
        env = "SERVICE_JWT_SA_TOKEN_PATH",
        default_value = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )]
    pub service_jwt_sa_token_path: String,
    /// Path to the PEM CA bundle that signed hub-api's internal gRPC server
    /// cert (`k8s/helm/waddlebot/templates/hub-api-grpc-tls-secret.yaml`'s
    /// `ca.crt`, mounted by `templates/svc-process-rust.yaml`), passed as
    /// `core/hub_client::HubClient::connect`'s `ca_cert_path`. Empty (the
    /// default) falls back to `connect`'s system/webpki trust store --
    /// never correct against this chart's self-signed internal CA, but
    /// kept as the permissive default for tests/local runs that dial a
    /// publicly-rooted endpoint instead.
    #[arg(long, env = "HUB_API_GRPC_CA_FILE", default_value = "")]
    pub hub_api_grpc_ca_file: String,

    /// Plain env/values off-switch for inbound PII tokenization
    /// (`crate::build_hub_client`'s gate), independent of the
    /// `waddles.core.disable-pii-tokenization` PostHog kill-switch --
    /// `rules/critical-rules.md` Feature Flags & License Tiers' opt-out
    /// kill-switch principle ("keeps unseen-flags-OFF without stranding
    /// air-gapped deploys"). PostHog alone can't be toggled in an
    /// environment with no in-cluster PostHog (e.g. alpha), which would
    /// otherwise strand that deployment behind the fail-loud gate whenever
    /// hub-api's internal gRPC isn't reachable yet. `None` (unset, the
    /// default) leaves the existing PostHog-gated, default-ENABLED,
    /// fail-loud-when-unreachable behavior completely unchanged --
    /// `Some(false)` is the only value this crate's startup gate treats
    /// specially (see [`crate::run_with_shutdown`]'s call site): tokenization
    /// runs disabled, `hub_client` is never connected, and startup never
    /// fails loud. This is an explicit, loudly-logged operator escape hatch
    /// for dev/air-gapped deployments -- never a silent bypass, and never the
    /// default in a production tenant.
    #[arg(long, env = "PII_TOKENIZATION_ENABLED")]
    pub pii_tokenization_enabled_override: Option<bool>,
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
    /// type. Fails closed on a malformed entry.
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
    /// InstanceEgressPolicy`] snapshot.
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

    /// [`Self::bundle_reputation_membership_refresh_s`] clamped to a 5s floor
    /// so a misconfigured `0` can never hot-loop the membership query.
    pub fn bundle_reputation_membership_refresh(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.bundle_reputation_membership_refresh_s.max(5))
    }

    /// [`Self::bundle_economy_membership_refresh_s`] clamped to a 5s floor so
    /// a misconfigured `0` can never hot-loop the membership query.
    pub fn bundle_economy_membership_refresh(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.bundle_economy_membership_refresh_s.max(5))
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
    /// `validate` already rejects `0`, but this floors at 1ms anyway rather
    /// than ever constructing a zero-length sleep/timeout.
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
    pub cache_password: Option<Secret>,
    pub service_api_key: Secret,
    /// Raw `ENVELOPE_BINDING_KEYS` value (spec SS5.11): `kid:hexkey[,kid:hexkey...]`
    /// -- the symmetric HMAC key material `penguin_spine::KeyRing` parses
    /// (`crate::hop`). `None` when unset; the process loop refuses to start
    /// hop verification without it rather than silently accepting every
    /// envelope (a missing keyring must never fail open).
    pub envelope_binding_keys: Option<Secret>,
    /// `DB_READER_PASSWORD` for the DB-driven active-bundle loader's
    /// read-only Postgres role (`crate::bundle_loader`). Deliberately
    /// `Option`, unlike `db_password`/`service_api_key`: this loader is
    /// enabled by default (opt out via `waddles.core.disable-db-bundle-config`),
    /// so a fresh alpha deployment that hasn't provisioned the RO role yet must not
    /// fail startup over it -- `crate::bundle_loader::try_start` logs a
    /// warning and stays disabled (falling back to the existing
    /// `PROCESS_APP_ID`/`PROCESS_BUNDLE_*` env selection) when this is
    /// unset, the same graceful-degradation contract as
    /// `envelope_binding_keys` above.
    pub db_reader_password: Option<Secret>,
    /// `BUNDLE_DB_PASSWORD` for the bundle `db` host capability's
    /// `waddles_bundle_runtime` connection (`crate::capabilities::DbWiring`).
    /// `Option`, same rationale as `db_reader_password`: `BUNDLE_DB_CAPABILITY_FLAG`
    /// defaults OFF, so a fresh deployment that hasn't provisioned the role
    /// yet must not fail startup over it -- `crate::lib::try_build_db_wiring`
    /// logs and leaves the capability unwired (every `db` call then denies
    /// `not_implemented`) when this is unset. Once set, a connection
    /// *failure* is a different, louder case -- see that function's doc.
    pub bundle_db_password: Option<Secret>,
    /// `BUNDLE_REPUTATION_PASSWORD` for the `reputation` host capability's
    /// `waddles_bundle_reputation` connection
    /// (`crate::lib::try_build_reputation_wiring`). `Option`, same rationale
    /// as `bundle_db_password`: the capability's flag defaults OFF, so an
    /// unprovisioned deployment must start; the capability then denies
    /// `not_implemented`. An empty value is treated as unset.
    pub bundle_reputation_password: Option<Secret>,
    /// `BUNDLE_ECONOMY_PASSWORD` for the `economy` host capability's
    /// `waddles_economy_runtime` connection
    /// (`crate::lib::try_build_economy_wiring`). `Option`, same rationale as
    /// `bundle_reputation_password`: the capability's flag defaults OFF, so an
    /// unprovisioned deployment must start; the capability then denies
    /// `not_implemented`. An empty value is treated as unset.
    pub bundle_economy_password: Option<Secret>,
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
                "db_reader_password",
                &self.db_reader_password.as_ref().map(|_| Secret::new("")),
            )
            .field(
                "bundle_db_password",
                &self.bundle_db_password.as_ref().map(|_| Secret::new("")),
            )
            .field(
                "bundle_reputation_password",
                &self
                    .bundle_reputation_password
                    .as_ref()
                    .map(|_| Secret::new("")),
            )
            .field(
                "bundle_economy_password",
                &self
                    .bundle_economy_password
                    .as_ref()
                    .map(|_| Secret::new("")),
            )
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
        // Same "Helm always renders the secret key, empty until
        // provisioned" treatment as `db_reader_password` above.
        let bundle_db_password = std::env::var("BUNDLE_DB_PASSWORD")
            .ok()
            .filter(|s| !s.is_empty())
            .map(Secret::new);
        let bundle_reputation_password = std::env::var("BUNDLE_REPUTATION_PASSWORD")
            .ok()
            .filter(|s| !s.is_empty())
            .map(Secret::new);
        let bundle_economy_password = std::env::var("BUNDLE_ECONOMY_PASSWORD")
            .ok()
            .filter(|s| !s.is_empty())
            .map(Secret::new);
        Ok(Self {
            cli,
            db_password,
            cache_password,
            service_api_key,
            envelope_binding_keys,
            db_reader_password,
            bundle_db_password,
            bundle_reputation_password,
            bundle_economy_password,
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
            "DB_READER_PASSWORD",
            "BUNDLE_DB_PASSWORD",
            "BUNDLE_REPUTATION_PASSWORD",
            "BUNDLE_ECONOMY_PASSWORD",
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

    /// Security review fix (PR #468, MEDIUM): an empty cluster CIDR
    /// denylist is a hard startup error outside alpha/local -- the default
    /// `deployment_tier` in tests/CLI defaults is `"alpha"`, so this must be
    /// set explicitly to prove the gate actually fires.
    #[test]
    fn empty_cluster_cidr_denylist_is_rejected_outside_alpha_local() {
        let cli = CliConfig::parse_from(["svc-process", "--deployment-tier", "beta"]);
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
            let cli = CliConfig::parse_from(["svc-process", "--deployment-tier", tier]);
            cli.validate()
                .unwrap_or_else(|e| panic!("tier {tier:?} should tolerate an empty denylist: {e}"));
        }
    }

    #[test]
    fn cluster_cidr_denylist_is_required_and_parsed_outside_alpha_local() {
        let cli = CliConfig::parse_from([
            "svc-process",
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
            "svc-process",
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
    fn instance_egress_allow_private_ip_flag_override_is_honored() {
        let cli = CliConfig::parse_from(["svc-process", "--instance-egress-allow-private-ip"]);
        assert!(cli.instance_egress_allow_private_ip);
        assert!(cli.instance_egress_policy().allow_private_ip_egress);
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
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
            std::env::set_var("SERVICE_API_KEY", "test-api-key");
            std::env::set_var("DB_READER_PASSWORD", "");
        }
        let cli = CliConfig::parse_from(["svc-process"]);
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
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("DB_PASSWORD", "test-db-pass");
            std::env::set_var("SERVICE_API_KEY", "test-api-key");
            std::env::set_var("DB_READER_PASSWORD", "real-ro-password");
        }
        let cli = CliConfig::parse_from(["svc-process"]);
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
        let result = CliConfig::try_parse_from(["svc-process", "--bundle-scope-tenant-id", "42"]);
        assert!(
            result.is_err(),
            "BUNDLE_SCOPE_TENANT_ID must be fully removed, not just unused"
        );
    }

    #[test]
    fn bundle_config_full_reconcile_minutes_defaults_to_fifteen() {
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.bundle_config_full_reconcile_minutes, 15);
        assert_eq!(
            cli.full_reconcile_interval(),
            std::time::Duration::from_secs(15 * 60)
        );
    }

    #[test]
    fn full_reconcile_interval_is_clamped_to_a_one_minute_floor() {
        let cli =
            CliConfig::parse_from(["svc-process", "--bundle-config-full-reconcile-minutes", "0"]);
        assert_eq!(
            cli.full_reconcile_interval(),
            std::time::Duration::from_secs(60)
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
