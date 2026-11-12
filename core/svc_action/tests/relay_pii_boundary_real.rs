//! PII boundary, producer side, against a REAL Valkey (`VALKEY_URL`; unset
//! FAILS, never skips): svc-action forwards `{user:<uuid>}` placeholders to
//! the outbound queues UNCHANGED. It holds no resolver and never substitutes
//! a display name -- svc-ingest's drain is the only egress that detokenizes.
//!
//! Replaces the retired inline-vs-dispatcher shadow-parity harness: the
//! inline Discord REST path no longer exists, so there is nothing to compare.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};
use svc_action::capabilities::{
    CapabilityHandler, InvokeScope, StageCapabilities, DISCORD_DRAIN_READY_KEY,
    DISCORD_OP_ACK_KEY_PREFIX,
};
use svc_action::distribution::BundleCatalog;
use svc_action::egress::{EgressGuard, EgressLimits};
use svc_action::flags::{boxed, StaticFlag};
use svc_action::usage::UsageBatcher;

const APP_ID: &str = "waddles.pii.boundary.test";
const TWITCH_QUEUE: &str = "waddles:transport:irc:twitch:outbound";
const DISCORD_QUEUE: &str = "waddles:transport:irc:discord:outbound";
const TOKEN: &str = "0b9f2c1e-7d3a-4c58-9a1e-5f6d7e8a9b0c";
/// A display name a (forbidden) resolver would substitute; it must never
/// appear anywhere in what svc-action queued.
const CANARY_NAME: &str = "CanaryDisplayName";

fn scope(platform: &str, channel: &str) -> InvokeScope {
    InvokeScope {
        tenant: "acme".to_string(),
        community: Some("main".to_string()),
        app_id: APP_ID.to_string(),
        origin_channel_id: Some(channel.to_string()),
        origin_platform: Some(platform.to_string()),
        origin_guild_id: None,
        origin_user_id: None,
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
    let grants = ["chat.send:twitch", "chat.send:discord"]
        .into_iter()
        .map(|id| {
            (
                id.to_string(),
                GrantedPermission {
                    permission_id: id.to_string(),
                    params: serde_json::json!({}),
                },
            )
        })
        .collect::<HashMap<_, _>>();
    snapshot.set(
        GrantScopeKey {
            tenant_id: 7,
            community_id: 3,
            app_id: APP_ID.to_string(),
            app_version: 1,
        },
        GrantSet {
            permission_snapshot_hash: "test".to_string(),
            grants,
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
            prometheus::Opts::new("pii_boundary_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition"),
        boxed(StaticFlag(true)),
    ))
}

fn relay(provider: &str, text: &str) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Relay,
        op: "send".to_string(),
        args: serde_json::json!({
            "provider": provider,
            "message_json": serde_json::json!({"text": text}).to_string(),
        }),
        call_id: 1,
    }
}

/// Every payload currently on `key`, via a plain `LRANGE` on the real server.
async fn lrange(conn: &mut redis::aio::MultiplexedConnection, key: &str) -> Vec<String> {
    redis::cmd("LRANGE")
        .arg(key)
        .arg(0)
        .arg(-1)
        .query_async(conn)
        .await
        .expect("LRANGE the outbound queue")
}

#[tokio::test]
async fn relay_queues_the_token_unchanged_and_never_a_name_on_real_valkey() {
    let url = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: this test needs a REAL Valkey (no silent skip)");
    let client = redis::Client::open(url).expect("valid VALKEY_URL");
    let mut conn = client
        .get_multiplexed_async_connection()
        .await
        .expect("connect to real Valkey");
    let _: () = redis::cmd("DEL")
        .arg(TWITCH_QUEUE)
        .arg(DISCORD_QUEUE)
        .arg(DISCORD_DRAIN_READY_KEY)
        .query_async(&mut conn)
        .await
        .expect("clean the queues");
    // The Discord op waits for the drain's ack; stand in for the drain (it is
    // the only thing that may detokenize, and here it just acknowledges).
    let _: () = redis::cmd("SET")
        .arg(DISCORD_DRAIN_READY_KEY)
        .arg("1")
        .query_async(&mut conn)
        .await
        .expect("advertise a ready drain");

    let caps: StageCapabilities<redis::aio::MultiplexedConnection> = StageCapabilities::new(
        conn.clone(),
        egress(),
        Arc::new(Mutex::new(UsageBatcher::new())),
        gate(),
    )
    .with_discord_ack_timeout(Duration::from_secs(5))
    .with_outbound_pin_keys(
        svc_action::pin_sig::PinKeys::parse("k1:000102030405060708090a0b0c0d0e0f", Some("k1"))
            .expect("test keys"),
    );

    let text = format!("hi {{user:{TOKEN}}} welcome");

    caps.handle(&scope("twitch", "somechannel"), relay("twitch", &text))
        .await
        .expect("twitch relay queued");

    let acker_conn = conn.clone();
    let acker = tokio::spawn(async move {
        let mut c = acker_conn;
        for _ in 0..100 {
            let queued = lrange(&mut c, DISCORD_QUEUE).await;
            if let Some(raw) = queued.first() {
                let env: serde_json::Value = serde_json::from_str(raw).expect("json");
                let id = env["op_id"].as_str().expect("op_id").to_string();
                let _: () = redis::cmd("SET")
                    .arg(format!("{DISCORD_OP_ACK_KEY_PREFIX}{id}"))
                    .arg(r#"{"ok":true}"#)
                    .query_async(&mut c)
                    .await
                    .expect("ack");
                return;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        panic!("the discord op was never queued");
    });
    caps.handle(
        &scope("discord", "123456789012345678"),
        relay("discord", &text),
    )
    .await
    .expect("discord relay queued");
    acker.await.expect("acker");

    let mut examined = 0;
    for (key, tenant_wire) in [(TWITCH_QUEUE, 7), (DISCORD_QUEUE, 7)] {
        let queued = lrange(&mut conn, key).await;
        assert_eq!(queued.len(), 1, "exactly one entry on {key}");
        for raw in &queued {
            examined += 1;
            let env: serde_json::Value = serde_json::from_str(raw).expect("json");
            assert_eq!(env["text"], text.as_str(), "{key}: tokens go out unchanged");
            assert!(raw.contains(TOKEN), "{key}: the token is on the wire");
            assert!(!raw.contains(CANARY_NAME), "{key}: no display name");
            assert_eq!(
                env["tenant_id"],
                serde_json::json!(tenant_wire),
                "{key}: MAC-bound tenant stamped for the drain's lookup"
            );
            assert!(env["pin_sig"].as_str().is_some(), "{key}: signed");
        }
    }
    assert_eq!(examined, 2, "both queues examined (zero would be a vacuous pass)");
}
