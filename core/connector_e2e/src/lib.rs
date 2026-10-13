//! Real-path harness for the connector-bundle host capability (issue #840 B0).
//!
//! [`ExecutorInvoker`] implements `svc_ingest`'s [`ConnectorInvoker`] over the
//! bundle executor's in-process [`ConnectorHost`], so the host-side driver
//! (`svc_ingest::connector`) runs against the real wasmtime executor and a
//! real compiled WASM connector -- not a mock on either side of the seam.
//!
//! Production joins these two over the host-API wire as separate processes;
//! that wire cannot carry connector exports until `penguin-libs`'
//! `ExportKind` grows them. This adapter is the executable statement of the
//! contract the wire-backed invoker must also satisfy: the error mapping
//! below is exactly the three-class taxonomy of spec S2.6.

use std::collections::HashMap;

use bundle_executor::connector::{
    ConnectionCtx as ExecCtx, ConnectorCallError, ConnectorHost, FrameError, RawFrame as ExecFrame,
};
use svc_ingest::connector::{
    ConnectionCtx, ConnectorEvent, ConnectorFrameError, ConnectorInvokeError, ConnectorInvoker,
    Handshake, RawFrame,
};

/// A [`ConnectorInvoker`] backed by one executor [`ConnectorHost`] per
/// platform.
#[derive(Default)]
pub struct ExecutorInvoker {
    hosts: HashMap<String, ConnectorHost>,
}

impl ExecutorInvoker {
    /// An invoker with no connectors; every call for an unknown platform is a
    /// typed [`ConnectorInvokeError::Unavailable`].
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Serves `platform` with `host`.
    #[must_use]
    pub fn with_host(mut self, platform: impl Into<String>, host: ConnectorHost) -> Self {
        self.hosts.insert(platform.into(), host);
        self
    }

    fn host(&self, platform: &str) -> Result<&ConnectorHost, ConnectorInvokeError> {
        self.hosts.get(platform).ok_or_else(|| {
            ConnectorInvokeError::Unavailable(format!(
                "no executor connector loaded for platform {platform:?}"
            ))
        })
    }
}

fn to_exec_ctx(ctx: &ConnectionCtx) -> ExecCtx {
    ExecCtx {
        platform: ctx.platform.clone(),
        source_id: ctx.source_id.clone(),
        connection_id: ctx.connection_id.clone(),
        session_token: ctx.session_token.clone(),
    }
}

fn map_frame_error(e: FrameError) -> ConnectorFrameError {
    match e {
        FrameError::Unsupported(m) => ConnectorFrameError::Unsupported(m),
        FrameError::Malformed(m) => ConnectorFrameError::Malformed(m),
        FrameError::Backend(m) => ConnectorFrameError::Backend(m),
    }
}

/// Maps the executor's call error onto the host's three failure classes: a
/// guest `frame-error` keeps the connection; every sandbox containment tears
/// it down; everything else is "unavailable" (never blamed on the guest).
fn map_call_error(e: ConnectorCallError) -> ConnectorInvokeError {
    let kind = e.kind();
    match e {
        ConnectorCallError::Guest(fe) => ConnectorInvokeError::Guest(map_frame_error(fe)),
        ConnectorCallError::FuelExhausted(message)
        | ConnectorCallError::DeadlineExceeded(message)
        | ConnectorCallError::MemoryLimit(message)
        | ConnectorCallError::Trap(message) => ConnectorInvokeError::SandboxTrip { kind, message },
        other => ConnectorInvokeError::Unavailable(other.to_string()),
    }
}

impl ConnectorInvoker for ExecutorInvoker {
    async fn on_connect(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        let reply = self
            .host(platform)?
            .on_connect(to_exec_ctx(ctx))
            .await
            .map_err(map_call_error)?;
        Ok(Handshake {
            bytes: reply.value.bytes,
            secret_refs: reply.value.secret_refs,
        })
    }

    async fn on_frame(
        &self,
        platform: &str,
        frame: RawFrame,
    ) -> Result<Vec<ConnectorEvent>, ConnectorInvokeError> {
        let reply = self
            .host(platform)?
            .on_frame(ExecFrame {
                ctx: to_exec_ctx(&frame.ctx),
                bytes: frame.bytes,
                received_at: frame.received_at,
            })
            .await
            .map_err(map_call_error)?;
        Ok(reply
            .value
            .into_iter()
            .map(|e| ConnectorEvent {
                platform: e.platform,
                event_type: e.event_type,
                actor: e.actor,
                payload_json: e.payload_json,
                occurred_at: e.occurred_at,
            })
            .collect())
    }

    async fn on_heartbeat_due(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError> {
        Ok(self
            .host(platform)?
            .on_heartbeat_due(to_exec_ctx(ctx))
            .await
            .map_err(map_call_error)?
            .value)
    }

    async fn on_disconnect(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
        reason: &str,
    ) -> Result<(), ConnectorInvokeError> {
        self.host(platform)?
            .on_disconnect(to_exec_ctx(ctx), reason.to_string())
            .await
            .map_err(map_call_error)?;
        Ok(())
    }
}
