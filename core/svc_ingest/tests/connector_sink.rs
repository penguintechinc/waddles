//! `SpineEventSink` publishes a connector's event through the one real
//! `crate::publish::publish_event` construction site, so a connector-sourced
//! event is a fully D30-minted `StageEnvelope` on the correct per-source
//! stream -- indistinguishable downstream from a native receiver's event.
//! The appender is a recording fake because Valkey is an external dependency;
//! everything between the sink and the appender is the production code.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Mutex;

use penguin_spine::{KeyRing, NoopMetrics, Scope, SpineError, StageEnvelope};
use svc_ingest::connector::{ConnectionCtx, ConnectorEvent, EventSink, SinkError, SpineEventSink};
use svc_ingest::publish::{deterministic_workstream_id, EventAppender};

struct RecordingAppender {
    calls: Mutex<Vec<(String, StageEnvelope)>>,
    fail: bool,
}

impl EventAppender for RecordingAppender {
    async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
        if self.fail {
            return Err(SpineError::Config("connection refused".to_string()));
        }
        self.calls
            .lock()
            .unwrap()
            .push((stream.to_string(), env.clone()));
        Ok("1-0".to_string())
    }
}

struct FakeDekProvider;

impl svc_ingest::identity_crypto::DekProvider for FakeDekProvider {
    async fn get_dek(
        &self,
        _tenant_id: &str,
    ) -> Result<(svc_ingest::identity_crypto::Dek, u32), svc_ingest::identity_crypto::DekUnavailableError> {
        Ok((zeroize::Zeroizing::new([9u8; 32]), 3))
    }
}

fn test_identity_metrics() -> prometheus::IntCounterVec {
    prometheus::IntCounterVec::new(
        prometheus::Opts::new("test_identity_encryption_total", "test-only"),
        &["tenant", "status"],
    )
    .unwrap()
}

fn sink(fail: bool) -> SpineEventSink<RecordingAppender, NoopMetrics, FakeDekProvider> {
    SpineEventSink::new(
        RecordingAppender {
            calls: Mutex::new(Vec::new()),
            fail,
        },
        NoopMetrics,
        KeyRing::new(vec![("kid-1".to_string(), vec![9u8; 32])]),
        "kid-1",
        Scope::new("acme", Some("main".to_string())),
        FakeDekProvider,
        test_identity_metrics(),
    )
}

fn ctx() -> ConnectionCtx {
    ConnectionCtx {
        platform: "echo".to_string(),
        source_id: "src-1".to_string(),
        connection_id: "conn-1".to_string(),
        session_token: None,
    }
}

fn event() -> ConnectorEvent {
    ConnectorEvent {
        platform: "echo".to_string(),
        event_type: "message".to_string(),
        actor: Some("alice".to_string()),
        payload_json: r#"{"text":"hi"}"#.to_string(),
        occurred_at: "2026-10-12T00:00:00.000Z".to_string(),
    }
}

#[tokio::test]
async fn a_connector_event_is_published_as_a_d30_minted_envelope_on_its_source_stream() {
    let sink = sink(false);
    sink.publish(&ctx(), event()).await.unwrap();

    let calls = sink.appender_calls();
    assert_eq!(calls.len(), 1);
    let (stream, env) = &calls[0];
    assert_eq!(stream, "waddles:t:acme:c:main:src:echo:src-1:events");
    assert_eq!(env.workstream_id, deterministic_workstream_id("src-1"));
    assert_eq!(env.session_id.as_deref(), Some("conn-1"));
    assert_eq!(env.event.platform, "echo");
    let text_str = env.event.payload["text"].as_str().unwrap();
    let json_env: svc_ingest::identity_crypto::JsonEnvelope =
        serde_json::from_str(text_str).unwrap();
    let dek = zeroize::Zeroizing::new([9u8; 32]);
    let decrypted = svc_ingest::identity_crypto::decrypt_identity_value(
        &json_env,
        &dek,
        "acme",
        stream,
        "text",
        &env.event_id,
    )
    .unwrap();
    assert_eq!(decrypted, "hi");
    assert_eq!(env.binding.kid, "kid-1");
    assert_eq!(env.binding.mac.len(), 64);
}

#[tokio::test]
async fn an_invalid_event_is_refused_before_anything_is_appended() {
    let sink = sink(false);
    let mut bad = event();
    bad.payload_json = "[1]".to_string();
    let err = sink.publish(&ctx(), bad).await.unwrap_err();
    assert!(matches!(err, SinkError::Invalid(_)), "{err:?}");
    assert!(sink.appender_calls().is_empty());
}

#[tokio::test]
async fn an_appender_failure_surfaces_as_a_typed_publish_error() {
    let sink = sink(true);
    let err = sink.publish(&ctx(), event()).await.unwrap_err();
    assert!(matches!(err, SinkError::Publish(_)), "{err:?}");
    assert_eq!(err.kind(), "publish_error");
}

/// Test-only accessor so assertions can read the recording appender.
trait AppenderCalls {
    fn appender_calls(&self) -> Vec<(String, StageEnvelope)>;
}

impl AppenderCalls for SpineEventSink<RecordingAppender, NoopMetrics, FakeDekProvider> {
    fn appender_calls(&self) -> Vec<(String, StageEnvelope)> {
        self.appender().calls.lock().unwrap().clone()
    }
}
