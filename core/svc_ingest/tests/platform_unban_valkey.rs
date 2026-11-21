//! Real-Valkey proof of the ingest half of the Discord-native unban sync: a `GUILD_BAN_REMOVE`
//! frame on the gateway connection bound to its guild puts exactly one signed, PII-free event on
//! the list svc-action consumes; an unpaired guild, a frame for another tenant's guild, and a
//! frame through a non-gateway edge put nothing on it.
//!
//! Requires Docker (the `docker` CLI, `core/test_support`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use ring::signature::{Ed25519KeyPair, KeyPair, UnparsedPublicKey, ED25519};
use svc_ingest::commands_reconcile::BoxFut;
use svc_ingest::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};
use svc_ingest::connector_runtime::scoped::ScopeLookup;
use svc_ingest::interaction_tap::{FrameBinding, FrameTap};
use svc_ingest::platform_unban::{self, Signer, UnbanTap, ValkeySink, EVENTS_LIST};
use svc_ingest::scope_resolver::{ResolvedScope, Unresolved};
#[allow(dead_code)]
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::start_container;

const GUILD: &str = "111111111111111111";
const UNPAIRED: &str = "888888888888888888";
const OTHER_TENANT_GUILD: &str = "999999999999999999";
const USER: &str = "222222222222222222";
const UUID: &str = "11111111-2222-4333-8444-555555555555";

/// `GUILD` is paired to tenant 7, `OTHER_TENANT_GUILD` to tenant 8, anything else is unpaired.
struct Pairing;
impl ScopeLookup for Pairing {
    fn lookup<'a>(
        &'a self,
        platform: &'a str,
        source: &'a str,
    ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
        Box::pin(async move {
            assert_eq!(platform, "discord");
            match source {
                GUILD => Ok(ResolvedScope::validated(7, 9, "acme", Some("main")).unwrap()),
                OTHER_TENANT_GUILD => {
                    Ok(ResolvedScope::validated(8, 20, "other", Some("main")).unwrap())
                }
                _ => Err(Unresolved::NotFound),
            }
        })
    }
}

struct Tok;
impl IdentityTokenizer for Tok {
    fn tokenize<'a>(
        &'a self,
        scope: &'a CallScope,
        id: &'a str,
    ) -> BoxFut<'a, Result<String, TokenizeError>> {
        Box::pin(async move {
            assert_eq!((scope.tenant_id, scope.connection_id), (7, 31));
            assert_eq!(id, USER);
            Ok(UUID.to_string())
        })
    }
}

fn frame(guild: &str) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "op": 0, "s": 5, "t": "GUILD_BAN_REMOVE",
        "d": {"guild_id": guild, "user": {"id": USER}}
    }))
    .unwrap()
}

/// Tenant 7's gateway connection 31, bound to `guild`.
fn conn(guild: &str, gateway_ws: bool) -> FrameBinding {
    FrameBinding {
        tenant_id: 7,
        community_id: 9,
        connection_id: 31,
        bound_guild: Some(guild.to_owned()),
        gateway_ws,
    }
}

async fn list_len(c: &mut redis::aio::ConnectionManager) -> i64 {
    redis::cmd("LLEN")
        .arg(EVENTS_LIST)
        .query_async(c)
        .await
        .unwrap()
}

#[tokio::test]
async fn bound_gateway_forwards_one_signed_event_and_everything_else_is_dropped() {
    let valkey = start_container(
        "valkey/valkey:8.1.10-alpine",
        6379,
        &[],
        &["valkey-cli", "ping"],
        Duration::from_secs(120),
    )
    .await;
    let client =
        redis::Client::open(format!("redis://{}:{}/", valkey.host(), valkey.port())).unwrap();
    let mut conn_mgr = client.get_connection_manager().await.unwrap();
    let tap = UnbanTap::new(
        Arc::new(Pairing),
        Arc::new(Tok),
        Arc::new(ValkeySink::new(conn_mgr.clone())),
        Some(Signer::from_seed("k1", &[7u8; 32]).unwrap()),
    );

    // Unpaired guild (the connection is bound to it, but no pairing): ignored + metric.
    let before = platform_unban::count("ignored", "unpaired_guild");
    let b = conn(UNPAIRED, true);
    tap.on_frame_bound("discord", Some(&b), &frame(UNPAIRED))
        .await;
    assert_eq!(
        platform_unban::count("ignored", "unpaired_guild"),
        before + 1
    );

    // A tenant-Y guild frame arriving on tenant X's connection: rejected.
    let b = conn(GUILD, true);
    let rej = platform_unban::rejected_count("guild_not_bound_to_connection");
    tap.on_frame_bound("discord", Some(&b), &frame(OTHER_TENANT_GUILD))
        .await;
    assert_eq!(
        platform_unban::rejected_count("guild_not_bound_to_connection"),
        rej + 1
    );

    // The right frame through an HTTP/webhook edge: rejected.
    let http = conn(GUILD, false);
    let rej = platform_unban::rejected_count("not_gateway_transport");
    tap.on_frame_bound("discord", Some(&http), &frame(GUILD))
        .await;
    assert_eq!(
        platform_unban::rejected_count("not_gateway_transport"),
        rej + 1
    );
    assert_eq!(
        list_len(&mut conn_mgr).await,
        0,
        "nothing may be queued so far"
    );

    // The correct connection: exactly one signed, PII-free event.
    tap.on_frame_bound("discord", Some(&b), &frame(GUILD)).await;
    let entries: Vec<String> = redis::cmd("LRANGE")
        .arg(EVENTS_LIST)
        .arg(0)
        .arg(-1)
        .query_async(&mut conn_mgr)
        .await
        .unwrap();
    assert_eq!(entries.len(), 1);
    assert!(!entries[0].contains(USER), "raw discord id leaked");
    let v: serde_json::Value = serde_json::from_str(&entries[0]).unwrap();
    assert_eq!(
        (v["tenant_id"].as_i64(), v["connection_id"].as_i64()),
        (Some(7), Some(31))
    );
    let msg = platform_unban::signing_bytes(
        v["event_id"].as_str().unwrap(),
        7,
        31,
        "discord",
        GUILD,
        UUID,
        v["observed_ms"].as_u64().unwrap(),
        v["exp_ms"].as_u64().unwrap(),
    );
    let public = Ed25519KeyPair::from_seed_unchecked(&[7u8; 32])
        .unwrap()
        .public_key()
        .as_ref()
        .to_vec();
    UnparsedPublicKey::new(&ED25519, public)
        .verify(&msg, &B64.decode(v["sig"].as_str().unwrap()).unwrap())
        .expect("signature verifies under the public key");
}
