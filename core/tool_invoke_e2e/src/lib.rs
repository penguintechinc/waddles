//! Real-path harness for the bundle-to-bundle `tool.invoke` framework
//! (bundle-MCP-tools P3b, Task 7a).
//!
//! [`Rig`] stands up the production path inside one process: a REAL
//! `bundle_executor::Executor` (wasmtime, the production `stage-tools` linker,
//! the frame codec) speaking the host-API wire over a duplex pipe to
//! svc-process's REAL host-API [`Connection`], with REAL componentized bundles
//! loaded. The caller drives a call chain by invoking a bundle's exported tool
//! with the REAL per-invoke `StageCapabilities` (so every nested `tool.invoke`
//! goes through the shipped `handle_tool` gate/matrix/guards and the shipped
//! nested-invoke helper). Only the pipe stands in for the mTLS socket.

use std::collections::HashMap;
use std::sync::Arc;

use bundle_executor::config::CliConfig;
use bundle_executor::error::ExecutorError;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::wire::run_connection as run_executor_connection;
use clap::Parser;
use penguin_bundle_host::wire::{
    ExportKind, HelloBody, HelloLimits, HelloOkBody, InvokeBody, LoadBody, LoadLimits, Message,
    SandboxInfo,
};
use sha2::{Digest, Sha256};
use svc_process::capabilities::{CapabilityHandler, DenyAllCapabilities};
use svc_process::host_api::{
    run_connection as run_stage_connection, Connection, ConnectionRegistry,
};
use tokio::io::duplex;
use tokio::task::JoinHandle;

/// Bytes the in-memory pipe buffers each way (far above any frame here).
const PIPE_BYTES: usize = 4 << 20;

/// `sha256:<hex>` of `bytes`, the digest form the executor and registry use.
#[must_use]
pub fn digest_of(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("sha256:{:x}", hasher.finalize())
}

/// Serves component bytes by `component_key` (the app id); an unknown key is a
/// typed error -- never fabricated bytes.
pub struct MapSource(HashMap<String, Vec<u8>>);

impl ComponentSource for MapSource {
    async fn fetch(
        &self,
        component_key: &str,
        _sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
        // No `bundle_signing_public_keys` configured, so the `{}` stub sidecar
        // is never parsed (same convention as the executor's own tests).
        self.0
            .get(component_key)
            .map(|wasm| (wasm.clone(), b"{}".to_vec()))
            .ok_or_else(|| {
                ExecutorError::Config(format!("tool-invoke-e2e: no component {component_key:?}"))
            })
    }
}

/// A live executor + svc-process host-API connection with bundles loaded.
pub struct Rig {
    /// The stage's connection registry (`handle_tool` resolves `.active()` at
    /// call time, exactly as in production).
    pub registry: Arc<ConnectionRegistry>,
    /// The handshaken stage-side connection.
    pub connection: Arc<Connection>,
    tasks: Vec<JoinHandle<()>>,
}

impl Rig {
    /// Starts the executor + stage link and `Load`s every `(app_id, wasm)`
    /// (the component key is the app id; the digest is the bytes' sha256).
    ///
    /// # Errors
    /// A human-readable reason when the executor, the handshake or any load
    /// fails -- the harness never proceeds on a half-built rig.
    pub async fn start(components: &[(&str, &'static [u8])]) -> Result<Self, String> {
        let cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-process:8301",
        ])
        .map_err(|e| e.to_string())?;
        let source = MapSource(
            components
                .iter()
                .map(|(id, wasm)| ((*id).to_string(), wasm.to_vec()))
                .collect(),
        );
        let executor = Arc::new(Executor::new(&cfg, source).map_err(|e| e.to_string())?);

        let (executor_io, stage_io) = duplex(PIPE_BYTES);
        let hello = HelloBody {
            protocol_version: 1,
            executor_version: "0.1.0".to_string(),
            wasmtime_version: bundle_executor::engine::WASMTIME_VERSION.to_string(),
            wasmtime_abi: bundle_executor::engine::WASMTIME_VERSION.to_string(),
            collector: "drc".to_string(),
            sandbox: SandboxInfo {
                runtime: "runc".to_string(),
                verified: false,
            },
        };
        let mut tasks = Vec::new();
        tasks.push(tokio::spawn(async move {
            let _ = run_executor_connection(
                executor_io,
                hello,
                executor,
                "tool-invoke-e2e",
                Heartbeat::disabled(),
            )
            .await;
        }));
        let (connection, read_loop) = run_stage_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: HelloLimits {
                    call_timeout_ms: 8000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .map_err(|e| format!("stage handshake: {e}"))?;
        tasks.push(tokio::spawn(async move {
            let _ = read_loop.await;
        }));
        let registry = Arc::new(ConnectionRegistry::new());
        registry.set_active(Arc::clone(&connection));
        let rig = Self {
            registry,
            connection,
            tasks,
        };
        for (app_id, wasm) in components {
            rig.load(app_id, wasm).await?;
        }
        Ok(rig)
    }

    async fn load(&self, app_id: &str, wasm: &[u8]) -> Result<(), String> {
        let frame = self
            .connection
            .request(Message::Load(LoadBody {
                tenant_id: 1,
                community_id: 0,
                app_id: app_id.to_string(),
                version: "1".to_string(),
                digest: digest_of(wasm),
                component_key: app_id.to_string(),
                sidecar_key: "s".to_string(),
                capabilities: vec![],
                limits: LoadLimits {
                    timeout_ms: 8000,
                    memory_mb: 64,
                },
            }))
            .await
            .map_err(|e| format!("load {app_id}: {e}"))?;
        match frame.message {
            Message::Loaded(_) => Ok(()),
            other => Err(format!("load {app_id}: unexpected reply {other:?}")),
        }
    }

    /// Runs `tool` on `app_id` as the top-level invocation (`export =
    /// ToolCall`) under `capabilities` (the REAL per-invoke stage scope whose
    /// call chain starts at `app_id`). Returns the executor's reply message --
    /// a `Result` carrying `{"ok": ..}` / `{"tool_error": ..}`, or an `Error`.
    ///
    /// # Errors
    /// The host-API transport failure, rendered.
    pub async fn call_tool(
        &self,
        app_id: &str,
        wasm: &[u8],
        tool: &str,
        args: serde_json::Value,
        deadline_ms: u64,
        capabilities: Arc<dyn CapabilityHandler>,
    ) -> Result<Message, String> {
        let frame = self
            .connection
            .invoke(
                InvokeBody {
                    app_id: app_id.to_string(),
                    digest: digest_of(wasm),
                    export: ExportKind::ToolCall,
                    payload: serde_json::json!({
                        "tool": tool,
                        "args": args,
                        "call_chain": [app_id],
                        "idempotency_key": null,
                    }),
                    deadline_ms,
                    trace: None,
                },
                capabilities,
            )
            .await
            .map_err(|e| e.to_string())?;
        Ok(frame.message)
    }
}

impl Drop for Rig {
    fn drop(&mut self) {
        for t in &self.tasks {
            t.abort();
        }
    }
}
