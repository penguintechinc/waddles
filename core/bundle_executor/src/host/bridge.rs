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

/// The reserved top-level key under which the executor stamps the host-derived
/// call scope into every connector host-call's `args` (connector runtime spec
/// S2.4). Stamped AFTER the import handler built the args, overwriting any
/// existing value, so a guest can never choose it.
pub const SCOPE_KEY: &str = "_scope";

/// What a bridge does with [`SCOPE_KEY`] on the way out.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScopeStamp {
    /// Stage-link bridge: args are forwarded untouched.
    Unscoped,
    /// Connector-link call whose export carries no `ConnectionCtx`
    /// (`tool-call`, `build-request`): any `_scope` is REMOVED so the listener
    /// sees an unscoped call and denies it. Never a guessed scope.
    Strip,
    /// Connector-link call carrying this host-derived `connection_id`.
    Set(String),
}

impl ScopeStamp {
    /// Applies the stamp to `args`, returning the args to put on the wire.
    fn apply(
        &self,
        capability: CapabilityKind,
        op: &'static str,
        mut args: serde_json::Value,
    ) -> Result<serde_json::Value, ExecutorError> {
        match self {
            Self::Unscoped => Ok(args),
            Self::Strip => {
                if let Some(map) = args.as_object_mut() {
                    map.remove(SCOPE_KEY);
                }
                Ok(args)
            }
            Self::Set(connection_id) => {
                let Some(map) = args.as_object_mut() else {
                    return Err(ExecutorError::MalformedHostCall {
                        capability: capability_name(capability),
                        op,
                        detail: "host-call args must be a JSON object to carry a scope".to_string(),
                    });
                };
                map.insert(
                    SCOPE_KEY.to_string(),
                    serde_json::json!({ "connection_id": connection_id }),
                );
                Ok(args)
            }
        }
    }
}

/// A live handle to one host-API connection, cheap to clone and shared
/// across every concurrently-running call's `ExecState` (spec SS7.2: many
/// instances, `EXECUTOR_STAGE_CONNECTIONS` connections spread across
/// them).
pub struct HostBridge {
    connection: Arc<Connection>,
    stamp: ScopeStamp,
}

impl HostBridge {
    /// Wraps an already-handshaked [`Connection`]; args are forwarded
    /// untouched ([`ScopeStamp::Unscoped`]).
    pub fn new(connection: Arc<Connection>) -> Arc<Self> {
        Arc::new(Self {
            connection,
            stamp: ScopeStamp::Unscoped,
        })
    }

    /// A bridge for ONE connector export call arriving over the connector
    /// link: every host-call goes back over that same `connection` and carries
    /// `_scope = {connection_id}` (`Some`), or has any `_scope` stripped
    /// (`None`: an export with no `ConnectionCtx`).
    pub fn for_connector_call(
        connection: Arc<Connection>,
        connection_id: Option<String>,
    ) -> Arc<Self> {
        Arc::new(Self {
            connection,
            stamp: connection_id.map_or(ScopeStamp::Strip, ScopeStamp::Set),
        })
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
        let args = self.stamp.apply(capability, op, args)?;
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

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use penguin_bundle_host::wire::{Frame, HostResultBody, Message};
    use tokio::sync::mpsc;

    /// Runs one `call` through the bridge `make` builds, against an in-memory
    /// writer, and returns the args exactly as they were put on the wire.
    async fn wire_args(
        make: impl FnOnce(Arc<Connection>) -> Arc<HostBridge>,
        args: serde_json::Value,
    ) -> Result<serde_json::Value, ExecutorError> {
        let (tx, mut rx) = mpsc::channel::<Frame>(8);
        let connection = Connection::new(tx);
        let bridge = make(Arc::clone(&connection));
        let answer = Arc::clone(&connection);
        let responder = tokio::spawn(async move {
            let frame = rx.recv().await.expect("a host-call frame");
            let Message::HostCall(call) = &frame.message else {
                panic!("expected host-call");
            };
            let seen = call.args.clone();
            answer
                .deliver(Frame::new(
                    frame.id,
                    Message::HostResult(HostResultBody {
                        result: Some(serde_json::json!({})),
                        error: None,
                    }),
                ))
                .expect("deliver");
            seen
        });
        bridge
            .call(
                "waddles.core.service.x",
                9,
                CapabilityKind::Flags,
                "enabled",
                args,
            )
            .await?;
        Ok(responder.await.unwrap())
    }

    #[tokio::test]
    async fn connector_call_stamps_the_host_scope_over_a_forged_one() {
        let forged = serde_json::json!({ "key": "k", "_scope": { "connection_id": "evil" } });
        let seen = wire_args(
            |c| HostBridge::for_connector_call(c, Some("conn-7".to_string())),
            forged,
        )
        .await
        .unwrap();
        assert_eq!(seen["_scope"]["connection_id"], "conn-7");
        assert_eq!(seen["key"], "k");
    }

    #[tokio::test]
    async fn ctxless_connector_call_strips_any_scope() {
        let forged = serde_json::json!({ "key": "k", "_scope": { "connection_id": "evil" } });
        let seen = wire_args(|c| HostBridge::for_connector_call(c, None), forged)
            .await
            .unwrap();
        assert!(seen.get("_scope").is_none(), "{seen}");
    }

    #[tokio::test]
    async fn stage_bridge_forwards_args_untouched() {
        let args = serde_json::json!({ "key": "k", "_scope": 1 });
        let seen = wire_args(HostBridge::new, args.clone()).await.unwrap();
        assert_eq!(seen, args);
    }

    #[tokio::test]
    async fn scoped_call_with_non_object_args_fails_loud() {
        let err = wire_args(
            |c| HostBridge::for_connector_call(c, Some("c".to_string())),
            serde_json::json!([1]),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ExecutorError::MalformedHostCall { .. }),
            "{err:?}"
        );
    }
}
