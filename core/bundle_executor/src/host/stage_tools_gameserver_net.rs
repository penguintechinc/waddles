//! Host side of the `stage-tools` world's `gameserver-net` import. The guest-facing calls are
//! ENCODED and FORWARDED to the stage as host calls (`capability = db`,
//! `op = "gameserver_net.<op>"`, the op-prefix pattern of [`super::stage_tools`]); the stage
//! owns the sockets, the SSRF guard, the RCON password and every limit. Nothing here decides
//! authority. The `tcp` resource is a thin handle (a stage-assigned session id); dropping it
//! tells the stage to close the socket.
//!
//! Wire contract (pinned): args are JSON `{server, scheme?}` for opens (answer `{"handle": u64}`),
//! `{handle, bytes_b64}` for `tcp_write`, `{handle, max}` for `tcp_read` (answer `{"bytes_b64"}`),
//! `{server, payload_b64, max_response}` for `udp_exchange` (answer `{"bytes_b64"}`),
//! `{handle}` for `tcp_close`. A stage refusal `code` of `denied`/`unreachable`/`timeout`/
//! `auth_failed`/`too_large`/`backend` maps onto the WIT variant; any other code (a gate refusal
//! such as `not_granted`) is `denied(code)`. The password and address never cross this boundary.

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use penguin_bundle_host::wire::CapabilityKind;
use wasmtime::component::Resource;

use crate::engine::stage_tools_world::waddle::bundle::gameserver_net as gs;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

/// Largest guest write the executor forwards (the host enforces its own, lower, caps).
const MAX_WRITE: usize = 8 * 1024;
/// Largest `server` string the executor forwards (a UUID is 36 bytes).
const MAX_SERVER: usize = 64;

fn error_from(err: ExecutorError) -> gs::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "denied" => gs::Error::Denied(message.clone()),
            "unreachable" => gs::Error::Unreachable,
            "timeout" => gs::Error::Timeout,
            "auth_failed" => gs::Error::AuthFailed,
            "too_large" => gs::Error::TooLarge,
            "backend" => gs::Error::Backend(message.clone()),
            other => gs::Error::Denied(other.to_string()),
        },
        other => gs::Error::Backend(other.to_string()),
    }
}

fn check_server(server: &str) -> Result<(), gs::Error> {
    if server.is_empty() || server.len() > MAX_SERVER {
        return Err(gs::Error::Denied("invalid_server".to_string()));
    }
    Ok(())
}

fn handle_of(v: &serde_json::Value) -> Result<u64, gs::Error> {
    v.get("handle")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| gs::Error::Backend("malformed host-result: no handle".to_string()))
}

fn bytes_of(v: &serde_json::Value) -> Result<Vec<u8>, gs::Error> {
    let s = v
        .get("bytes_b64")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| gs::Error::Backend("malformed host-result: no bytes".to_string()))?;
    B64.decode(s)
        .map_err(|_| gs::Error::Backend("malformed host-result: bytes".to_string()))
}

impl ExecState {
    /// The guest-visible `tcp` resource uses the stage's session id directly as its rep (no socket
    /// and no table entry live in the executor; wasmtime owns the guest-side handle).
    fn push_tcp(&mut self, handle: u64) -> Result<Resource<gs::Tcp>, gs::Error> {
        let rep = u32::try_from(handle)
            .map_err(|_| gs::Error::Backend("malformed host-result: handle range".to_string()))?;
        Ok(Resource::new_own(rep))
    }

    fn tcp_handle(&self, res: &Resource<gs::Tcp>) -> Result<u64, gs::Error> {
        Ok(u64::from(res.rep()))
    }
}

impl gs::Host for ExecState {
    async fn rcon_open(
        &mut self,
        server: String,
        scheme: gs::Scheme,
    ) -> Result<Resource<gs::Tcp>, gs::Error> {
        check_server(&server)?;
        let scheme = match scheme {
            gs::Scheme::SourceRcon => "source-rcon",
            gs::Scheme::MinecraftRcon => "minecraft-rcon",
        };
        let args = serde_json::json!({ "server": server, "scheme": scheme });
        let v = call(self, CapabilityKind::Db, "gameserver_net.rcon_open", args)
            .await
            .map_err(error_from)?;
        let handle = handle_of(&v)?;
        self.push_tcp(handle)
    }

    async fn tcp_open(&mut self, server: String) -> Result<Resource<gs::Tcp>, gs::Error> {
        check_server(&server)?;
        let args = serde_json::json!({ "server": server });
        let v = call(self, CapabilityKind::Db, "gameserver_net.tcp_open", args)
            .await
            .map_err(error_from)?;
        let handle = handle_of(&v)?;
        self.push_tcp(handle)
    }

    async fn udp_exchange(
        &mut self,
        server: String,
        payload: Vec<u8>,
        max_response: u32,
    ) -> Result<Vec<u8>, gs::Error> {
        check_server(&server)?;
        if payload.len() > MAX_WRITE {
            return Err(gs::Error::TooLarge);
        }
        let args = serde_json::json!({
            "server": server,
            "payload_b64": B64.encode(&payload),
            "max_response": max_response,
        });
        let v = call(
            self,
            CapabilityKind::Db,
            "gameserver_net.udp_exchange",
            args,
        )
        .await
        .map_err(error_from)?;
        bytes_of(&v)
    }
}

impl gs::HostTcp for ExecState {
    async fn write_all(
        &mut self,
        self_: Resource<gs::Tcp>,
        bytes: Vec<u8>,
    ) -> Result<(), gs::Error> {
        if bytes.len() > MAX_WRITE {
            return Err(gs::Error::TooLarge);
        }
        let handle = self.tcp_handle(&self_)?;
        let args = serde_json::json!({ "handle": handle, "bytes_b64": B64.encode(&bytes) });
        call(self, CapabilityKind::Db, "gameserver_net.tcp_write", args)
            .await
            .map(|_| ())
            .map_err(error_from)
    }

    async fn read(&mut self, self_: Resource<gs::Tcp>, max: u32) -> Result<Vec<u8>, gs::Error> {
        let handle = self.tcp_handle(&self_)?;
        let args = serde_json::json!({ "handle": handle, "max": max });
        let v = call(self, CapabilityKind::Db, "gameserver_net.tcp_read", args)
            .await
            .map_err(error_from)?;
        bytes_of(&v)
    }

    async fn drop(&mut self, rep: Resource<gs::Tcp>) -> wasmtime::Result<()> {
        let handle = u64::from(rep.rep());
        // Best effort: the stage also closes every session at the end of the invocation.
        let args = serde_json::json!({ "handle": handle });
        if let Err(e) = call(self, CapabilityKind::Db, "gameserver_net.tcp_close", args).await {
            tracing::debug!(error = %e, "gameserver-net tcp_close failed (stage cleans up)");
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use std::sync::Arc;

    use penguin_bundle_host::wire::{Frame, HostResultBody, HostResultError, Message};
    use tokio::sync::{mpsc, oneshot};

    use super::*;
    use crate::host::HostBridge;
    use crate::wire::Connection;

    /// An `ExecState` whose stage answers every host call from `answers` in order, plus a
    /// receiver of the `(op, args)` pairs observed.
    fn state_with_stage(
        answers: Vec<Result<serde_json::Value, (&'static str, &'static str)>>,
    ) -> (
        ExecState,
        mpsc::UnboundedReceiver<(String, serde_json::Value)>,
    ) {
        let (tx, mut rx) = mpsc::channel::<Frame>(8);
        let connection = Connection::new(tx);
        let stage = Arc::clone(&connection);
        let (seen_tx, seen_rx) = mpsc::unbounded_channel();
        tokio::spawn(async move {
            let mut answers = answers.into_iter();
            while let Some(frame) = rx.recv().await {
                if let Message::HostCall(body) = &frame.message {
                    let _ = seen_tx.send((body.op.clone(), body.args.clone()));
                    let reply = match answers.next() {
                        Some(Ok(v)) => HostResultBody {
                            result: Some(v),
                            error: None,
                        },
                        Some(Err((code, message))) => HostResultBody {
                            result: None,
                            error: Some(HostResultError {
                                code: code.to_string(),
                                message: message.to_string(),
                            }),
                        },
                        None => HostResultBody {
                            result: Some(serde_json::json!({})),
                            error: None,
                        },
                    };
                    let _ = stage.deliver(Frame::new(frame.id, Message::HostResult(reply)));
                }
            }
        });
        let _ = oneshot::channel::<()>();
        let state = ExecState::new(
            Some(HostBridge::new(connection)),
            "waddles.core.service.gameserver".to_string(),
            7,
        );
        (state, seen_rx)
    }

    #[tokio::test]
    async fn rcon_open_forwards_only_server_and_scheme_and_returns_a_handle_resource() {
        let (mut st, mut seen) = state_with_stage(vec![Ok(serde_json::json!({"handle": 5}))]);
        let res = gs::Host::rcon_open(
            &mut st,
            "123e4567-e89b-12d3-a456-426614174000".into(),
            gs::Scheme::SourceRcon,
        )
        .await
        .unwrap();
        let (op, args) = seen.recv().await.unwrap();
        assert_eq!(op, "gameserver_net.rcon_open");
        assert_eq!(
            args,
            serde_json::json!({"server": "123e4567-e89b-12d3-a456-426614174000", "scheme": "source-rcon"})
        );
        assert_eq!(st.tcp_handle(&res).unwrap(), 5);
    }

    #[tokio::test]
    async fn write_read_and_drop_use_the_stage_handle() {
        let (mut st, mut seen) = state_with_stage(vec![
            Ok(serde_json::json!({"handle": 9})),
            Ok(serde_json::json!({})),
            Ok(serde_json::json!({"bytes_b64": B64.encode(b"hi")})),
            Ok(serde_json::json!({})),
        ]);
        let res = gs::Host::tcp_open(&mut st, "s".into()).await.unwrap();
        let rep = res.rep();
        gs::HostTcp::write_all(&mut st, Resource::new_borrow(rep), b"abc".to_vec())
            .await
            .unwrap();
        let out = gs::HostTcp::read(&mut st, Resource::new_borrow(rep), 16)
            .await
            .unwrap();
        assert_eq!(out, b"hi");
        gs::HostTcp::drop(&mut st, res).await.unwrap();
        let ops: Vec<String> = {
            let mut v = vec![];
            while let Ok((op, args)) = seen.try_recv() {
                if op != "gameserver_net.tcp_open" {
                    assert_eq!(args["handle"], 9);
                }
                v.push(op);
            }
            v
        };
        assert_eq!(
            ops,
            [
                "gameserver_net.tcp_open",
                "gameserver_net.tcp_write",
                "gameserver_net.tcp_read",
                "gameserver_net.tcp_close"
            ]
        );
    }

    #[tokio::test]
    async fn stage_refusal_codes_map_onto_the_wit_variant() {
        for (code, want) in [
            ("denied", "Error::Denied(\"m\")"),
            ("unreachable", "Error::Unreachable"),
            ("timeout", "Error::Timeout"),
            ("auth_failed", "Error::AuthFailed"),
            ("too_large", "Error::TooLarge"),
            ("backend", "Error::Backend(\"m\")"),
            ("not_granted", "Error::Denied(\"not_granted\")"),
            ("feature_disabled", "Error::Denied(\"feature_disabled\")"),
        ] {
            let (mut st, _s) = state_with_stage(vec![Err((code, "m"))]);
            let err = gs::Host::tcp_open(&mut st, "s".into()).await.err().unwrap();
            assert_eq!(format!("{err:?}"), want, "{code}");
        }
    }

    #[tokio::test]
    async fn oversized_or_empty_server_and_big_writes_never_reach_the_stage() {
        let (mut st, mut seen) = state_with_stage(vec![]);
        assert!(matches!(
            gs::Host::tcp_open(&mut st, String::new()).await,
            Err(gs::Error::Denied(_))
        ));
        assert!(matches!(
            gs::Host::tcp_open(&mut st, "x".repeat(65)).await,
            Err(gs::Error::Denied(_))
        ));
        assert!(matches!(
            gs::Host::udp_exchange(&mut st, "s".into(), vec![0; MAX_WRITE + 1], 100).await,
            Err(gs::Error::TooLarge)
        ));
        assert!(seen.try_recv().is_err());
    }

    #[tokio::test]
    async fn no_bridge_fails_closed_and_malformed_answers_are_backend() {
        let mut bare = ExecState::new(None, "a".into(), 1);
        assert!(matches!(
            gs::Host::tcp_open(&mut bare, "s".into()).await,
            Err(gs::Error::Backend(_))
        ));
        let (mut st, _s) = state_with_stage(vec![Ok(serde_json::json!({"weird": 1}))]);
        assert!(matches!(
            gs::Host::tcp_open(&mut st, "s".into()).await,
            Err(gs::Error::Backend(_))
        ));
    }
}
