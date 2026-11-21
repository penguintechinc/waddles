//! Real-Valkey proof of the ingest half of the Discord-native unban sync: a `GUILD_BAN_REMOVE`
//! frame for a paired guild puts exactly one MAC-verifiable, PII-free event on the list svc-action
//! consumes; an unpaired guild puts nothing on it and is counted as ignored.
//!
//! Requires Docker (the `docker` CLI, `core/test_support`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use svc_ingest::commands_reconcile::BoxFut;
use svc_ingest::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};
use svc_ingest::connector_runtime::scoped::ScopeLookup;
use svc_ingest::interaction_tap::FrameTap;
use svc_ingest::pin_sig::PinKeys;
use svc_ingest::platform_unban::{self, UnbanTap, ValkeySink, EVENTS_LIST};
use svc_ingest::scope_resolver::{ResolvedScope, Unresolved};
#[allow(dead_code)]
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::start_container;

const GUILD: &str = "111111111111111111";
const UNPAIRED: &str = "999999999999999999";
const USER: &str = "222222222222222222";
const UUID: &str = "11111111-2222-4333-8444-555555555555";

/// Only `GUILD` is paired (tenant 7, community 9); everything else is unpaired.
struct Pairing;
impl ScopeLookup for Pairing {
    fn lookup<'a>(
        &'a self,
        platform: &'a str,
        source: &'a str,
    ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
        Box::pin(async move {
            assert_eq!(platform, "discord");
            if source == GUILD {
                Ok(ResolvedScope::validated(7, 9, "acme", Some("main")).unwrap())
            } else {
                Err(Unresolved::NotFound)
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
            assert_eq!((scope.tenant_id, scope.community_id), (7, 9));
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

#[tokio::test]
async fn paired_guild_forwards_one_signed_event_and_unpaired_is_ignored() {
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
    let mut conn = client.get_connection_manager().await.unwrap();
    let keys = PinKeys::parse("k1:00112233445566778899aabbccddeeff", Some("k1")).unwrap();
    let tap = UnbanTap::new(
        Arc::new(Pairing),
        Arc::new(Tok),
        Arc::new(ValkeySink::new(conn.clone())),
        Some(keys.clone()),
    );

    let ignored_before = platform_unban::count("ignored", "unpaired_guild");
    tap.on_frame("discord", &frame(UNPAIRED)).await;
    assert_eq!(
        platform_unban::count("ignored", "unpaired_guild"),
        ignored_before + 1
    );
    let len: i64 = redis::cmd("LLEN")
        .arg(EVENTS_LIST)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(len, 0, "an unpaired guild must put nothing on the list");

    tap.on_frame("discord", &frame(GUILD)).await;
    let entries: Vec<String> = redis::cmd("LRANGE")
        .arg(EVENTS_LIST)
        .arg(0)
        .arg(-1)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(entries.len(), 1);
    assert!(!entries[0].contains(USER), "raw discord id leaked");
    let v: serde_json::Value = serde_json::from_str(&entries[0]).unwrap();
    assert_eq!(
        (v["tenant_id"].as_i64(), v["community_id"].as_i64()),
        (Some(7), Some(9))
    );
    let fields = platform_unban::mac_fields(
        GUILD,
        UUID,
        v["event_id"].as_str().unwrap(),
        v["exp_ms"].as_u64().unwrap(),
        9,
        7,
    );
    keys.verify(&fields, v["sig"].as_str().unwrap()).unwrap();
}
