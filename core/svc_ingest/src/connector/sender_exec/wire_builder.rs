//! The production [`TemplateBuilder`]: `sender.build-request` carried over the
//! executor host-API wire (`penguin-bundle-host` 0.5.0
//! `ExportKind::ConnectorBuildRequest`).
//!
//! ```text
//!   SenderExecutor ──> WireTemplateBuilder ──invoke frame──> bundle_executor
//!                          │                                   (ConnectorHost
//!                          │ registry.resolve(platform)          .build_request)
//!                          └─> pinned (app_id, digest)
//! ```
//!
//! The builder resolves a platform to its digest-pinned connector in the
//! [`ConnectorRegistry`], sends the `action` (the executor refuses an unpinned
//! digest or a mismatched `app_id` before any guest runs), and decodes the
//! reply STRICTLY into a [`BuiltTemplate`] whose `component_digest` is the
//! registry's pinned digest -- the value the executor's own pin re-check
//! compares against `SENDER_EXEC_DIGEST_<PLATFORM>`.
//!
//! # Failure classes (typed, never a silent fallback)
//!
//! | Wire outcome | [`BuildError`] |
//! |---|---|
//! | `result` with `{"frame_error"}` | `Guest(unsupported/malformed/backend)` |
//! | `error` `EXECUTOR_DEADLINE`/`MEMORY_LIMIT`/`WASM_TRAP` | `Sandbox(deadline/fuel_exhausted/memory_limit/trap)` |
//! | unpinned platform, no executor, lost connection, any other code, malformed reply | `Unavailable` |
//!
//! # Boot wiring seam (NOT wired in this change)
//!
//! Nothing in `lib.rs` constructs a [`WireTemplateBuilder`] yet: boot still
//! starts the legacy IRC/Discord drains. The follow-up (after the generic
//! executor, PR #999, merges) builds, at the point `lib.rs` starts the
//! outbound drains and where it already owns the `Arc<ExecutorLink>` and the
//! `Arc<ConnectorRegistry>` shared with the connector driver:
//!
//! ```ignore
//! let builder = WireTemplateBuilder::new(Arc::clone(&link), Arc::clone(&registry));
//! let exec = SenderExecutor::new(builder, /* flags, slots, policy, guarded client */);
//! // then start `outbound::run_exec(&spine_cfg, &exec, pin, shutdown)` /
//! // `outbound::run_discord_exec(..)` INSTEAD of the legacy drains, behind
//! // the `waddles.connector-tools` flag, and drop `RealIrcOutbound` /
//! // `discord_rest.rs` once verified.
//! ```
//!
//! # Logging
//!
//! Only the platform slug and the error class are ever logged -- never the
//! action payload, the template, or executor/guest-supplied text.

use std::sync::Arc;

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use penguin_bundle_host::wire::ExportKind;
use serde::Deserialize;
use tracing::warn;

use super::{BuildError, BuiltTemplate, RequestTpl, SenderAction, TemplateBuilder};
use crate::connector::{
    ConnectorFrameError, ConnectorInvokeError, ConnectorRegistry, ExecutorLink,
};

/// Wire `http-request-tpl` reply (`bundle_executor::connector_wire`).
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TemplateReply {
    method: String,
    url: String,
    headers: Vec<(String, String)>,
    body: Option<String>,
    secret_refs: Vec<(String, String)>,
}

/// Wire guest `frame-error` reply.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FrameErrorEnvelope {
    frame_error: WireFrameError,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WireFrameError {
    kind: String,
    // Present on the wire; deliberately never read or logged (it can echo
    // guest-supplied text).
    #[allow(dead_code)]
    message: String,
}

/// [`TemplateBuilder`] over the executor wire: runs `sender.build-request` on
/// the digest-pinned connector for a platform. Cheap to share; holds no
/// per-call state.
pub struct WireTemplateBuilder {
    link: Arc<ExecutorLink>,
    registry: Arc<ConnectorRegistry>,
}

impl WireTemplateBuilder {
    /// A builder for the connectors pinned in `registry`, reaching the
    /// executor over `link`.
    #[must_use]
    pub fn new(link: Arc<ExecutorLink>, registry: Arc<ConnectorRegistry>) -> Self {
        Self { link, registry }
    }
}

/// Maps a transport-level invoke failure onto the builder's typed classes.
fn map_invoke_error(e: ConnectorInvokeError) -> BuildError {
    match e {
        ConnectorInvokeError::Guest(fe) => BuildError::Guest(fe.kind()),
        ConnectorInvokeError::SandboxTrip { kind, .. } => BuildError::Sandbox(kind),
        ConnectorInvokeError::Unavailable(_) => BuildError::Unavailable,
    }
}

/// Maps the guest's wire `frame-error` kind (unknown kinds are NOT guessed).
fn frame_error_class(kind: &str) -> Result<&'static str, BuildError> {
    match kind {
        "unsupported" => Ok(ConnectorFrameError::Unsupported(String::new()).kind()),
        "malformed" => Ok(ConnectorFrameError::Malformed(String::new()).kind()),
        "backend" => Ok(ConnectorFrameError::Backend(String::new()).kind()),
        _ => Err(BuildError::Unavailable),
    }
}

/// Decodes the executor's reply payload strictly into a [`RequestTpl`]; any
/// shape drift is `Unavailable` (fail loud), never a guess.
fn decode_template(payload: serde_json::Value) -> Result<RequestTpl, BuildError> {
    if payload.get("frame_error").is_some() {
        let env: FrameErrorEnvelope =
            serde_json::from_value(payload).map_err(|_| BuildError::Unavailable)?;
        return Err(BuildError::Guest(frame_error_class(&env.frame_error.kind)?));
    }
    let reply: TemplateReply =
        serde_json::from_value(payload).map_err(|_| BuildError::Unavailable)?;
    let body = reply
        .body
        .as_deref()
        .map(|b| BASE64.decode(b))
        .transpose()
        .map_err(|_| BuildError::Unavailable)?;
    Ok(RequestTpl {
        method: reply.method,
        url: reply.url,
        headers: reply.headers,
        body,
        secret_refs: reply.secret_refs,
    })
}

impl TemplateBuilder for WireTemplateBuilder {
    /// Runs `sender.build-request` for `platform` over the wire. An unpinned
    /// platform is `Unavailable` (never a fallback to some other connector).
    async fn build_request(
        &self,
        platform: &str,
        action: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError> {
        let pinned = self.registry.resolve(platform).map_err(|_| {
            warn!(platform, "build-request: platform has no pinned connector");
            BuildError::Unavailable
        })?;
        let payload = serde_json::json!({
            "kind": action.kind,
            "target_source_id": action.target_source_id,
            "payload_json": action.payload_json,
        });
        let reply = self
            .link
            .call(
                &pinned.app_id,
                &pinned.digest,
                ExportKind::ConnectorBuildRequest,
                payload,
            )
            .await
            .map_err(|e| {
                let class = map_invoke_error(e);
                warn!(platform, error = %class, "build-request invoke failed");
                class
            })?;
        let tpl = decode_template(reply.payload).inspect_err(|class| {
            warn!(platform, error = %class, "build-request reply refused");
        })?;
        Ok(BuiltTemplate {
            tpl,
            component_digest: pinned.digest.clone(),
        })
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::connector::registry::PinnedConnector;
    use penguin_bundle_host::wire::{
        read_frame, write_frame, ErrorBody, ErrorCode, Frame, HelloBody, InvokeBody, Message,
        ResultBody, SandboxInfo,
    };
    use std::time::Duration;
    use tokio::io::{duplex, DuplexStream};

    const PLATFORM: &str = "twitch";
    const APP_ID: &str = "waddles.core.connector.twitch";
    const DIGEST: &str = "sha256:abc";

    fn action() -> SenderAction {
        SenderAction {
            kind: "chat.send".to_string(),
            target_source_id: "src-1".to_string(),
            payload_json: r#"{"text":"hi"}"#.to_string(),
        }
    }

    fn registry() -> Arc<ConnectorRegistry> {
        let mut r = ConnectorRegistry::new();
        r.register(PinnedConnector::new(PLATFORM, APP_ID, DIGEST, 4096))
            .unwrap();
        Arc::new(r)
    }

    /// Scripted executor half, post-handshake (mirrors `wire_invoker` tests).
    struct FakeExecutor {
        io: DuplexStream,
    }

    impl FakeExecutor {
        async fn attach(link: &Arc<ExecutorLink>) -> Self {
            let (stage_io, mut exec_io) = duplex(1 << 20);
            let link2 = Arc::clone(link);
            let attach = tokio::spawn(async move { link2.attach(stage_io).await });
            let hello = Message::Hello(HelloBody {
                protocol_version: 1,
                executor_version: "t".to_string(),
                wasmtime_version: "t".to_string(),
                wasmtime_abi: "t".to_string(),
                collector: "drc".to_string(),
                sandbox: SandboxInfo {
                    runtime: "runc".to_string(),
                    verified: false,
                },
                executor_id: None,
            });
            write_frame(&mut exec_io, &Frame::new(1, hello))
                .await
                .unwrap();
            let ok = read_frame(&mut exec_io).await.unwrap();
            assert!(matches!(ok.message, Message::HelloOk(_)));
            attach.await.unwrap().unwrap();
            Self { io: exec_io }
        }

        async fn next_invoke(&mut self) -> (u64, InvokeBody) {
            let frame = read_frame(&mut self.io).await.unwrap();
            match frame.message {
                Message::Invoke(body) => (frame.id, body),
                other => panic!("expected invoke, got {other:?}"),
            }
        }

        async fn reply_ok(&mut self, id: u64, payload: serde_json::Value) {
            let m = Message::Result(ResultBody {
                payload,
                duration_ms: 1,
                fuel_used: 1,
            });
            write_frame(&mut self.io, &Frame::new(id, m)).await.unwrap();
        }

        async fn reply_err(&mut self, id: u64, code: ErrorCode, detail: Option<&str>) {
            let m = Message::Error(ErrorBody {
                code,
                message: "guest text must not leak".to_string(),
                detail: detail.map(str::to_string),
            });
            write_frame(&mut self.io, &Frame::new(id, m)).await.unwrap();
        }
    }

    async fn rig() -> (Arc<WireTemplateBuilder>, FakeExecutor) {
        let link = Arc::new(ExecutorLink::new().with_call_timeout(Duration::from_millis(400)));
        let exec = FakeExecutor::attach(&link).await;
        let builder = Arc::new(WireTemplateBuilder::new(Arc::clone(&link), registry()));
        (builder, exec)
    }

    /// Runs one build against a scripted answer.
    async fn build_with(
        answer: impl FnOnce(&mut FakeExecutor, u64) -> futures_util::future::BoxFuture<'_, ()>,
    ) -> Result<BuiltTemplate, BuildError> {
        let (builder, mut exec) = rig().await;
        let call = tokio::spawn({
            let b = Arc::clone(&builder);
            async move { b.build_request(PLATFORM, &action()).await }
        });
        let (id, _) = exec.next_invoke().await;
        answer(&mut exec, id).await;
        call.await.unwrap()
    }

    #[tokio::test]
    async fn the_request_carries_the_pinned_identity_and_the_action_fields() {
        let (builder, mut exec) = rig().await;
        let call = tokio::spawn({
            let b = Arc::clone(&builder);
            async move { b.build_request(PLATFORM, &action()).await }
        });
        let (id, body) = exec.next_invoke().await;
        assert_eq!(body.export, ExportKind::ConnectorBuildRequest);
        assert_eq!(body.app_id, APP_ID);
        assert_eq!(body.digest, DIGEST);
        assert_eq!(
            body.payload,
            serde_json::json!({
                "kind": "chat.send",
                "target_source_id": "src-1",
                "payload_json": "{\"text\":\"hi\"}",
            })
        );
        exec.reply_ok(
            id,
            serde_json::json!({
                "method": "POST",
                "url": "https://api.example.test/x",
                "headers": [["Content-Type", "application/json"]],
                "body": BASE64.encode(b"{}"),
                "secret_refs": [["token", "handle-1"]],
            }),
        )
        .await;
        let built = call.await.unwrap().unwrap();
        assert_eq!(built.component_digest, DIGEST);
        assert_eq!(built.tpl.method, "POST");
        assert_eq!(built.tpl.url, "https://api.example.test/x");
        assert_eq!(
            built.tpl.headers,
            vec![("Content-Type".to_string(), "application/json".to_string())]
        );
        assert_eq!(built.tpl.body.as_deref(), Some(b"{}".as_slice()));
        assert_eq!(
            built.tpl.secret_refs,
            vec![("token".to_string(), "handle-1".to_string())]
        );
    }

    #[tokio::test]
    async fn a_template_without_a_body_decodes_to_none() {
        let built = build_with(|exec, id| {
            Box::pin(exec.reply_ok(
                id,
                serde_json::json!({
                    "method": "DELETE", "url": "https://a.test/", "headers": [],
                    "body": null, "secret_refs": [],
                }),
            ))
        })
        .await
        .unwrap();
        assert!(built.tpl.body.is_none());
    }

    #[tokio::test]
    async fn each_guest_frame_error_kind_is_a_typed_guest_error() {
        for kind in ["unsupported", "malformed", "backend"] {
            let err = build_with(|exec, id| {
                Box::pin(exec.reply_ok(
                    id,
                    serde_json::json!({"frame_error": {"kind": kind, "message": "m"}}),
                ))
            })
            .await
            .unwrap_err();
            assert_eq!(err, BuildError::Guest(frame_error_class(kind).unwrap()));
        }
    }

    #[tokio::test]
    async fn shape_drift_and_unknown_kinds_are_unavailable_not_guessed() {
        for payload in [
            serde_json::json!({"frame_error": {"kind": "weird", "message": "m"}}),
            serde_json::json!({"frame_error": {"kind": "backend"}}),
            serde_json::json!({"method": "POST"}),
            serde_json::json!({
                "method": "POST", "url": "u", "headers": [], "body": null,
                "secret_refs": [], "extra": 1,
            }),
            serde_json::json!({
                "method": "POST", "url": "u", "headers": [], "body": "!!not-base64!!",
                "secret_refs": [],
            }),
        ] {
            let err = build_with(|exec, id| Box::pin(exec.reply_ok(id, payload)))
                .await
                .unwrap_err();
            assert_eq!(err, BuildError::Unavailable);
        }
    }

    #[tokio::test]
    async fn executor_error_codes_map_to_sandbox_or_unavailable_by_code_not_text() {
        let cases = [
            (
                ErrorCode::ExecutorDeadline,
                Some("fuel_exhausted"),
                BuildError::Sandbox("fuel_exhausted"),
            ),
            (
                ErrorCode::ExecutorDeadline,
                None,
                BuildError::Sandbox("deadline"),
            ),
            (
                ErrorCode::MemoryLimit,
                None,
                BuildError::Sandbox("memory_limit"),
            ),
            (ErrorCode::WasmTrap, None, BuildError::Sandbox("trap")),
            (
                ErrorCode::ExecutorDeadline,
                Some("pool_exhausted"),
                BuildError::Unavailable,
            ),
            (ErrorCode::UnknownBundle, None, BuildError::Unavailable),
            (ErrorCode::MalformedFrame, None, BuildError::Unavailable),
            (ErrorCode::ExportMissing, None, BuildError::Unavailable),
        ];
        for (code, detail, want) in cases {
            let err = build_with(|exec, id| Box::pin(exec.reply_err(id, code, detail)))
                .await
                .unwrap_err();
            assert_eq!(err, want, "{code:?}/{detail:?}");
            assert!(!err.to_string().contains("guest text"));
        }
    }

    #[tokio::test]
    async fn an_unpinned_platform_is_unavailable_and_nothing_is_sent() {
        let (builder, mut exec) = rig().await;
        let err = builder
            .build_request("discord", &action())
            .await
            .unwrap_err();
        assert_eq!(err, BuildError::Unavailable);
        // Nothing reached the executor: the next frame it sees is for a later,
        // pinned call.
        let call = tokio::spawn({
            let b = Arc::clone(&builder);
            async move { b.build_request(PLATFORM, &action()).await }
        });
        let (_, body) = exec.next_invoke().await;
        assert_eq!(body.app_id, APP_ID);
        call.abort();
    }

    #[tokio::test]
    async fn no_attached_executor_is_unavailable() {
        let link = Arc::new(ExecutorLink::new());
        let builder = WireTemplateBuilder::new(link, registry());
        let err = builder
            .build_request(PLATFORM, &action())
            .await
            .unwrap_err();
        assert_eq!(err, BuildError::Unavailable);
    }

    #[tokio::test]
    async fn a_silent_executor_times_out_unavailable() {
        let (builder, mut exec) = rig().await;
        let call = tokio::spawn({
            let b = Arc::clone(&builder);
            async move { b.build_request(PLATFORM, &action()).await }
        });
        let _ = exec.next_invoke().await; // never answered
        assert_eq!(call.await.unwrap().unwrap_err(), BuildError::Unavailable);
    }
}
