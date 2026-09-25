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
            executor_max_concurrent_calls: 32,
            executor_pool_wait_ms: 500,
            executor_precompile_dir: PathBuf::from("/var/cache/waddles/wasm"),
            executor_wasm_collector: "drc".to_string(),
            sandbox_gvisor: true,
            host_api_client_cert_file: None,
            host_api_client_key_file: None,
            host_api_ca_file: None,
            bundle_bucket_endpoint: None,
            bundle_bucket_name: None,
            bundle_bucket_region: "us-east-1".to_string(),
            bundle_bucket_access_key_id: None,
            bundle_bucket_secret_access_key: None,
            bundle_bucket_ca_file: None,
            bundle_fetch_timeout_s: 30,
            bundle_max_component_bytes: 33_554_432,
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
