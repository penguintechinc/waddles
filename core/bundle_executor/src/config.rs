//! Runtime configuration, loaded from environment variables per spec
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS12.7.
//! Every secret-bearing value (the mTLS client key) is a file path, never
//! a CLI flag or an inline value -- `rules/critical-rules.md` Token &
//! Secret Hygiene.

use std::path::PathBuf;

use clap::Parser;

use crate::error::ExecutorError;

/// CLI/env configuration surface. `clap`'s `env` feature reads the SS12.7
/// variable of the same name when the flag is not passed explicitly --
/// this binary is started by its Deployment with env vars only, never
/// flags, but `clap::Parser` doubles as a typed, testable env-var reader
/// (same pattern as `core/svc_process::config::CliConfig`).
#[derive(Debug, Parser, Clone)]
#[command(name = "bundle-executor")]
pub struct CliConfig {
    /// The stage's host-API listener this executor dials, e.g.
    /// `svc-process:8301` or `svc-action:8302` (spec SS6.6/SS7.1).
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

    /// Pre-instantiated stores kept warm per loaded bundle (spec SS7.2).
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

    /// Global ceiling on in-flight calls across all loaded bundles (spec
    /// SS7.2).
    #[arg(long, env = "EXECUTOR_MAX_CONCURRENT_CALLS", default_value_t = 32)]
    pub executor_max_concurrent_calls: u32,

    /// How long a call waits for a free pooled instance before failing
    /// `pool_exhausted` (spec SS7.2).
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
}

impl CliConfig {
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
            bundle_signing_public_keys: None,
            executor_heartbeat_interval_secs: 5,
            executor_heartbeat_enabled: true,
            executor_self_ping_enabled: false,
            executor_probe_file: PathBuf::from("/tmp/executor-live"),
            executor_grace_secs: 60,
        }
    }

    /// Validates cross-field invariants `clap` cannot express alone.
    pub fn validate(&self) -> Result<(), ExecutorError> {
        if self.stage_host_api_addr.trim().is_empty() {
            return Err(ExecutorError::Config(
                "STAGE_HOST_API_ADDR must not be empty".to_string(),
            ));
        }
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
        Ok(())
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
        Ok(())
    }
}

#[cfg(test)]
mod tests {
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
    fn unsupported_collector_is_rejected() -> Result<(), Box<dyn std::error::Error>> {
        let mut args = base_args();
        args.extend_from_slice(&["--executor-wasm-collector", "null"]);
        let cfg = CliConfig::try_parse_from(args)?;
        assert!(matches!(cfg.validate(), Err(ExecutorError::Config(_))));
        Ok(())
    }
}
