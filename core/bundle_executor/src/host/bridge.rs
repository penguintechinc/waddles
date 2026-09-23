//! [`HostBridge`]: the single funnel every WIT `stage` world import
//! (`context`/`http`/`kv`/`db`/`relay`/`%flags`/`log`/`clock`) goes
//! through to reach the stage over the wire connection (spec
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS6.5/
//! SS6.6/SS7.4). Each `Host` trait impl in `crate::host::imports` builds
//! its capability-specific JSON args, calls [`HostBridge::call`], and
//! decodes the JSON result -- the args/result JSON shape below is this
//! executor's half of a contract the stage side (blocked on M4 per
//! `core/svc_process`) implements to match.

use std::sync::Arc;

use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};

use crate::error::ExecutorError;
use crate::wire::{host_call, Connection};

/// A live handle to one host-API connection, cheap to clone and shared
/// across every concurrently-running call's `ExecState` (spec SS7.2: many
/// instances, `EXECUTOR_STAGE_CONNECTIONS` connections spread across
/// them).
pub struct HostBridge {
    connection: Arc<Connection>,
}

impl HostBridge {
    /// Wraps an already-handshaked [`Connection`].
    pub fn new(connection: Arc<Connection>) -> Arc<Self> {
        Arc::new(Self { connection })
    }

    /// Issues one `host-call` for `capability`/`op` with `args` as its
    /// JSON body, charged against the invocation `call_id` belongs to
    /// (spec SS6.6: "Time spent waiting for the stage counts against
    /// `deadline_ms`"), and returns the decoded JSON result.
    ///
    /// This is a REAL round trip over the wire protocol: the frame is
    /// serialized with `penguin_bundle_host::wire::write_frame`, carries a
    /// real correlation id, and this call does not return until either a
    /// `host-result` frame with a matching id arrives or the connection is
    /// lost -- there is no local short-circuit path that fabricates a
    /// result (`rules/general.md`: "never fake a host call").
    pub async fn call(
        &self,
        app_id: &str,
        call_id: u64,
        capability: CapabilityKind,
        op: &'static str,
        args: serde_json::Value,
    ) -> Result<serde_json::Value, ExecutorError> {
        let body = HostCallBody {
            app_id: app_id.to_string(),
            capability,
            op: op.to_string(),
            args,
            call_id,
        };
        host_call(&self.connection, body, capability_name(capability), op).await
    }
}

/// The stable string used in `ExecutorError`'s capability field, matching
/// `CapabilityKind`'s own `snake_case` wire representation.
pub(crate) fn capability_name(capability: CapabilityKind) -> &'static str {
    match capability {
        CapabilityKind::Http => "http",
        CapabilityKind::Kv => "kv",
        CapabilityKind::Db => "db",
        CapabilityKind::Relay => "relay",
        CapabilityKind::Flags => "flags",
        CapabilityKind::Log => "log",
        CapabilityKind::Clock => "clock",
        CapabilityKind::Context => "context",
    }
}
