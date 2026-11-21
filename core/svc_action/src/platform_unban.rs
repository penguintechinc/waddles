//! Platform-originated unban consumer: lifts the Waddles floor ban when a moderator unbans the
//! user in Discord's own UI.
//!
//! svc-ingest turns its gateway `GUILD_BAN_REMOVE` frame into a signed event on
//! [`EVENTS_LIST`] (`svc_ingest::platform_unban`); this stage verifies it and calls the same
//! SECURITY DEFINER function a trusted `mod.unban` uses
//! (`reputation_floor_ban_lift_from_platform`, granted to this service's role only). Data-plane
//! decoupling: ingest never touches the database and this stage never touches the gateway; they
//! share only the list and the MAC key.
//!
//! ```text
//!  svc-ingest --LPUSH signed event--> waddles:platform-unban:discord:events
//!                                          | BRPOP (this consumer)
//!                  verify MAC -> freshness -> single-use -> lift_from_platform(source = fixed id)
//! ```
//!
//! **Trust.** An entry is honoured only when its MAC verifies under the DEDICATED-label
//! `OUTBOUND_PIN_KEYS` ring (mounted only into svc-ingest and svc-action: never the executor, so
//! no bundle can write a believable entry) and it is fresh and unseen. The tenant and community
//! are MAC-bound values ingest took from hub-api's guild pairing, never anything a bundle
//! supplies. The lift is recorded under the fixed identity [`SOURCE_APP`] and runs the SQL's own
//! guards: community-scope bans only (a tenant-scope ban is NOT lifted), community must belong to
//! the tenant, idempotent. **No echo**: `lift_from_platform` never enqueues an unban, so a
//! platform-originated lift cannot loop back to the platform. Logs and metrics carry numeric ids
//! and fixed reason codes only.

use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use uuid::Uuid;

use crate::floor_ban_dispatch::{StoreFuture, UnbanSync};
use crate::pin_sig::{PinFields, PinKeys};
use crate::stage_metrics::StageMetrics;

/// Valkey list svc-ingest pushes verified events onto.
pub const EVENTS_LIST: &str = "waddles:platform-unban:discord:events";
/// Prefix of the single-use marker of a handled event id.
pub const SEEN_PREFIX: &str = "waddles:platform-unban:discord:seen:";
/// Fixed `p_source_app` recorded on a gateway-originated lift (never a bundle id).
pub const SOURCE_APP: &str = "system:discord-gateway";
/// MAC domain-separation label (`platform` slot); must equal ingest's.
pub const MAC_PLATFORM: &str = "platform-unban";
/// MAC `op`; must equal ingest's.
pub const MAC_OP: &str = "ban.removed";
/// MAC `origin_channel`; must equal ingest's.
pub const MAC_NO_ORIGIN: &str = "-";
/// The only platform this consumer serves.
pub const PLATFORM: &str = "discord";
/// Largest accepted entry, bytes.
const MAX_ENTRY_BYTES: usize = 4 * 1024;
/// An event's deadline may be at most this far ahead (ingest's TTL plus clock skew).
const MAX_AHEAD_MS: u64 = 6 * 60_000;
/// How long a handled event id stays claimed (>= the maximum event lifetime).
const SEEN_TTL_SECS: u64 = 900;
/// Blocking-pop timeout, seconds (bounds shutdown latency).
const POP_TIMEOUT_SECS: u64 = 5;

/// One wire event as svc-ingest writes it.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Event {
    v: u32,
    event_id: String,
    tenant_id: i64,
    community_id: i64,
    platform: String,
    guild_id: String,
    user_uuid: String,
    exp_ms: u64,
    sig: String,
}

/// What handling one entry came to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// A floor ban (and/or soft ban) was lifted.
    Lifted {
        /// Floor bans lifted.
        lifted: i32,
        /// Soft bans cleared.
        soft: i32,
    },
    /// Valid event, nothing in force to lift (no ban, or a tenant-scope ban only).
    NoBan,
    /// Refused before touching the database, with a fixed reason.
    Rejected(&'static str),
    /// The lift itself failed (database).
    Error,
}

/// Single-use marker store.
pub trait SeenStore: Send + Sync {
    /// `Ok(true)` on the first claim of `event_id`, `Ok(false)` on a replay.
    fn claim<'a>(&'a self, event_id: &'a str) -> StoreFuture<'a, bool>;
}

/// Valkey `SET NX EX` marker store.
pub struct ValkeySeen {
    conn: redis::aio::ConnectionManager,
}

impl ValkeySeen {
    /// Wraps a connected manager.
    #[must_use]
    pub fn new(conn: redis::aio::ConnectionManager) -> Self {
        Self { conn }
    }
}

impl SeenStore for ValkeySeen {
    fn claim<'a>(&'a self, event_id: &'a str) -> StoreFuture<'a, bool> {
        Box::pin(async move {
            let mut conn = self.conn.clone();
            let set: Option<String> = redis::cmd("SET")
                .arg(format!("{SEEN_PREFIX}{event_id}"))
                .arg(1)
                .arg("NX")
                .arg("EX")
                .arg(SEEN_TTL_SECS)
                .query_async(&mut conn)
                .await
                .map_err(|e| format!("{:?}", e.kind()))?;
            Ok(set.is_some())
        })
    }
}

/// Verifies and applies platform-originated unban events.
pub struct PlatformUnban {
    lift: Arc<dyn UnbanSync>,
    keys: PinKeys,
    seen: Arc<dyn SeenStore>,
    metrics: Arc<StageMetrics>,
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

impl PlatformUnban {
    /// Builds the handler over a lift backend, the verify keys and a single-use store.
    #[must_use]
    pub fn new(
        lift: Arc<dyn UnbanSync>,
        keys: PinKeys,
        seen: Arc<dyn SeenStore>,
        metrics: Arc<StageMetrics>,
    ) -> Self {
        Self {
            lift,
            keys,
            seen,
            metrics,
        }
    }

    fn rejected(&self, label: &'static str, reason: &'static str) -> Outcome {
        self.metrics.record_floor_ban_dispatch(label);
        tracing::warn!(reason, "platform unban event rejected");
        Outcome::Rejected(reason)
    }

    /// Handles one raw list entry at `now` (epoch ms).
    pub async fn handle(&self, raw: &str, now: u64) -> Outcome {
        if raw.len() > MAX_ENTRY_BYTES {
            return self.rejected("gateway_unban_malformed", "oversize");
        }
        let Ok(ev) = serde_json::from_str::<Event>(raw) else {
            return self.rejected("gateway_unban_malformed", "malformed");
        };
        let (Ok(tenant), Ok(community)) =
            (i32::try_from(ev.tenant_id), i32::try_from(ev.community_id))
        else {
            return self.rejected("gateway_unban_malformed", "bad_scope");
        };
        if ev.v != 1
            || ev.platform != PLATFORM
            || tenant < 1
            || community < 1
            || Uuid::parse_str(&ev.event_id).is_err()
            || ev.guild_id.is_empty()
            || ev.guild_id.len() > 20
        {
            return self.rejected("gateway_unban_malformed", "malformed");
        }
        let Ok(user) = Uuid::parse_str(&ev.user_uuid) else {
            return self.rejected("gateway_unban_malformed", "bad_user");
        };
        let fields = PinFields {
            platform: MAC_PLATFORM,
            op: MAC_OP,
            channel: &ev.guild_id,
            origin_channel: MAC_NO_ORIGIN,
            message_id: "",
            text: "",
            user_id: &ev.user_uuid,
            op_id: &ev.event_id,
            exp_ms: ev.exp_ms,
            community_id: ev.community_id,
            tenant_id: ev.tenant_id,
        };
        if self.keys.verify(&fields, &ev.sig).is_err() {
            return self.rejected("gateway_unban_bad_signature", "bad_signature");
        }
        if ev.exp_ms < now || ev.exp_ms > now.saturating_add(MAX_AHEAD_MS) {
            return self.rejected("gateway_unban_expired", "expired");
        }
        match self.seen.claim(&ev.event_id).await {
            Ok(true) => {}
            Ok(false) => return self.rejected("gateway_unban_replayed", "replayed"),
            Err(_) => {
                // Fail closed: without the single-use marker a replay could lift a fresh ban.
                self.metrics
                    .record_floor_ban_dispatch("gateway_unban_error");
                tracing::error!(
                    tenant_id = tenant,
                    community_id = community,
                    "platform unban: single-use store unavailable; event dropped, ban stays"
                );
                return Outcome::Error;
            }
        }
        match self
            .lift
            .lift_from_platform(tenant, community, PLATFORM, user, SOURCE_APP)
            .await
        {
            Ok((lifted, soft)) if lifted + soft > 0 => {
                self.metrics
                    .record_floor_ban_dispatch("gateway_unban_lifted");
                tracing::info!(
                    tenant_id = tenant,
                    community_id = community,
                    lifted,
                    soft_cleared = soft,
                    "discord-native unban lifted the Waddles ban (no echo)"
                );
                Outcome::Lifted { lifted, soft }
            }
            Ok(_) => {
                self.metrics
                    .record_floor_ban_dispatch("gateway_unban_no_ban");
                tracing::info!(
                    tenant_id = tenant,
                    community_id = community,
                    "discord-native unban: no community-scope Waddles ban in force"
                );
                Outcome::NoBan
            }
            Err(e) => {
                self.metrics
                    .record_floor_ban_dispatch("gateway_unban_error");
                tracing::error!(
                    tenant_id = tenant,
                    community_id = community,
                    error = %e,
                    "ALERT discord-native unban: the Waddles floor ban could NOT be lifted"
                );
                Outcome::Error
            }
        }
    }
}

/// Pops [`EVENTS_LIST`] until `shutdown`, handing each entry to `handler`.
pub async fn run_consumer<S>(
    handler: Arc<PlatformUnban>,
    mut pop_conn: redis::aio::ConnectionManager,
    shutdown: S,
) where
    S: std::future::Future<Output = ()>,
{
    tokio::pin!(shutdown);
    let mut failures: u32 = 0;
    loop {
        let mut pop = redis::cmd("BRPOP");
        pop.arg(EVENTS_LIST).arg(POP_TIMEOUT_SECS);
        let popped = tokio::select! {
            () = &mut shutdown => return,
            r = pop.query_async::<Option<(String, String)>>(&mut pop_conn) => r,
        };
        match popped {
            Ok(Some((_key, raw))) => {
                failures = 0;
                handler.handle(&raw, now_ms()).await;
            }
            Ok(None) => failures = 0,
            Err(e) => {
                failures = failures.saturating_add(1);
                let delay = Duration::from_millis(500u64.saturating_mul(1 << failures.min(6)));
                tracing::error!(error = %e, "platform-unban list read failed");
                tokio::select! {
                    () = &mut shutdown => return,
                    () = tokio::time::sleep(delay) => {}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use std::collections::HashSet;
    use std::sync::Mutex;

    const UUID: &str = "11111111-2222-4333-8444-555555555555";
    const EVENT: &str = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

    type LiftCall = (i32, i32, String, Uuid, String);

    #[derive(Default)]
    struct FakeLift {
        calls: Mutex<Vec<LiftCall>>,
        result: Mutex<Option<Result<(i32, i32), String>>>,
    }
    impl UnbanSync for FakeLift {
        fn lift_from_platform<'a>(
            &'a self,
            t: i32,
            c: i32,
            p: &'a str,
            u: Uuid,
            s: &'a str,
        ) -> StoreFuture<'a, (i32, i32)> {
            Box::pin(async move {
                self.calls
                    .lock()
                    .unwrap()
                    .push((t, c, p.into(), u, s.into()));
                self.result.lock().unwrap().clone().unwrap_or(Ok((1, 0)))
            })
        }
    }
    #[derive(Default)]
    struct FakeSeen(Mutex<HashSet<String>>, bool);
    impl SeenStore for FakeSeen {
        fn claim<'a>(&'a self, id: &'a str) -> StoreFuture<'a, bool> {
            Box::pin(async move {
                if self.1 {
                    return Err("down".into());
                }
                Ok(self.0.lock().unwrap().insert(id.into()))
            })
        }
    }

    fn keys() -> PinKeys {
        PinKeys::parse("k1:00112233445566778899aabbccddeeff", Some("k1")).unwrap()
    }

    fn event(keys: &PinKeys, exp: u64, tenant: i64, community: i64) -> String {
        let f = PinFields {
            platform: MAC_PLATFORM,
            op: MAC_OP,
            channel: "111111111111111111",
            origin_channel: MAC_NO_ORIGIN,
            message_id: "",
            text: "",
            user_id: UUID,
            op_id: EVENT,
            exp_ms: exp,
            community_id: community,
            tenant_id: tenant,
        };
        let sig = keys.sign(&f).unwrap();
        serde_json::json!({"v":1,"event_id":EVENT,"tenant_id":tenant,"community_id":community,
            "platform":"discord","guild_id":"111111111111111111","user_uuid":UUID,
            "exp_ms":exp,"sig":sig})
        .to_string()
    }

    fn handler(lift: Arc<FakeLift>, seen_down: bool) -> PlatformUnban {
        PlatformUnban::new(
            lift,
            keys(),
            Arc::new(FakeSeen(Mutex::default(), seen_down)),
            crate::stage_metrics::stage_metrics(),
        )
    }

    #[tokio::test]
    async fn a_verified_event_lifts_under_the_fixed_gateway_identity() {
        let lift = Arc::new(FakeLift::default());
        let h = handler(lift.clone(), false);
        let out = h.handle(&event(&keys(), 2_000, 7, 9), 1_000).await;
        assert_eq!(out, Outcome::Lifted { lifted: 1, soft: 0 });
        let calls = lift.calls.lock().unwrap();
        assert_eq!(
            calls.as_slice(),
            &[(
                7,
                9,
                "discord".into(),
                Uuid::parse_str(UUID).unwrap(),
                SOURCE_APP.into()
            )]
        );
    }

    #[tokio::test]
    async fn nothing_in_force_is_a_counted_no_op() {
        let lift = Arc::new(FakeLift::default());
        *lift.result.lock().unwrap() = Some(Ok((0, 0)));
        let out = handler(lift, false)
            .handle(&event(&keys(), 2_000, 7, 9), 1_000)
            .await;
        assert_eq!(out, Outcome::NoBan);
    }

    #[tokio::test]
    async fn forged_tampered_stale_and_replayed_events_never_reach_the_lift() {
        let lift = Arc::new(FakeLift::default());
        let h = handler(lift.clone(), false);
        // Signed by a different key (a writer without the ring).
        let other = PinKeys::parse("k1:ffeeddccbbaa99887766554433221100", Some("k1")).unwrap();
        assert_eq!(
            h.handle(&event(&other, 2_000, 7, 9), 1_000).await,
            Outcome::Rejected("bad_signature")
        );
        // Tenant/community tampered after signing.
        let tampered =
            event(&keys(), 2_000, 7, 9).replace("\"community_id\":9", "\"community_id\":10");
        assert_eq!(
            h.handle(&tampered, 1_000).await,
            Outcome::Rejected("bad_signature")
        );
        // Expired / too far ahead.
        assert_eq!(
            h.handle(&event(&keys(), 500, 7, 9), 1_000).await,
            Outcome::Rejected("expired")
        );
        assert_eq!(
            h.handle(&event(&keys(), 1_000 + MAX_AHEAD_MS + 1, 7, 9), 1_000)
                .await,
            Outcome::Rejected("expired")
        );
        // Malformed / unknown field / oversize.
        assert_eq!(h.handle("{}", 1_000).await, Outcome::Rejected("malformed"));
        assert_eq!(
            h.handle(&"x".repeat(MAX_ENTRY_BYTES + 1), 1_000).await,
            Outcome::Rejected("oversize")
        );
        assert!(lift.calls.lock().unwrap().is_empty());
        // First delivery lifts, the replay does not.
        let good = event(&keys(), 2_000, 7, 9);
        assert!(matches!(
            h.handle(&good, 1_000).await,
            Outcome::Lifted { .. }
        ));
        assert_eq!(h.handle(&good, 1_000).await, Outcome::Rejected("replayed"));
        assert_eq!(lift.calls.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn an_outbound_style_mac_cannot_stand_in_for_an_unban_event() {
        // Same fields but a real outbound platform label: must not verify as an unban.
        let f = PinFields {
            platform: "discord",
            op: "chat.send",
            channel: "111111111111111111",
            origin_channel: "111111111111111111",
            message_id: "",
            text: "hi",
            user_id: "",
            op_id: EVENT,
            exp_ms: 2_000,
            community_id: 9,
            tenant_id: 7,
        };
        let sig = keys().sign(&f).unwrap();
        let raw = serde_json::json!({"v":1,"event_id":EVENT,"tenant_id":7,"community_id":9,
            "platform":"discord","guild_id":"111111111111111111","user_uuid":UUID,
            "exp_ms":2000,"sig":sig})
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
                .handle(&event(&keys(), 2_000, 7, 9), 1_000)
                .await,
            Outcome::Error
        );
        assert!(lift.calls.lock().unwrap().is_empty());
        *lift.result.lock().unwrap() = Some(Err("db".into()));
        assert_eq!(
            handler(lift, false)
                .handle(&event(&keys(), 2_000, 7, 9), 1_000)
                .await,
            Outcome::Error
        );
    }
}
