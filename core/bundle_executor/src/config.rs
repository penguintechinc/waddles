//! Runtime configuration, loaded from environment variables per spec
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS12.7.
//! Every secret-bearing value (the mTLS client key) is a file path, never
//! a CLI flag or an inline value -- `rules/critical-rules.md` Token &
//! Secret Hygiene.

use std::path::PathBuf;

use clap::Parser;

use crate::error::ExecutorError;

/// One stage host-API listener this executor serves: `label` names the stage
/// in metrics/logs, `addr` is the `host:port` it dials.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StageTarget {
    pub label: String,
    pub addr: String,
}

/// CLI/env configuration surface. `clap`'s `env` feature reads the SS12.7
/// variable of the same name when the flag is not passed explicitly --
/// this binary is started by its Deployment with env vars only, never
/// flags, but `clap::Parser` doubles as a typed, testable env-var reader
/// (same pattern as `core/svc_process::config::CliConfig`).
#[derive(Debug, Parser, Clone)]
#[command(name = "bundle-executor")]
pub struct CliConfig {
    /// The stage host-API listener(s) this executor dials, e.g.
    /// `svc-process:8301` or `svc-action:8302` (spec SS6.6/SS7.1). A
    /// comma-separated list (`svc-process:8301,svc-action:8302`) serves every
    /// listed stage from this one process: each gets its own dial loop,
    /// reconnect/backoff, bundle registry and metrics `stage` label, so one
    /// stage failing never blocks another. All stages share the
    /// `HOST_API_*` mTLS material and `HOST_API_STAGE_IDENTITY`.
    #[arg(long, env = "STAGE_HOST_API_ADDR")]
    pub stage_host_api_addr: String,

    /// Connections held open to the stage, invocations spread across them
    /// (spec SS4.5).
    #[arg(long, env = "EXECUTOR_STAGE_CONNECTIONS", default_value_t = 4)]
    pub executor_stage_connections: u32,

    /// Default per-call wall-clock budget; a `load`'s `limits.timeout_ms`
    /// may override up to `executor_max_call_timeout_ms` (spec SS7.3).
    #[arg(long, env = "EXECUTOR_CALL_TIMEOUT_MS", default_value_t = 2000)]
    pub executor_call_timeout_ms: u64,

    /// Hard ceiling no per-bundle override may exceed (spec SS7.3).
    #[arg(long, env = "EXECUTOR_MAX_CALL_TIMEOUT_MS", default_value_t = 10_000)]
    pub executor_max_call_timeout_ms: u64,

    /// Default linear-memory cap per instance, in MiB (spec SS7.3).
    #[arg(long, env = "EXECUTOR_MEMORY_LIMIT_MB", default_value_t = 64)]
    pub executor_memory_limit_mb: u32,

    /// Hard ceiling no per-bundle override may exceed (spec SS7.3).
    #[arg(long, env = "EXECUTOR_MAX_MEMORY_LIMIT_MB", default_value_t = 256)]
    pub executor_max_memory_limit_mb: u32,

    /// Max concurrent in-flight instances (invocations) PER loaded bundle
    /// digest (spec SS7.2) -- enforced by a per-bundle semaphore in
    /// `crate::invoke::Executor`. Each call still gets a FRESH `Store` (no
    /// guest state is shared across invokes, so a digest shared by several
    /// tenants can never leak data between them); what is reused is the
    /// pre-linked `InstancePre`, so per-call work is instantiation only.
    #[arg(long, env = "EXECUTOR_INSTANCES_PER_BUNDLE", default_value_t = 4)]
    pub executor_instances_per_bundle: u32,

    /// Wasmtime fuel budget armed on every `process-stage.transform` call
    /// (connector spec docs/superpowers/specs/2026-09-28-connector-bundles.md
    /// SS0 condition 4: "the existing epoch deadline (PR #406) plus fuel per
    /// `on-frame` invocation"). `transform` is this executor's hot,
    /// per-event path -- the `stage` world's analogue of the connector
    /// world's not-yet-implemented `receiver.on-frame` -- so it gets its own
    /// budget, distinct from `dispatch`'s. A guest instruction executes
    /// roughly one unit of fuel per operation (wasmtime's own accounting,
    /// not wall-clock), so this is a CPU-work bound complementing (never
    /// replacing) the epoch wall-clock deadline: a tight loop that never
    /// yields can burn this budget well before an epoch tick fires.
    #[arg(
        long,
        env = "EXECUTOR_FUEL_LIMIT_TRANSFORM",
        default_value_t = 500_000_000
    )]
    pub executor_fuel_limit_transform: u64,

    /// Same as [`Self::executor_fuel_limit_transform`], armed instead for
    /// `action-stage.dispatch` calls -- kept as its own knob since a sender
    /// bundle's outbound-shaping work has a different realistic cost profile
    /// than a receiver-side transform.
    #[arg(
        long,
        env = "EXECUTOR_FUEL_LIMIT_DISPATCH",
        default_value_t = 500_000_000
    )]
    pub executor_fuel_limit_dispatch: u64,

    /// Wasmtime fuel budget armed on every connector-world export call
    /// (`receiver.on-connect`/`on-frame`/`on-heartbeat-due`/`on-disconnect`,
    /// connector spec SS3.5 "`on-frame` fuel (Gemini condition 4)"). An
    /// instruction-count bound independent of, and in addition to, the epoch
    /// deadline; whichever trips first aborts the call into a typed
    /// `crate::connector::ConnectorCallError`. Per-platform tuning is
    /// expected; this is the global default.
    #[arg(
        long,
        env = "EXECUTOR_CONNECTOR_FUEL_LIMIT",
        default_value_t = 50_000_000
    )]
    pub executor_connector_fuel_limit: u64,

    /// Concurrent in-flight calls per always-pinned connector bundle (connector
    /// spec SS2.3: "sized to the replica's executor worker-thread count").
    /// A call beyond this waits `EXECUTOR_POOL_WAIT_MS`, then fails
    /// `pool_exhausted` rather than queueing unboundedly.
    #[arg(long, env = "EXECUTOR_CONNECTOR_POOL_SIZE", default_value_t = 8)]
    pub executor_connector_pool_size: u32,

    /// Global ceiling on in-flight calls across all loaded bundles (spec
    /// SS7.2) -- enforced by a process-wide semaphore; also the size of the
    /// slice of the tokio blocking pool guest execution may occupy.
    #[arg(long, env = "EXECUTOR_MAX_CONCURRENT_CALLS", default_value_t = 32)]
    pub executor_max_concurrent_calls: u32,

    /// How long a call waits for a free per-bundle instance slot AND a
    /// global call slot before failing `pool_exhausted` (spec SS7.2),
    /// reported as an `EXECUTOR_DEADLINE` error with detail
    /// `pool_exhausted`.
    #[arg(long, env = "EXECUTOR_POOL_WAIT_MS", default_value_t = 500)]
    pub executor_pool_wait_ms: u64,

    /// Directory precompiled `.cwasm` artifacts are cached under, keyed by
    /// `{digest}-{wasmtime_abi}-{collector}` (spec SS7.2/SS7.6).
    #[arg(
        long,
        env = "EXECUTOR_PRECOMPILE_DIR",
        default_value = "/var/cache/waddles/wasm"
    )]
    pub executor_precompile_dir: PathBuf,

    /// GC collector the runtime engine is configured with; reported in the
    /// `hello` frame's `collector` field and must match the collector any
    /// precompiled artifact was produced with (spec SS7.2). `drc`
    /// (deferred reference counting) is the only value this crate wires
    /// today -- see `crate::engine::build_engine`.
    #[arg(long, env = "EXECUTOR_WASM_COLLECTOR", default_value = "drc")]
    pub executor_wasm_collector: String,

    /// Whether this pod expects to be running under the gVisor
    /// `RuntimeClass` (spec SS12.2). A mismatch between this and what the
    /// stage expects refuses the connection with `UNSANDBOXED_EXECUTOR`.
    #[arg(long, env = "WADDLES_SANDBOX_GVISOR", default_value_t = true)]
    pub sandbox_gvisor: bool,

    /// PEM client certificate for the mTLS connection to the stage's
    /// host-API port (spec SS6.6).
    #[arg(long, env = "HOST_API_CLIENT_CERT_FILE")]
    pub host_api_client_cert_file: Option<PathBuf>,

    /// PEM client private key, file-only (never inline) per Token & Secret
    /// Hygiene.
    #[arg(long, env = "HOST_API_CLIENT_KEY_FILE")]
    pub host_api_client_key_file: Option<PathBuf>,

    /// PEM CA bundle used to verify the stage's server certificate.
    #[arg(long, env = "HOST_API_CA_FILE")]
    pub host_api_ca_file: Option<PathBuf>,

    /// The stage's expected peer identity (gh security review CRITICAL
    /// finding on PR #406, item 1) -- a SPIFFE URI SAN (e.g.
    /// `spiffe://penguintech.io/beta/svc-process`) or, if none is
    /// configured on the stage's certificate yet, its DNS SAN/CN. Standard
    /// TLS chain+hostname verification alone (`crate::tls::
    /// build_client_config`'s base behavior) proves "issued by our CA for
    /// this DNS name"; this additionally pins WHICH exact service identity
    /// this executor will accept `load`/`unload`/`invoke` commands from --
    /// spec SS6.6's "otherwise pinned by configuration" requirement.
    /// `validate()` requires this in production; `crate::tls::
    /// build_client_config` falls back to base verification only (no
    /// identity pinning) when unset, which every existing test that
    /// doesn't exercise pinning relies on.
    #[arg(long, env = "HOST_API_STAGE_IDENTITY")]
    pub host_api_stage_identity: Option<String>,

    /// S3-compatible bucket endpoint bundle components are fetched from,
    /// e.g. `http://minio.waddles.svc.cluster.local:9000` (spec SS7.6/
    /// SS12.7). `Option` (rather than a required arg) so every existing
    /// test config that never touches the bucket keeps parsing; the real
    /// production path (`crate::bucket::BucketConfig::from_cli`, wired in
    /// `crate::run`) fails closed with a clear `Config` error if unset,
    /// same effect as a required arg without breaking every other test's
    /// `CliConfig` fixture.
    #[arg(long, env = "BUNDLE_BUCKET_ENDPOINT")]
    pub bundle_bucket_endpoint: Option<String>,

    /// Bucket name components are stored under (spec SS12.7 default
    /// `waddles-bundles`).
    #[arg(long, env = "BUNDLE_BUCKET_NAME")]
    pub bundle_bucket_name: Option<String>,

    /// AWS SigV4 region; MinIO accepts any value consistent between signer
    /// and server (spec SS12.7 default `us-east-1`).
    #[arg(long, env = "BUNDLE_BUCKET_REGION", default_value = "us-east-1")]
    pub bundle_bucket_region: String,

    /// SigV4 access key id -- env only per spec SS12.7 (the spec documents
    /// this pair as "required, env only", distinct from the mTLS material
    /// above which Token & Secret Hygiene requires to be file-based).
    #[arg(long, env = "BUNDLE_BUCKET_ACCESS_KEY_ID")]
    pub bundle_bucket_access_key_id: Option<String>,

    /// SigV4 secret access key -- env only, same as above. Never logged:
    /// `crate::bucket::SecretAccessKey`'s `Debug` impl redacts it before it
    /// ever reaches a `{:?}`/`tracing` field.
    #[arg(long, env = "BUNDLE_BUCKET_SECRET_ACCESS_KEY")]
    pub bundle_bucket_secret_access_key: Option<String>,

    /// PEM CA bundle verifying the bucket's server certificate when
    /// `BUNDLE_BUCKET_ENDPOINT` is `https://`. Required in that case --
    /// this binary never disables certificate verification (`rules/
    /// security.md`) -- and unused for the plain `http://` in-namespace
    /// MinIO endpoint spec SS12.7 documents as the default.
    #[arg(long, env = "BUNDLE_BUCKET_CA_FILE")]
    pub bundle_bucket_ca_file: Option<PathBuf>,

    /// Wall-clock budget for one bucket GET (spec SS12.7 default `30`).
    #[arg(long, env = "BUNDLE_FETCH_TIMEOUT_S", default_value_t = 30)]
    pub bundle_fetch_timeout_s: u64,

    /// Hard cap on a fetched component's byte size (spec SS12.7 default
    /// `33554432` = 32 MiB). A `Content-Length` above this -- or a body
    /// that would grow past it -- fails the fetch rather than buffering
    /// unbounded misconfiguration- or compromise-controlled data.
    #[arg(long, env = "BUNDLE_MAX_COMPONENT_BYTES", default_value_t = 33_554_432)]
    pub bundle_max_component_bytes: u64,

    /// Upper bound on a precompiled `.cwasm` artifact fetched from the bundle
    /// bucket (`precompiled/<engine_fp>/<digest>.cwasm`). Compiled code is
    /// several times larger than the source component, hence its own cap
    /// (default 256 MiB); an oversized object fails the AOT path and falls
    /// back (loudly) to a local compile.
    #[arg(long, env = "BUNDLE_MAX_PRECOMPILED_BYTES", default_value_t = 268_435_456)]
    pub bundle_max_precompiled_bytes: u64,

    /// Resident compiled-module budget for the whole process, in MiB, shared
    /// by every stage this executor serves. Above it, idle modules no
    /// in-flight invoke is using are evicted least-recently-used first and
    /// new loads only verify (compile/deserialize happens on first call).
    /// `0` = unlimited (unreferenced modules are then dropped immediately).
    #[arg(long, env = "EXECUTOR_MEMORY_BUDGET_MB", default_value_t = 8192)]
    pub executor_memory_budget_mb: u64,

    /// Kill switch `waddles.disable-aot-precompile` (opt-out, unseen = AOT on).
    /// This credential-less sandbox has no PostHog egress, so the flag is read
    /// from its Docker/Helm ENV baseline (`FLAG_` + key, `.`/`-` -> `_`), the
    /// same convention every stage uses. `true` = never load `.cwasm`
    /// artifacts; always JIT-compile.
    #[arg(
        long,
        env = "FLAG_WADDLES_DISABLE_AOT_PRECOMPILE",
        default_value_t = false,
        action = clap::ArgAction::Set
    )]
    pub flag_disable_aot_precompile: bool,

    /// Stable executor identity (StatefulSet pod name, e.g.
    /// `waddlebot-svc-bundles-executor-0`) sent to stages in the `hello` frame
    /// for slot ownership. Unset = `POD_NAME` (downward API).
    #[arg(long, env = "EXECUTOR_ID")]
    pub executor_id: Option<String>,

    /// StatefulSet (sharded) mode: refuse to start without a resolvable
    /// executor identity (see [`CliConfig::validate_executor_id`]).
    #[arg(
        long,
        env = "EXECUTOR_REQUIRE_ID",
        default_value_t = false,
        action = clap::ArgAction::Set
    )]
    pub executor_require_id: bool,

    /// JSON object mapping `key_id -> base64(32-byte Ed25519 public key)`
    /// (spec SS5.6/Gemini condition 9) -- the platform key(s)
    /// `crate::signing::verify_artifact_signature` checks a bundle's
    /// signed sidecar against, supporting rotation via multiple entries.
    /// `Option` (rather than required) for the same reason
    /// `bundle_bucket_endpoint` is: every existing test's `CliConfig`
    /// fixture keeps parsing without setting it, and `crate::invoke::
    /// Executor::new` treats an unset/blank value as "verification off"
    /// (`crate::signing::PlatformPublicKeys::from_cli`) -- but the real
    /// production path (`crate::lib::run`, via `PlatformPublicKeys::
    /// from_cli_required`) fails closed at startup if it's unset, so that
    /// "off" state is never reachable outside a test that never claimed
    /// to enforce signatures in the first place.
    #[arg(long, env = "BUNDLE_SIGNING_PUBLIC_KEYS")]
    pub bundle_signing_public_keys: Option<String>,

    /// Interval between `crate::heartbeat`'s liveness checks (and, when
    /// `EXECUTOR_SELF_PING_ENABLED` is set, this executor's own `ping`
    /// frames). A connection is declared stale after
    /// `heartbeat::STALE_INTERVAL_MULTIPLIER` silent intervals.
    #[arg(long, env = "EXECUTOR_HEARTBEAT_INTERVAL_SECS", default_value_t = 5)]
    pub executor_heartbeat_interval_secs: u64,

    /// Kill switch for the whole stale-session monitor (`crate::heartbeat`),
    /// in case it ever needs rolling back independently of a redeploy.
    /// Default on -- this is the fix for the silent-half-open-connection
    /// incident this PR exists to close.
    #[arg(long, env = "EXECUTOR_HEARTBEAT_ENABLED", default_value_t = true)]
    pub executor_heartbeat_enabled: bool,

    /// Whether this executor ALSO sends its own `ping` to the stage on the
    /// heartbeat interval, rather than only reacting to frames the stage
    /// sends. Defaults to **false**: today's svc-process/svc-action
    /// host-API read loop treats an unsolicited `ping` FROM the executor as
    /// a fatal/unexpected frame and closes the connection -- flipping this
    /// on before the companion fix (`fix/executor-link-heartbeat`, svc
    /// side) lands would kill every healthy connection on its first
    /// self-initiated heartbeat. Set to `true` once that PR is deployed.
    #[arg(long, env = "EXECUTOR_SELF_PING_ENABLED", default_value_t = false)]
    pub executor_self_ping_enabled: bool,

    /// Where `crate::probe` atomically writes a timestamp whenever the
    /// host-API session is healthy, and `--healthcheck=session` reads it
    /// back from (spec: this process has no HTTP surface, so Kubernetes'
    /// liveness/readiness probes exec this binary instead of hitting a
    /// port).
    #[arg(
        long,
        env = "EXECUTOR_PROBE_FILE",
        default_value = "/tmp/executor-live"
    )]
    pub executor_probe_file: PathBuf,

    /// `--healthcheck=session`'s default max age for the probe file (and
    /// the startup-grace window before a still-missing file is treated as
    /// a failure) when `--max-age` is not passed explicitly.
    #[arg(long, env = "EXECUTOR_GRACE_SECONDS", default_value_t = 60)]
    pub executor_grace_secs: u64,

    /// Ceiling on concurrent WASM JIT compiles (`Component::new`). Each
    /// compile runs on a tokio blocking-pool thread (never the async
    /// reactor) and holds one of these permits for its duration, so a burst
    /// of cold digests from many tenants queues instead of saturating every
    /// core. Compiles are CPU-heavy (~3-4s for a large component, spec
    /// SS7.2), hence the small default.
    /// `0` = auto: `min(4, available CPUs)`. Shared by app-bundle AND
    /// connector compiles so boot never JIT-compiles every bundle at once.
    #[arg(long, env = "EXECUTOR_MAX_CONCURRENT_COMPILES", default_value_t = 0)]
    pub executor_max_concurrent_compiles: u32,

    /// On SIGTERM/SIGINT: how long to wait for in-flight invokes/compiles to
    /// drain before exiting 0 anyway. Keep below the pod's
    /// `terminationGracePeriodSeconds` (Kubernetes default 30s) so a rollout
    /// never falls through to SIGKILL.
    #[arg(long, env = "EXECUTOR_SHUTDOWN_DRAIN_SECONDS", default_value_t = 25)]
    pub executor_shutdown_drain_secs: u64,

    /// The connector host-API listener (svc-ingest) this executor DIALS a
    /// SECOND link to, `host:port` with `host` a service DNS name (IP
    /// literals are refused at config load). Unset/empty = no connector
    /// link is dialed. The address comes from config ONLY -- nothing read off
    /// a wire frame ever selects a dial target (SSRF). `validate()` refuses a
    /// value without at least one `CONNECTOR_PIN_DIGEST_<P>`
    /// (connector runtime spec S2.1/S2.2).
    #[arg(long, env = "CONNECTOR_HOST_API_ADDR")]
    pub connector_host_api_addr: Option<String>,

    /// The connector listener's expected server identity (URI/DNS SAN, else
    /// CN), pinned through the same `crate::tls` path as
    /// `HOST_API_STAGE_IDENTITY`. Must differ from the stage identity so
    /// svc-action/svc-process can never stand in for svc-ingest.
    #[arg(long, env = "CONNECTOR_HOST_API_IDENTITY")]
    pub connector_host_api_identity: Option<String>,

    /// PEM client certificate presented on the connector link. A DISTINCT
    /// identity from `HOST_API_CLIENT_CERT_FILE`: the stage client cert's SANs
    /// name the stages, so a listener pinning the executor identity by SAN
    /// would otherwise accept a stage certificate. This one's SAN must be ONLY
    /// the executor identity.
    #[arg(long, env = "CONNECTOR_HOST_API_CLIENT_CERT_FILE")]
    pub connector_host_api_client_cert_file: Option<PathBuf>,

    /// PEM private key for [`Self::connector_host_api_client_cert_file`],
    /// file-only per Token & Secret Hygiene.
    #[arg(long, env = "CONNECTOR_HOST_API_CLIENT_KEY_FILE")]
    pub connector_host_api_client_key_file: Option<PathBuf>,

    /// Connections THIS replica holds to the connector listener. Fixed at 1: the
    /// executor runs exactly one connector dial loop. Scale out by replicas --
    /// svc-ingest keeps one session per replica and round-robins across them
    /// (validated in `validate()`).
    #[arg(long, env = "EXECUTOR_CONNECTOR_CONNECTIONS", default_value_t = 1)]
    pub executor_connector_connections: u32,

    /// `sha256:<64 lowercase hex>` of the Twitch connector to pin at startup
    /// (`connectors/twitch/<digest>.wasm`). Empty/unset = not pinned.
    #[arg(long, env = "CONNECTOR_PIN_DIGEST_TWITCH")]
    pub connector_pin_digest_twitch: Option<String>,

    /// Same as [`Self::connector_pin_digest_twitch`] for Discord.
    #[arg(long, env = "CONNECTOR_PIN_DIGEST_DISCORD")]
    pub connector_pin_digest_discord: Option<String>,

    /// Same as [`Self::connector_pin_digest_twitch`] for YouTube.
    #[arg(long, env = "CONNECTOR_PIN_DIGEST_YOUTUBE")]
    pub connector_pin_digest_youtube: Option<String>,

    /// Same as [`Self::connector_pin_digest_twitch`] for the inbound-only RSI Spectrum connector.
    #[arg(long, env = "CONNECTOR_PIN_DIGEST_SPECTRUM")]
    pub connector_pin_digest_spectrum: Option<String>,
}

impl CliConfig {
    /// Resolved compile-concurrency cap: the explicit value when >= 1,
    /// else `min(4, available CPUs)`. Kept small because each in-flight
    /// JIT compile holds hundreds of MiB of Cranelift state.
    #[must_use]
    pub fn effective_max_concurrent_compiles(&self) -> u32 {
        if self.executor_max_concurrent_compiles >= 1 {
            return self.executor_max_concurrent_compiles;
        }
        let cpus = std::thread::available_parallelism().map_or(1, std::num::NonZeroUsize::get);
        u32::try_from(cpus.min(4)).unwrap_or(4).max(1)
    }

    /// The executor identity reported in `hello`: `EXECUTOR_ID` when set and
    /// non-blank, else `POD_NAME` (the StatefulSet pod name via the downward
    /// API), else `None` (stages then fall back to broadcast for this
    /// executor, logging WARN).
    #[must_use]
    pub fn resolved_executor_id(&self) -> Option<String> {
        self.executor_id
            .clone()
            .or_else(|| std::env::var("POD_NAME").ok())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    }

    /// Fails startup loudly when `EXECUTOR_REQUIRE_ID=true` (StatefulSet mode,
    /// slot sharding) and no identity resolves -- an anonymous executor in a
    /// sharded pool would silently degrade every stage to broadcast.
    pub fn validate_executor_id(&self) -> Result<(), ExecutorError> {
        if self.executor_require_id && self.resolved_executor_id().is_none() {
            return Err(ExecutorError::Config(
                "EXECUTOR_REQUIRE_ID is set (StatefulSet mode) but neither EXECUTOR_ID nor \
                 POD_NAME is set; wire POD_NAME from metadata.name via the downward API"
                    .to_string(),
            ));
        }
        Ok(())
    }

    /// The process-wide resident module budget in bytes (`0` = unlimited).
    #[must_use]
    pub fn memory_budget_bytes(&self) -> u64 {
        self.executor_memory_budget_mb.saturating_mul(1024 * 1024)
    }

    /// A config good enough to build the wasmtime `Engine`/`Linker` with
    /// but nothing else -- used only by `run_healthcheck`, which probes
    /// exactly that (this process has no HTTP surface to probe, spec
    /// SS12.4/SS12.5) and has no real `STAGE_HOST_API_ADDR` to read since
    /// the container `HEALTHCHECK` invocation never sets one.
    pub fn for_healthcheck() -> Self {
        Self {
            stage_host_api_addr: "unused-for-healthcheck".to_string(),
            executor_stage_connections: 4,
            executor_call_timeout_ms: 2000,
            executor_max_call_timeout_ms: 10_000,
            executor_memory_limit_mb: 64,
            executor_max_memory_limit_mb: 256,
            executor_instances_per_bundle: 4,
            executor_fuel_limit_transform: 500_000_000,
            executor_fuel_limit_dispatch: 500_000_000,
            executor_connector_fuel_limit: 50_000_000,
            executor_connector_pool_size: 8,
            executor_max_concurrent_calls: 32,
            executor_pool_wait_ms: 500,
            executor_precompile_dir: PathBuf::from("/var/cache/waddles/wasm"),
            executor_wasm_collector: "drc".to_string(),
            sandbox_gvisor: true,
            host_api_client_cert_file: None,
            host_api_client_key_file: None,
            host_api_ca_file: None,
            host_api_stage_identity: None,
            bundle_bucket_endpoint: None,
            bundle_bucket_name: None,
            bundle_bucket_region: "us-east-1".to_string(),
            bundle_bucket_access_key_id: None,
            bundle_bucket_secret_access_key: None,
            bundle_bucket_ca_file: None,
            bundle_fetch_timeout_s: 30,
            bundle_max_component_bytes: 33_554_432,
            bundle_max_precompiled_bytes: 268_435_456,
            executor_memory_budget_mb: 8192,
            flag_disable_aot_precompile: false,
            executor_id: None,
            executor_require_id: false,
            bundle_signing_public_keys: None,
            executor_heartbeat_interval_secs: 5,
            executor_heartbeat_enabled: true,
            executor_self_ping_enabled: false,
            executor_probe_file: PathBuf::from("/tmp/executor-live"),
            executor_grace_secs: 60,
            executor_max_concurrent_compiles: 0,
            executor_shutdown_drain_secs: 25,
            connector_host_api_addr: None,
            connector_host_api_identity: None,
            connector_host_api_client_cert_file: None,
            connector_host_api_client_key_file: None,
            executor_connector_connections: 1,
            connector_pin_digest_twitch: None,
            connector_pin_digest_discord: None,
            connector_pin_digest_youtube: None,
            connector_pin_digest_spectrum: None,
        }
    }

    /// Validates cross-field invariants `clap` cannot express alone.
    pub fn validate(&self) -> Result<(), ExecutorError> {
        if self.stage_host_api_addr.trim().is_empty() {
            return Err(ExecutorError::Config(
                "STAGE_HOST_API_ADDR must not be empty".to_string(),
            ));
        }
        self.validate_stage_targets()?;
        if self.executor_call_timeout_ms == 0
            || self.executor_call_timeout_ms > self.executor_max_call_timeout_ms
        {
            return Err(ExecutorError::Config(format!(
                "EXECUTOR_CALL_TIMEOUT_MS ({}) must be > 0 and <= EXECUTOR_MAX_CALL_TIMEOUT_MS ({})",
                self.executor_call_timeout_ms, self.executor_max_call_timeout_ms
            )));
        }
        if self.executor_memory_limit_mb == 0
            || self.executor_memory_limit_mb > self.executor_max_memory_limit_mb
        {
            return Err(ExecutorError::Config(format!(
                "EXECUTOR_MEMORY_LIMIT_MB ({}) must be > 0 and <= EXECUTOR_MAX_MEMORY_LIMIT_MB ({})",
                self.executor_memory_limit_mb, self.executor_max_memory_limit_mb
            )));
        }
        if self.executor_fuel_limit_transform == 0 {
            return Err(ExecutorError::Config(
                "EXECUTOR_FUEL_LIMIT_TRANSFORM must be > 0".to_string(),
            ));
        }
        if self.executor_fuel_limit_dispatch == 0 {
            return Err(ExecutorError::Config(
                "EXECUTOR_FUEL_LIMIT_DISPATCH must be > 0".to_string(),
            ));
        }
        if self.executor_connector_fuel_limit == 0 {
            return Err(ExecutorError::Config(
                "EXECUTOR_CONNECTOR_FUEL_LIMIT must be > 0".to_string(),
            ));
        }
        if self.executor_connector_pool_size == 0 {
            return Err(ExecutorError::Config(
                "EXECUTOR_CONNECTOR_POOL_SIZE must be > 0".to_string(),
            ));
        }
        if self.executor_instances_per_bundle == 0 || self.executor_max_concurrent_calls == 0 {
            return Err(ExecutorError::Config(format!(
                "EXECUTOR_INSTANCES_PER_BUNDLE ({}) and \
                 EXECUTOR_MAX_CONCURRENT_CALLS ({}) must both be >= 1",
                self.executor_instances_per_bundle, self.executor_max_concurrent_calls,
            )));
        }
        if self.executor_wasm_collector != "drc" {
            // spec SS7.2: the collector is part of the artifact's
            // compatibility identity; this crate only wires `drc` today
            // (matching componentize-py's own output), so anything else
            // would silently produce artifacts the engine can't load.
            return Err(ExecutorError::Config(format!(
                "EXECUTOR_WASM_COLLECTOR {:?} is not supported by this build (only \"drc\")",
                self.executor_wasm_collector
            )));
        }
        self.validate_connector_link()
    }

    /// Every stage host-API listener this executor dials, parsed from
    /// `STAGE_HOST_API_ADDR` (a single `host:port`, or a comma-separated list
    /// to serve several stages from ONE process). Blank entries are skipped
    /// here; [`Self::validate`] rejects them before this is ever used.
    #[must_use]
    pub fn stage_targets(&self) -> Vec<StageTarget> {
        self.stage_host_api_addr
            .split(',')
            .map(str::trim)
            .filter(|a| !a.is_empty())
            .map(|addr| StageTarget {
                label: addr.split(':').next().unwrap_or(addr).to_string(),
                addr: addr.to_string(),
            })
            .collect()
    }

    /// A copy of this config bound to exactly one stage: its
    /// `STAGE_HOST_API_ADDR` becomes that stage's address, so every
    /// single-stage code path (dial, metrics label) works unchanged.
    #[must_use]
    pub fn for_stage(&self, target: &StageTarget) -> Self {
        let mut cfg = self.clone();
        cfg.stage_host_api_addr = target.addr.clone();
        cfg
    }

    /// The metrics/log label of this (single-stage) config's stage: the host
    /// part of its first address.
    #[must_use]
    pub fn stage_label(&self) -> String {
        self.stage_targets()
            .into_iter()
            .next()
            .map_or_else(|| "unknown".to_string(), |t| t.label)
    }

    /// Rejects a malformed multi-stage list: a blank entry (`a:1,,b:2`) or the
    /// same address twice (two dial loops on one listener would fight).
    fn validate_stage_targets(&self) -> Result<(), ExecutorError> {
        let mut seen = std::collections::HashSet::new();
        for raw in self.stage_host_api_addr.split(',') {
            let addr = raw.trim();
            if addr.is_empty() {
                return Err(ExecutorError::Config(
                    "STAGE_HOST_API_ADDR has a blank entry in its comma-separated list".to_string(),
                ));
            }
            if !seen.insert(addr.to_string()) {
                return Err(ExecutorError::Config(format!(
                    "STAGE_HOST_API_ADDR lists {addr:?} more than once"
                )));
            }
        }
        Ok(())
    }

    /// The configured connector listener address, `None` when unset/empty
    /// (no connector link is dialed).
    #[must_use]
    pub fn connector_dial_addr(&self) -> Option<&str> {
        self.connector_host_api_addr
            .as_deref()
            .filter(|a| !a.is_empty())
    }

    /// Validates the connector-runtime settings (spec S2.1-S2.3): exactly one
    /// connector connection, well-formed pin digests, and -- when a connector
    /// listener is configured -- a DNS-name address, a pinned server
    /// identity, and at least one connector to serve over the link.
    fn validate_connector_link(&self) -> Result<(), ExecutorError> {
        if self.executor_connector_connections != 1 {
            return Err(ExecutorError::Config(format!(
                "EXECUTOR_CONNECTOR_CONNECTIONS ({}) must be exactly 1: each replica runs one \
                 connector dial loop (scale out with replicas, not connections per replica)",
                self.executor_connector_connections
            )));
        }
        let mut pinned = 0usize;
        for (var, value) in self.connector_pin_digest_vars() {
            if parse_pin_digest(var, value)?.is_some() {
                pinned += 1;
            }
        }
        let Some(addr) = self.connector_dial_addr() else {
            return Ok(());
        };
        validate_service_dns_addr("CONNECTOR_HOST_API_ADDR", addr)?;
        if pinned == 0 {
            return Err(ExecutorError::Config(
                "CONNECTOR_HOST_API_ADDR requires at least one CONNECTOR_PIN_DIGEST_<P>: \
                 dialing a connector listener with nothing pinned would serve nothing"
                    .to_string(),
            ));
        }
        if self
            .connector_host_api_identity
            .as_deref()
            .is_none_or(str::is_empty)
        {
            return Err(ExecutorError::Config(
                "CONNECTOR_HOST_API_IDENTITY is required with CONNECTOR_HOST_API_ADDR: the \
                 listener's certificate must be pinned to an explicit identity"
                    .to_string(),
            ));
        }
        Ok(())
    }

    /// `(env var, raw value)` for every `CONNECTOR_PIN_DIGEST_<P>` field.
    pub(crate) fn connector_pin_digest_vars(&self) -> [(&'static str, Option<&str>); 4] {
        [
            (
                "CONNECTOR_PIN_DIGEST_TWITCH",
                self.connector_pin_digest_twitch.as_deref(),
            ),
            (
                "CONNECTOR_PIN_DIGEST_DISCORD",
                self.connector_pin_digest_discord.as_deref(),
            ),
            (
                "CONNECTOR_PIN_DIGEST_YOUTUBE",
                self.connector_pin_digest_youtube.as_deref(),
            ),
            (
                "CONNECTOR_PIN_DIGEST_SPECTRUM",
                self.connector_pin_digest_spectrum.as_deref(),
            ),
        ]
    }

    /// Validates that the host-API mutual-TLS material is fully configured
    /// (gh security review CRITICAL finding on PR #406, item 1) -- called
    /// by `crate::run` immediately before ever dialing the stage, kept
    /// SEPARATE from [`Self::validate`] rather than folded into it: this
    /// crate's own healthcheck path (`for_healthcheck`, `run_healthcheck`)
    /// builds a real `Engine`/`Linker` but never dials the stage at all, so
    /// it has no TLS material to validate and must keep calling the general
    /// [`Self::validate`] successfully; the narrower host-API tests in
    /// `crate::tls`/`crate::wire` likewise construct a `CliConfig` directly
    /// without either validation call by design.
    pub fn validate_host_api_tls(&self) -> Result<(), ExecutorError> {
        if self.host_api_ca_file.is_none() {
            return Err(ExecutorError::Config(
                "HOST_API_CA_FILE is required -- the host-API connection must verify the \
                 stage's certificate against a configured CA, never the public trust store"
                    .to_string(),
            ));
        }
        if self.host_api_client_cert_file.is_none() || self.host_api_client_key_file.is_none() {
            return Err(ExecutorError::Config(
                "HOST_API_CLIENT_CERT_FILE and HOST_API_CLIENT_KEY_FILE are both required -- \
                 mutual TLS (this executor presenting its own client certificate) is mandatory, \
                 never optional, for the host-API connection"
                    .to_string(),
            ));
        }
        if self
            .host_api_stage_identity
            .as_deref()
            .is_none_or(str::is_empty)
        {
            return Err(ExecutorError::Config(
                "HOST_API_STAGE_IDENTITY is required -- the stage's certificate must be pinned \
                 to an explicit expected identity (SPIFFE URI SAN or CN), not merely \"chains to \
                 the configured CA\""
                    .to_string(),
            ));
        }
        if self.connector_dial_addr().is_some() {
            self.validate_connector_tls()?;
        }
        Ok(())
    }

    /// The connector link's mTLS material: its OWN client cert/key (never the
    /// stage's -- a stage certificate's SANs name the stages, so reusing it
    /// would let a SAN-pinning listener accept a stage as the executor) and a
    /// server identity distinct from the stage's.
    fn validate_connector_tls(&self) -> Result<(), ExecutorError> {
        let (Some(cert), Some(key)) = (
            self.connector_host_api_client_cert_file.as_ref(),
            self.connector_host_api_client_key_file.as_ref(),
        ) else {
            return Err(ExecutorError::Config(
                "CONNECTOR_HOST_API_CLIENT_CERT_FILE and CONNECTOR_HOST_API_CLIENT_KEY_FILE are \
                 both required with CONNECTOR_HOST_API_ADDR -- the connector link needs its own \
                 executor client identity"
                    .to_string(),
            ));
        };
        if Some(cert) == self.host_api_client_cert_file.as_ref()
            || Some(key) == self.host_api_client_key_file.as_ref()
        {
            return Err(ExecutorError::Config(
                "the connector client cert/key must be distinct files from \
                 HOST_API_CLIENT_CERT_FILE/HOST_API_CLIENT_KEY_FILE: the stage certificate must \
                 never be presented as the executor identity"
                    .to_string(),
            ));
        }
        if self.connector_host_api_identity == self.host_api_stage_identity {
            return Err(ExecutorError::Config(
                "CONNECTOR_HOST_API_IDENTITY must differ from HOST_API_STAGE_IDENTITY so a stage \
                 can never impersonate the connector listener"
                    .to_string(),
            ));
        }
        Ok(())
    }
}

/// Parses one `CONNECTOR_PIN_DIGEST_<P>` value: unset or empty is `None` (not
/// pinned); anything else must be `sha256:` plus exactly 64 lowercase hex
/// digits. The error names the variable, never the value.
pub(crate) fn parse_pin_digest(
    var: &'static str,
    value: Option<&str>,
) -> Result<Option<String>, ExecutorError> {
    let Some(value) = value.filter(|v| !v.is_empty()) else {
        return Ok(None);
    };
    let well_formed = value.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64 && hex.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    });
    if well_formed {
        Ok(Some(value.to_string()))
    } else {
        Err(ExecutorError::Config(format!(
            "{var} must be `sha256:` followed by 64 lowercase hex digits"
        )))
    }
}

/// Validates a `host:port` dial address whose host is a service DNS name: a
/// non-zero port, RFC 1035-shaped labels, and never an IP literal (a service
/// is addressed by name, and a literal would be an SSRF-shaped dial target).
pub(crate) fn validate_service_dns_addr(
    var: &'static str,
    addr: &str,
) -> Result<(), ExecutorError> {
    let bad = |why: &str| ExecutorError::Config(format!("{var} {why}"));
    let (host, port) = addr
        .rsplit_once(':')
        .ok_or_else(|| bad("must be host:port"))?;
    match port.parse::<u16>() {
        Ok(p) if p != 0 => {}
        _ => return Err(bad("has an invalid port")),
    }
    if host.is_empty() || host.len() > 253 {
        return Err(bad("has an empty or over-long host"));
    }
    if host.parse::<std::net::IpAddr>().is_ok() || host.contains(['[', ']']) {
        return Err(bad("must name a service by DNS name, not an IP literal"));
    }
    let label_ok = |l: &str| {
        !l.is_empty()
            && l.len() <= 63
            && !l.starts_with('-')
            && !l.ends_with('-')
            && l.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
    };
    if !host.split('.').all(label_ok) {
        return Err(bad("host is not a valid DNS name"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use clap::Parser;

    #[test]
    fn for_healthcheck_produces_a_valid_config() -> Result<(), ExecutorError> {
        let cfg = CliConfig::for_healthcheck();
        cfg.validate()?;
        assert_eq!(cfg.executor_wasm_collector, "drc");
        assert!(cfg.sandbox_gvisor);
        assert!(cfg.host_api_client_cert_file.is_none());
        assert!(cfg.bundle_bucket_endpoint.is_none());
        assert_eq!(cfg.bundle_bucket_region, "us-east-1");
        assert_eq!(cfg.bundle_fetch_timeout_s, 30);
        assert_eq!(cfg.bundle_max_component_bytes, 33_554_432);
        assert!(cfg.bundle_signing_public_keys.is_none());
        Ok(())
    }

    #[test]
    fn defaults_parse_with_no_bundle_bucket_env_set() -> Result<(), Box<dyn std::error::Error>> {
        // The bundle-bucket fields are `Option`/defaulted precisely so that
        // this -- and every other pre-existing test's `CliConfig` fixture
        // -- keeps parsing without setting `BUNDLE_BUCKET_*` at all.
        let cfg = CliConfig::try_parse_from(base_args())?;
        assert!(cfg.bundle_bucket_endpoint.is_none());
        assert!(cfg.bundle_bucket_name.is_none());
        assert!(cfg.bundle_bucket_access_key_id.is_none());
        assert!(cfg.bundle_bucket_secret_access_key.is_none());
        assert!(cfg.bundle_bucket_ca_file.is_none());
        assert_eq!(cfg.bundle_bucket_region, "us-east-1");
        assert!(cfg.bundle_signing_public_keys.is_none());
        Ok(())
    }

    #[test]
    fn bundle_signing_public_keys_parses_from_its_env_flag(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let mut args = base_args();
        args.extend_from_slice(&["--bundle-signing-public-keys", r#"{"k1":"AAAA"}"#]);
        let cfg = CliConfig::try_parse_from(args)?;
        assert_eq!(
            cfg.bundle_signing_public_keys.as_deref(),
            Some(r#"{"k1":"AAAA"}"#)
        );
        Ok(())
    }

    fn base_args() -> Vec<&'static str> {
        vec![
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-process:8301",
        ]
    }

    #[test]
    fn defaults_parse_and_validate() -> Result<(), Box<dyn std::error::Error>> {
        let cfg = CliConfig::try_parse_from(base_args())?;
        cfg.validate()?;
        assert_eq!(cfg.stage_host_api_addr, "svc-process:8301");
        assert_eq!(cfg.executor_stage_connections, 4);
        assert!(cfg.sandbox_gvisor);
        Ok(())
    }

    #[test]
    fn empty_stage_addr_is_rejected() -> Result<(), Box<dyn std::error::Error>> {
        let mut args = base_args();
        args[2] = "";
        let cfg = CliConfig::try_parse_from(args)?;
        assert!(matches!(cfg.validate(), Err(ExecutorError::Config(_))));
        Ok(())
    }

    #[test]
    fn call_timeout_above_ceiling_is_rejected() -> Result<(), Box<dyn std::error::Error>> {
        let mut args = base_args();
        args.extend_from_slice(&["--executor-call-timeout-ms", "20000"]);
        let cfg = CliConfig::try_parse_from(args)?;
        assert!(matches!(cfg.validate(), Err(ExecutorError::Config(_))));
        Ok(())
    }

    #[test]
    fn memory_limit_above_ceiling_is_rejected() -> Result<(), Box<dyn std::error::Error>> {
        let mut args = base_args();
        args.extend_from_slice(&["--executor-memory-limit-mb", "512"]);
        let cfg = CliConfig::try_parse_from(args)?;
        assert!(matches!(cfg.validate(), Err(ExecutorError::Config(_))));
        Ok(())
    }

    #[test]
    fn zero_concurrency_knobs_are_rejected() -> Result<(), Box<dyn std::error::Error>> {
        for flag in [
            "--executor-instances-per-bundle",
            "--executor-max-concurrent-calls",
        ] {
            let mut a = base_args();
            a.extend([flag, "0"]);
            let cfg = CliConfig::try_parse_from(a)?;
            assert!(
                matches!(cfg.validate(), Err(ExecutorError::Config(_))),
                "{flag}=0 must be rejected"
            );
        }
        Ok(())
    }

    /// Compile cap: `0` = auto (1..=4), explicit values pass through.
    #[test]
    fn compile_cap_auto_and_explicit() -> Result<(), Box<dyn std::error::Error>> {
        let mut a = base_args();
        a.extend(["--executor-max-concurrent-compiles", "0"]);
        let cfg = CliConfig::try_parse_from(a)?;
        assert!(cfg.validate().is_ok(), "0 = auto must validate");
        assert!((1..=4).contains(&cfg.effective_max_concurrent_compiles()));
        let mut a = base_args();
        a.extend(["--executor-max-concurrent-compiles", "7"]);
        assert_eq!(
            CliConfig::try_parse_from(a)?.effective_max_concurrent_compiles(),
            7
        );
        Ok(())
    }

    #[test]
    fn concurrency_and_drain_defaults() -> Result<(), Box<dyn std::error::Error>> {
        let cfg = CliConfig::try_parse_from(base_args())?;
        assert_eq!(cfg.executor_max_concurrent_compiles, 0);
        let eff = cfg.effective_max_concurrent_compiles();
        assert!((1..=4).contains(&eff), "auto cap {eff} must be in 1..=4");
        assert_eq!(cfg.executor_shutdown_drain_secs, 25);
        Ok(())
    }

    const PIN: &str = "sha256:1111111111111111111111111111111111111111111111111111111111111111";

    fn connector_cfg(extra: &[&str]) -> Result<CliConfig, clap::Error> {
        let mut args = base_args();
        args.extend_from_slice(extra);
        CliConfig::try_parse_from(args)
    }

    #[test]
    fn connector_defaults_dial_nothing_and_validate() -> Result<(), Box<dyn std::error::Error>> {
        let cfg = connector_cfg(&[])?;
        cfg.validate()?;
        assert_eq!(cfg.executor_connector_connections, 1);
        assert!(cfg.connector_dial_addr().is_none());
        // An empty (helm-rendered-but-unset) address is "no dial", not an error.
        let cfg = connector_cfg(&["--connector-host-api-addr", ""])?;
        cfg.validate()?;
        assert!(cfg.connector_dial_addr().is_none());
        Ok(())
    }

    #[test]
    fn more_than_one_connector_connection_is_refused() -> Result<(), Box<dyn std::error::Error>> {
        for n in ["0", "2", "4"] {
            let cfg = connector_cfg(&["--executor-connector-connections", n])?;
            let err = cfg.validate().unwrap_err().to_string();
            assert!(err.contains("EXECUTOR_CONNECTOR_CONNECTIONS"), "{err}");
        }
        Ok(())
    }

    #[test]
    fn connector_addr_without_a_pin_is_refused() -> Result<(), Box<dyn std::error::Error>> {
        let cfg = connector_cfg(&[
            "--connector-host-api-addr",
            "svc-ingest:8301",
            "--connector-host-api-identity",
            "spiffe://x/ingest",
        ])?;
        let err = cfg.validate().unwrap_err().to_string();
        assert!(err.contains("CONNECTOR_PIN_DIGEST"), "{err}");
        Ok(())
    }

    #[test]
    fn connector_addr_requires_an_identity_and_accepts_a_full_set(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let no_identity = connector_cfg(&[
            "--connector-host-api-addr",
            "svc-ingest:8301",
            "--connector-pin-digest-twitch",
            PIN,
        ])?;
        let err = no_identity.validate().unwrap_err().to_string();
        assert!(err.contains("CONNECTOR_HOST_API_IDENTITY"), "{err}");
        let ok = connector_cfg(&[
            "--connector-host-api-addr",
            "svc-ingest.waddlebot.svc.cluster.local:8301",
            "--connector-host-api-identity",
            "spiffe://x/ingest",
            "--connector-pin-digest-twitch",
            PIN,
        ])?;
        ok.validate()?;
        assert_eq!(
            ok.connector_dial_addr(),
            Some("svc-ingest.waddlebot.svc.cluster.local:8301")
        );
        Ok(())
    }

    #[test]
    fn connector_addr_must_be_a_dns_name_with_a_valid_port() {
        for bad in [
            "127.0.0.1:8301",
            "[::1]:8301",
            "svc-ingest",
            "svc-ingest:0",
            "svc-ingest:70000",
            "svc-ingest:abc",
            ":8301",
            "-bad.svc:8301",
            "bad_label.svc:8301",
            "a..b:8301",
            "user@svc-ingest:8301",
        ] {
            assert!(
                validate_service_dns_addr("CONNECTOR_HOST_API_ADDR", bad).is_err(),
                "{bad} must be refused"
            );
        }
        for good in [
            "svc-ingest:8301",
            "svc-ingest.ns.svc.cluster.local:8301",
            "localhost:1",
        ] {
            validate_service_dns_addr("CONNECTOR_HOST_API_ADDR", good).unwrap();
        }
        let long = format!("{}:1", "a".repeat(254));
        assert!(validate_service_dns_addr("X", &long).is_err());
    }

    #[test]
    fn malformed_pin_digest_fails_validate() -> Result<(), Box<dyn std::error::Error>> {
        let cfg = connector_cfg(&["--connector-pin-digest-youtube", "sha256:zz"])?;
        let err = cfg.validate().unwrap_err().to_string();
        assert!(err.contains("CONNECTOR_PIN_DIGEST_YOUTUBE"), "{err}");
        Ok(())
    }

    #[test]
    fn malformed_spectrum_pin_digest_fails_validate() -> Result<(), Box<dyn std::error::Error>> {
        let cfg = connector_cfg(&["--connector-pin-digest-spectrum", "sha256:zz"])?;
        let err = cfg.validate().unwrap_err().to_string();
        assert!(err.contains("CONNECTOR_PIN_DIGEST_SPECTRUM"), "{err}");
        Ok(())
    }

    fn tls_ready(extra: &[&str]) -> CliConfig {
        let mut base = vec![
            "--connector-host-api-addr",
            "svc-ingest:8301",
            "--connector-host-api-identity",
            "spiffe://x/ingest",
            "--connector-pin-digest-twitch",
            PIN,
        ];
        base.extend_from_slice(extra);
        let mut cfg = connector_cfg(&base).unwrap();
        cfg.host_api_ca_file = Some(PathBuf::from("/tls/ca.pem"));
        cfg.host_api_client_cert_file = Some(PathBuf::from("/tls/stage.pem"));
        cfg.host_api_client_key_file = Some(PathBuf::from("/tls/stage.key"));
        cfg.host_api_stage_identity = Some("spiffe://x/stage".to_string());
        cfg
    }

    #[test]
    fn connector_link_needs_its_own_distinct_client_identity() {
        // Missing cert/key.
        assert!(tls_ready(&[]).validate_host_api_tls().is_err());
        // The stage cert reused as the executor identity.
        let mut reuse = tls_ready(&[]);
        reuse.connector_host_api_client_cert_file = Some(PathBuf::from("/tls/stage.pem"));
        reuse.connector_host_api_client_key_file = Some(PathBuf::from("/tls/exec.key"));
        let err = reuse.validate_host_api_tls().unwrap_err().to_string();
        assert!(err.contains("distinct"), "{err}");
        // Same server identity as the stage.
        let mut same_id = tls_ready(&[]);
        same_id.connector_host_api_client_cert_file = Some(PathBuf::from("/tls/exec.pem"));
        same_id.connector_host_api_client_key_file = Some(PathBuf::from("/tls/exec.key"));
        same_id.connector_host_api_identity = same_id.host_api_stage_identity.clone();
        assert!(same_id.validate_host_api_tls().is_err());
        // A complete, distinct set passes.
        let mut ok = tls_ready(&[]);
        ok.connector_host_api_client_cert_file = Some(PathBuf::from("/tls/exec.pem"));
        ok.connector_host_api_client_key_file = Some(PathBuf::from("/tls/exec.key"));
        ok.validate_host_api_tls().unwrap();
    }

    #[test]
    fn unsupported_collector_is_rejected() -> Result<(), Box<dyn std::error::Error>> {
        let mut args = base_args();
        args.extend_from_slice(&["--executor-wasm-collector", "null"]);
        let cfg = CliConfig::try_parse_from(args)?;
        assert!(matches!(cfg.validate(), Err(ExecutorError::Config(_))));
        Ok(())
    }
}
