//! Shadow-parity harness: for a fixed `(scope, text)`, the flag-ON dispatcher
//! path must target the same channel with the same body as the flag-OFF
//! inline REST path. The ON path enqueues to a fake queue and the OFF path
//! posts to a fake transport -- nothing real is ever sent.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody, HostResultError};
use svc_action::capabilities::{
    CapabilityHandler, InvokeScope, RelayQueue, StageCapabilities, DISCORD_DRAIN_READY_KEY,
    DISCORD_OP_ACK_KEY_PREFIX,
};
use svc_action::config::Secret;
use svc_action::distribution::BundleCatalog;
use svc_action::egress::{
    EgressGuard, EgressLimits, HttpTransport, TransportRequest, TransportResponse,
};
use svc_action::flags::{boxed, StaticFlag};
use svc_action::usage::UsageBatcher;

const APP_ID: &str = "waddles.parity.test";
const ORIGIN_CHANNEL: &str = "123456789012345678";
const BUNDLE_CHANNEL: &str = "999999999999999999";

/// Fake drain-backed queue: records pushes, advertises a ready drain and
/// acks every op carrying an `op_id`.
#[derive(Default)]
struct FakeQueue {
    pushed: Mutex<Vec<serde_json::Value>>,
    acks: Mutex<HashMap<String, String>>,
}

/// Shared handle so the test can read what the capability pushed.
struct SharedQueue(Arc<FakeQueue>);

impl RelayQueue for SharedQueue {
    fn lpush<'a>(
        &'a self,
        key: &'a str,
        value: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
        self.0.lpush(key, value)
    }
    fn key_exists<'a>(
        &'a self,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<bool, String>> + Send + 'a>> {
        self.0.key_exists(key)
    }
    fn incr_window<'a>(
        &'a self,
        key: &'a str,
        window_secs: u64,
    ) -> Pin<Box<dyn Future<Output = Result<u64, String>> + Send + 'a>> {
        self.0.incr_window(key, window_secs)
    }
    fn take<'a>(
        &'a self,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Option<String>, String>> + Send + 'a>> {
        self.0.take(key)
    }
}

impl RelayQueue for FakeQueue {
    fn lpush<'a>(
        &'a self,
        _key: &'a str,
        value: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
        Box::pin(async move {
            let v: serde_json::Value = serde_json::from_str(&value).map_err(|e| e.to_string())?;
            if let Some(id) = v.get("op_id").and_then(|i| i.as_str()) {
                self.acks.lock().map_err(|e| e.to_string())?.insert(
                    format!("{DISCORD_OP_ACK_KEY_PREFIX}{id}"),
                    r#"{"ok":true}"#.to_string(),
                );
            }
            self.pushed.lock().map_err(|e| e.to_string())?.push(v);
            Ok(())
        })
    }

    fn key_exists<'a>(
        &'a self,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<bool, String>> + Send + 'a>> {
        Box::pin(async move { Ok(key == DISCORD_DRAIN_READY_KEY) })
    }

    fn incr_window<'a>(
        &'a self,
        _key: &'a str,
        _window_secs: u64,
    ) -> Pin<Box<dyn Future<Output = Result<u64, String>> + Send + 'a>> {
        Box::pin(async { Ok(1) })
    }

    fn take<'a>(
        &'a self,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Option<String>, String>> + Send + 'a>> {
        Box::pin(async move { Ok(self.acks.lock().map_err(|e| e.to_string())?.remove(key)) })
    }
}

/// Fake Discord REST transport recording every request it is asked to send.
#[derive(Default)]
struct FakeTransport {
    requests: Mutex<Vec<TransportRequest>>,
}

impl HttpTransport for FakeTransport {
    fn send<'a>(
        &'a self,
        req: TransportRequest,
        _timeout: Duration,
        _max_response_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<TransportResponse, HostResultError>> + Send + 'a>> {
        if let Ok(mut r) = self.requests.lock() {
            r.push(req);
        }
        Box::pin(async {
            Ok(TransportResponse {
                status: 200,
                headers: vec![],
                body: b"{}".to_vec(),
                truncated: false,
            })
        })
    }
}

fn scope() -> InvokeScope {
    InvokeScope {
        tenant: "acme".to_string(),
        community: Some("main".to_string()),
        app_id: APP_ID.to_string(),
        origin_channel_id: Some(ORIGIN_CHANNEL.to_string()),
        origin_platform: Some("discord".to_string()),
        tenant_id: 7,
        community_id: 3,
        app_version: 1,
        tool_chain: Vec::new(),
        deadline_at: None,
        trace: None,
    }
}

fn gate() -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: 7,
            community_id: 3,
            app_id: APP_ID.to_string(),
            app_version: 1,
        },
        GrantSet {
            permission_snapshot_hash: "test".to_string(),
            grants: HashMap::from([(
                "chat.send:discord".to_string(),
                GrantedPermission {
                    permission_id: "chat.send:discord".to_string(),
                    params: serde_json::json!({}),
                },
            )]),
        },
    );
    Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ))
}

fn egress() -> Arc<EgressGuard> {
    Arc::new(EgressGuard::new(
        Arc::new(svc_action::egress::ReqwestTransport::new()),
        EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        Arc::new(BundleCatalog::new()),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("parity_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition"),
        boxed(StaticFlag(true)),
    ))
}

fn caps(
    on: bool,
    queue: Arc<FakeQueue>,
    transport: Arc<FakeTransport>,
) -> StageCapabilities<SharedQueue> {
    StageCapabilities::new(
        SharedQueue(queue),
        egress(),
        Arc::new(Mutex::new(UsageBatcher::new())),
        gate(),
    )
    .with_discord(transport, Secret::new("t"))
    .with_egress_dispatcher_flag(boxed(StaticFlag(on)))
}

fn chat_send(text: &str) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Relay,
        op: "send".to_string(),
        args: serde_json::json!({
            "provider": "discord",
            "message_json": serde_json::json!({"text": text, "channel": BUNDLE_CHANNEL}).to_string(),
        }),
        call_id: 1,
    }
}

#[tokio::test]
async fn dispatcher_matches_inline_for_chat_send() {
    let text = "parity check";

    // OFF: inline REST POST captured by the fake transport.
    let off_transport = Arc::new(FakeTransport::default());
    let off = caps(false, Arc::new(FakeQueue::default()), off_transport.clone());
    let off_out = off.handle(&scope(), chat_send(text)).await.expect("inline");
    let (off_channel, off_content) = {
        let reqs = off_transport.requests.lock().expect("lock");
        assert_eq!(reqs.len(), 1);
        let url = &reqs[0].url;
        let channel = url
            .strip_prefix("https://discord.com/api/v10/channels/")
            .and_then(|r| r.strip_suffix("/messages"))
            .expect("channel url")
            .to_string();
        let body: serde_json::Value =
            serde_json::from_slice(reqs[0].body.as_deref().expect("body")).expect("json");
        (
            channel,
            body["content"].as_str().expect("content").to_string(),
        )
    };

    // ON: enqueued to the fake queue; the fake transport must stay untouched.
    let on_transport = Arc::new(FakeTransport::default());
    let on_queue = Arc::new(FakeQueue::default());
    let on = caps(true, on_queue.clone(), on_transport.clone());
    let on_out = on
        .handle(&scope(), chat_send(text))
        .await
        .expect("dispatch");
    assert!(
        on_transport.requests.lock().expect("lock").is_empty(),
        "ON path must perform no inline send"
    );
    let pushed = on_queue.pushed.lock().expect("lock");
    assert_eq!(pushed.len(), 1);
    assert_eq!(pushed[0]["op"], "chat.send");
    assert_eq!(pushed[0]["channel"], off_channel.as_str());
    assert_eq!(pushed[0]["text"], off_content.as_str());
    assert_eq!(
        off_channel, ORIGIN_CHANNEL,
        "origin pinned, bundle channel ignored"
    );
    assert_eq!(on_out, off_out, "same observable result to the bundle");
}
