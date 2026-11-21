//! Twitch-originated unban consumer: lifts the Waddles floor ban when a channel moderator unbans
//! the user in Twitch itself (EventSub `channel.unban`).
//!
//! svc-ingest verifies the EventSub delivery against the receiving subscription's own ledger
//! secret, binds it to the subscription's tenant/community/connection, re-reads Twitch's live ban
//! state and signs an event onto [`EVENTS_LIST`] (`svc_ingest::twitch_unban`). This stage verifies
//! that signature, enforces freshness and single use, and lifts in the ONE community the signed
//! event names, through the same SECURITY DEFINER function as the Discord path. It never
//! touches Twitch and ingest never touches the database.
//!
//! **Trust.** Same dedicated Ed25519 pair as the Discord path (this stage holds PUBLIC keys only)
//! under a Twitch-specific domain label ([`DOMAIN`]) so neither platform's signature can stand in
//! for the other's. The signature covers the event id, tenant, community, connection, broadcaster,
//! tokenized user, the time the unban was OBSERVED (EventSub `message_timestamp`) and the expiry.
//! The lift is recorded under [`SOURCE_APP`], only lifts bans armed at or before the observed time,
//! and is idempotent. **No echo**: the lift never enqueues a platform unban. Logs and metrics carry
//! numeric ids and fixed reason codes only.

use std::sync::Arc;

use serde::Deserialize;
use uuid::Uuid;

use crate::floor_ban_dispatch::UnbanSync;
use crate::platform_unban::{
    now_ms, Outcome, SeenStore, VerifyKeys, MAX_AHEAD_MS, MAX_ENTRY_BYTES,
};
use crate::stage_metrics::StageMetrics;

/// Valkey list svc-ingest pushes verified Twitch events onto.
pub const EVENTS_LIST: &str = "waddles:platform-unban:twitch:events";
/// Prefix of the single-use marker of a handled event id.
pub const SEEN_PREFIX: &str = "waddles:platform-unban:twitch:seen:";
/// Fixed `p_source_app` recorded on an EventSub-originated lift (never a bundle id).
pub const SOURCE_APP: &str = "system:twitch-eventsub";
/// Signature domain-separation label; must equal ingest's.
pub const DOMAIN: &[u8] = b"waddles.platform-unban.twitch.v1";
/// The only platform this consumer serves.
pub const PLATFORM: &str = "twitch";
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
    connection_id: i64,
    platform: String,
    broadcaster_id: String,
    user_uuid: String,
    observed_ms: u64,
    exp_ms: u64,
    kid: String,
    sig: String,
}

/// The exact bytes ingest signs: the domain label, then nine length-prefixed (u32 BE) parts;
/// numbers as decimal text. Must equal ingest's `twitch_unban::signing_bytes`.
#[must_use]
#[allow(clippy::too_many_arguments)] // the signed field set is the contract; one param per field
pub fn signing_bytes(
    event_id: &str,
    tenant_id: i64,
    community_id: i64,
    connection_id: i64,
    broadcaster_id: &str,
    user_uuid: &str,
    observed_ms: u64,
    exp_ms: u64,
) -> Vec<u8> {
    let nums = [
        tenant_id.to_string(),
        community_id.to_string(),
        connection_id.to_string(),
        observed_ms.to_string(),
        exp_ms.to_string(),
    ];
    let parts: [&str; 9] = [
        event_id,
        &nums[0],
        &nums[1],
        &nums[2],
        broadcaster_id,
        user_uuid,
        &nums[3],
        &nums[4],
        PLATFORM,
    ];
    let mut out = DOMAIN.to_vec();
    for part in parts {
        out.extend_from_slice(&u32::try_from(part.len()).unwrap_or(u32::MAX).to_be_bytes());
        out.extend_from_slice(part.as_bytes());
    }
    out
}

/// Verifies and applies Twitch-originated unban events.
pub struct TwitchUnban {
    lift: Arc<dyn UnbanSync>,
    keys: VerifyKeys,
    seen: Arc<dyn SeenStore>,
    metrics: Arc<StageMetrics>,
}

impl TwitchUnban {
    /// Builds the handler over a lift backend, the PUBLIC verify keys and a single-use store.
    #[must_use]
    pub fn new(
        lift: Arc<dyn UnbanSync>,
        keys: VerifyKeys,
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
        tracing::warn!(reason, "twitch unban event rejected");
        Outcome::Rejected(reason)
    }

    /// Handles one raw list entry at `now` (epoch ms).
    pub async fn handle(&self, raw: &str, now: u64) -> Outcome {
        if raw.len() > MAX_ENTRY_BYTES {
            return self.rejected("eventsub_unban_malformed", "oversize");
        }
        let Ok(ev) = serde_json::from_str::<Event>(raw) else {
            return self.rejected("eventsub_unban_malformed", "malformed");
        };
        let (Ok(tenant), Ok(community), Ok(observed)) = (
            i32::try_from(ev.tenant_id),
            i32::try_from(ev.community_id),
            i64::try_from(ev.observed_ms),
        ) else {
            return self.rejected("eventsub_unban_malformed", "bad_scope");
        };
        if ev.v != 1
            || ev.platform != PLATFORM
            || tenant < 1
            || community < 1
            || ev.connection_id < 1
            || observed < 1
            || Uuid::parse_str(&ev.event_id).is_err()
            || ev.broadcaster_id.is_empty()
            || ev.broadcaster_id.len() > 20
            || !ev.broadcaster_id.bytes().all(|b| b.is_ascii_digit())
        {
            return self.rejected("eventsub_unban_malformed", "malformed");
        }
        let Ok(user) = Uuid::parse_str(&ev.user_uuid) else {
            return self.rejected("eventsub_unban_malformed", "bad_user");
        };
        let msg = signing_bytes(
            &ev.event_id,
            ev.tenant_id,
            ev.community_id,
            ev.connection_id,
            &ev.broadcaster_id,
            &ev.user_uuid,
            ev.observed_ms,
            ev.exp_ms,
        );
        if !self.keys.verify(&ev.kid, &msg, &ev.sig) {
            return self.rejected("eventsub_unban_bad_signature", "bad_signature");
        }
        if ev.exp_ms < now || ev.exp_ms > now.saturating_add(MAX_AHEAD_MS) {
            return self.rejected("eventsub_unban_expired", "expired");
        }
        match self.seen.claim(&ev.event_id).await {
            Ok(true) => {}
            Ok(false) => return self.rejected("eventsub_unban_replayed", "replayed"),
            Err(_) => {
                // Fail closed: without the single-use marker a replay could lift a fresh ban.
                self.metrics
                    .record_floor_ban_dispatch("eventsub_unban_error");
                tracing::error!(
                    tenant_id = tenant,
                    "twitch unban: single-use store unavailable; event dropped, ban stays"
                );
                return Outcome::Error;
            }
        }
        match self
            .lift
            .lift_from_platform(tenant, community, PLATFORM, user, SOURCE_APP, observed)
            .await
        {
            Ok((lifted, soft)) if lifted + soft > 0 => {
                self.metrics
                    .record_floor_ban_dispatch("eventsub_unban_lifted");
                tracing::info!(
                    tenant_id = tenant,
                    community_id = community,
                    connection_id = ev.connection_id,
                    lifted,
                    soft_cleared = soft,
                    "twitch-native unban lifted the Waddles ban (no echo)"
                );
                Outcome::Lifted {
                    communities: 1,
                    lifted,
                    soft,
                }
            }
            Ok(_) => {
                self.metrics
                    .record_floor_ban_dispatch("eventsub_unban_no_ban");
                tracing::info!(
                    tenant_id = tenant,
                    community_id = community,
                    "twitch-native unban: no community-scope Waddles ban in force to lift"
                );
                Outcome::NoBan
            }
            Err(e) => {
                self.metrics
                    .record_floor_ban_dispatch("eventsub_unban_error");
                tracing::error!(
                    tenant_id = tenant,
                    community_id = community,
                    error = %e,
                    "ALERT twitch-native unban: the Waddles floor ban could NOT be lifted"
                );
                Outcome::Error
            }
        }
    }
}

/// Pops [`EVENTS_LIST`] until `shutdown`, handing each entry to `handler`.
pub async fn run_consumer<S>(
    handler: Arc<TwitchUnban>,
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
                let delay =
                    std::time::Duration::from_millis(500u64.saturating_mul(1 << failures.min(6)));
                tracing::error!(error = %e, "twitch-unban list read failed");
                tokio::select! {
                    () = &mut shutdown => return,
                    () = tokio::time::sleep(delay) => {}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests;
