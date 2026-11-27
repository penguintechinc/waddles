#![allow(clippy::unwrap_used, clippy::expect_used)]
use super::*;
use crate::floor_ban_dispatch::StoreFuture;
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use ring::signature::{Ed25519KeyPair, KeyPair};
use std::collections::HashSet;
use std::sync::Mutex;

const UUID: &str = "11111111-2222-4333-8444-555555555555";
const EVENT: &str = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const BROADCASTER: &str = "141981764";

/// `(tenant, community, platform, user, source app, observed_ms)` of one lift.
type LiftCall = (i32, i32, String, Uuid, String, i64);
type LiftResult = Result<(i32, i32), String>;

#[derive(Default)]
struct FakeLift {
    calls: Mutex<Vec<LiftCall>>,
    result: Mutex<Option<LiftResult>>,
}
impl UnbanSync for FakeLift {
    fn lift_from_platform<'a>(
        &'a self,
        t: i32,
        c: i32,
        p: &'a str,
        u: Uuid,
        s: &'a str,
        o: i64,
    ) -> StoreFuture<'a, (i32, i32)> {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .push((t, c, p.into(), u, s.into(), o));
            self.result.lock().unwrap().clone().unwrap_or(Ok((1, 0)))
        })
    }
    fn lift_guild_from_platform<'a>(
        &'a self,
        _t: i32,
        _p: &'a str,
        _g: &'a str,
        _u: Uuid,
        _s: &'a str,
        _o: i64,
    ) -> StoreFuture<'a, (i32, i32, i32)> {
        Box::pin(async { Err("guild lift is not used by the twitch consumer".into()) })
    }
}

#[derive(Default)]
struct FakeSeen(Mutex<HashSet<String>>, bool);
impl SeenStore for FakeSeen {
    fn is_seen<'a>(&'a self, id: &'a str) -> StoreFuture<'a, bool> {
        Box::pin(async move {
            if self.1 {
                return Err("down".into());
            }
            Ok(self.0.lock().unwrap().contains(id))
        })
    }

    fn claim<'a>(&'a self, id: &'a str) -> StoreFuture<'a, bool> {
        Box::pin(async move {
            if self.1 {
                return Err("down".into());
            }
            Ok(self.0.lock().unwrap().insert(id.into()))
        })
    }
}

fn b64(bytes: &[u8]) -> String {
    B64.encode(bytes)
}

fn pair(seed: u8) -> Ed25519KeyPair {
    Ed25519KeyPair::from_seed_unchecked(&[seed; 32]).unwrap()
}

fn keys(kp: &Ed25519KeyPair) -> VerifyKeys {
    VerifyKeys::parse(&format!("{{\"k1\":\"{}\"}}", b64(kp.public_key().as_ref()))).unwrap()
}

fn event_with(kp: &Ed25519KeyPair, tenant: i64, community: i64, observed: u64, exp: u64) -> String {
    let msg = signing_bytes(EVENT, tenant, community, 31, UUID, observed, exp);
    let sig = b64(kp.sign(&msg).as_ref());
    serde_json::json!({"v":1,"event_id":EVENT,"tenant_id":tenant,"community_id":community,
        "connection_id":31,"platform":"twitch","user_uuid":UUID,
        "observed_ms":observed,"exp_ms":exp,"kid":"k1","sig":sig})
    .to_string()
}

fn handler(lift: Arc<FakeLift>, seen_down: bool) -> TwitchUnban {
    TwitchUnban::new(
        lift,
        keys(&pair(7)),
        Arc::new(FakeSeen(Mutex::default(), seen_down)),
        crate::stage_metrics::stage_metrics(),
    )
}

#[test]
fn the_domain_label_separates_twitch_from_discord_signatures() {
    let twitch = signing_bytes(EVENT, 7, 9, 31, UUID, 1_000, 2_000);
    assert!(twitch.starts_with(DOMAIN));
    assert!(!twitch.starts_with(crate::platform_unban::DOMAIN));
}

#[tokio::test]
async fn a_verified_event_lifts_in_the_one_signed_community_with_no_echo() {
    let lift = Arc::new(FakeLift::default());
    let out = handler(lift.clone(), false)
        .handle(&event_with(&pair(7), 7, 9, 1_500, 2_000), 1_000)
        .await;
    assert_eq!(
        out,
        Outcome::Lifted {
            communities: 1,
            lifted: 1,
            soft: 0
        }
    );
    assert_eq!(
        lift.calls.lock().unwrap().as_slice(),
        &[(
            7,
            9,
            "twitch".into(),
            Uuid::parse_str(UUID).unwrap(),
            SOURCE_APP.into(),
            1_500
        )],
        "tenant, community and observed time are the signed ones; only a lift is recorded"
    );
}

#[tokio::test]
async fn nothing_in_force_is_a_counted_no_op() {
    let lift = Arc::new(FakeLift::default());
    *lift.result.lock().unwrap() = Some(Ok((0, 0)));
    let out = handler(lift, false)
        .handle(&event_with(&pair(7), 7, 9, 1_500, 2_000), 1_000)
        .await;
    assert_eq!(out, Outcome::NoBan);
}

#[tokio::test]
async fn forged_tampered_stale_cross_tenant_and_replayed_events_never_reach_the_lift() {
    let lift = Arc::new(FakeLift::default());
    let h = handler(lift.clone(), false);
    assert_eq!(
        h.handle(&event_with(&pair(9), 7, 9, 1_500, 2_000), 1_000)
            .await,
        Outcome::Rejected("bad_signature"),
        "signed by a key this stage does not hold"
    );
    let good = event_with(&pair(7), 7, 9, 1_500, 2_000);
    for (from, to) in [
        ("\"tenant_id\":7", "\"tenant_id\":8"),
        ("\"community_id\":9", "\"community_id\":10"),
        ("\"connection_id\":31", "\"connection_id\":32"),
        ("\"observed_ms\":1500", "\"observed_ms\":1900"),
    ] {
        assert_eq!(
            h.handle(&good.replace(from, to), 1_000).await,
            Outcome::Rejected("bad_signature"),
            "{from}"
        );
    }
    assert_eq!(
        h.handle(&event_with(&pair(7), 7, 9, 100, 500), 1_000).await,
        Outcome::Rejected("expired")
    );
    assert_eq!(
        h.handle(
            &event_with(&pair(7), 7, 9, 100, 1_000 + MAX_AHEAD_MS + 1),
            1_000
        )
        .await,
        Outcome::Rejected("expired")
    );
    assert_eq!(h.handle("{}", 1_000).await, Outcome::Rejected("malformed"));
    assert_eq!(
        h.handle(&"x".repeat(MAX_ENTRY_BYTES + 1), 1_000).await,
        Outcome::Rejected("oversize")
    );
    assert_eq!(
        h.handle(&event_with(&pair(7), 7, 0, 1_500, 2_000), 1_000)
            .await,
        Outcome::Rejected("malformed")
    );
    assert!(lift.calls.lock().unwrap().is_empty());
    assert!(matches!(
        h.handle(&good, 1_000).await,
        Outcome::Lifted { .. }
    ));
    assert_eq!(h.handle(&good, 1_000).await, Outcome::Rejected("replayed"));
    assert_eq!(lift.calls.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn a_discord_signed_event_does_not_verify_as_a_twitch_event() {
    // Same key, Discord's domain and field layout: the Twitch stage must refuse it.
    let kp = pair(7);
    let msg = crate::platform_unban::signing_bytes(
        EVENT,
        7,
        31,
        "twitch",
        BROADCASTER,
        UUID,
        1_500,
        2_000,
    );
    let sig = b64(kp.sign(&msg).as_ref());
    let raw = serde_json::json!({"v":1,"event_id":EVENT,"tenant_id":7,"community_id":9,
        "connection_id":31,"platform":"twitch","user_uuid":UUID,
        "observed_ms":1_500,"exp_ms":2_000,"kid":"k1","sig":sig})
    .to_string();
    let lift = Arc::new(FakeLift::default());
    assert_eq!(
        handler(lift.clone(), false).handle(&raw, 1_000).await,
        Outcome::Rejected("bad_signature")
    );
    assert!(lift.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn the_single_use_store_failing_closed_and_a_lift_error_are_errors() {
    let lift = Arc::new(FakeLift::default());
    assert_eq!(
        handler(lift.clone(), true)
            .handle(&event_with(&pair(7), 7, 9, 1_500, 2_000), 1_000)
            .await,
        Outcome::Error
    );
    assert!(lift.calls.lock().unwrap().is_empty());
    *lift.result.lock().unwrap() = Some(Err("db".into()));
    assert_eq!(
        handler(lift, false)
            .handle(&event_with(&pair(7), 7, 9, 1_500, 2_000), 1_000)
            .await,
        Outcome::Error
    );
}
