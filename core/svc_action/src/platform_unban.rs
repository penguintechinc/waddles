//! Platform-originated unban consumer: lifts the Waddles floor ban when a moderator unbans the
//! user in Discord's own UI.
//!
//! svc-ingest turns its gateway `GUILD_BAN_REMOVE` frame into a signed event on [`EVENTS_LIST`]
//! (`svc_ingest::platform_unban`); this stage verifies it and lifts through the SECURITY DEFINER
//! function granted to this service's role only. Data-plane decoupling: ingest never touches the
//! database and this stage never touches the gateway; they share only the list and the key pair.
//!
//! ```text
//!  svc-ingest --LPUSH signed event--> waddles:platform-unban:discord:events
//!                                          | BRPOP (this consumer)
//!                  verify Ed25519 -> freshness -> single-use ->
//!                  lift in EVERY community of the tenant linked to the guild (observed-time bound)
//! ```
//!
//! **Trust.** The event is Ed25519-signed under a DEDICATED key pair: svc-ingest holds the private
//! half, this stage holds PUBLIC keys only (`PLATFORM_UNBAN_PUBLIC_KEYS`), so it cannot mint an
//! unban event, and the executor (hence no bundle) has neither Valkey access nor any key. The
//! signature covers the event id, tenant, the receiving gateway `connection_id`, guild, tokenized
//! user, the time the unban was OBSERVED and the expiry. The tenant is the one ingest bound to the
//! receiving connection (never a payload field a frame could name); expansion from guild to
//! communities happens in SQL under that tenant and never crosses tenants. The lift is recorded
//! under the fixed identity [`SOURCE_APP`] and carries the SQL's guards: community-scope bans only,
//! only bans armed at or before the observed time (a stale event never lifts a newer re-ban),
//! idempotent. **No echo**: the lift never enqueues an unban. Logs and metrics carry numeric ids
//! and fixed reason codes only.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use ring::signature::{UnparsedPublicKey, ED25519};
use serde::Deserialize;
use uuid::Uuid;

use crate::floor_ban_dispatch::{StoreFuture, UnbanSync};
use crate::stage_metrics::StageMetrics;

/// Valkey list svc-ingest pushes verified events onto.
pub const EVENTS_LIST: &str = "waddles:platform-unban:discord:events";
/// Prefix of the single-use marker of a handled event id.
pub const SEEN_PREFIX: &str = "waddles:platform-unban:discord:seen:";
/// Fixed `p_source_app` recorded on a gateway-originated lift (never a bundle id).
pub const SOURCE_APP: &str = "system:discord-gateway";
/// Signature domain-separation label; must equal ingest's.
pub const DOMAIN: &[u8] = b"waddles.platform-unban.v1";
/// The only platform this consumer serves.
pub const PLATFORM: &str = "discord";
/// Largest accepted entry, bytes.
pub(crate) const MAX_ENTRY_BYTES: usize = 4 * 1024;
/// An event's deadline may be at most this far ahead (ingest's TTL plus clock skew).
pub(crate) const MAX_AHEAD_MS: u64 = 6 * 60_000;
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
    connection_id: i64,
    platform: String,
    guild_id: String,
    user_uuid: String,
    observed_ms: u64,
    exp_ms: u64,
    kid: String,
    sig: String,
}

/// The exact bytes ingest signs: the domain label, then each field length-prefixed (u32 BE) so
/// boundaries can never shift; numbers as decimal text. Must equal ingest's `signing_bytes`.
#[must_use]
#[allow(clippy::too_many_arguments)] // the signed field set is the contract; one param per field
pub fn signing_bytes(
    event_id: &str,
    tenant_id: i64,
    connection_id: i64,
    platform: &str,
    guild_id: &str,
    user_uuid: &str,
    observed_ms: u64,
    exp_ms: u64,
) -> Vec<u8> {
    let nums = [
        tenant_id.to_string(),
        connection_id.to_string(),
        observed_ms.to_string(),
        exp_ms.to_string(),
    ];
    let parts: [&str; 8] = [
        event_id, &nums[0], &nums[1], platform, guild_id, user_uuid, &nums[2], &nums[3],
    ];
    let mut out = DOMAIN.to_vec();
    for part in parts {
        out.extend_from_slice(&u32::try_from(part.len()).unwrap_or(u32::MAX).to_be_bytes());
        out.extend_from_slice(part.as_bytes());
    }
    out
}

/// Verify-only key ring: `kid` -> Ed25519 public key (32 raw bytes).
#[derive(Clone)]
pub struct VerifyKeys(HashMap<String, Vec<u8>>);

impl std::fmt::Debug for VerifyKeys {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("VerifyKeys")
            .field("kids", &self.0.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl VerifyKeys {
    /// Parses `{"<kid>":"<base64 32-byte public key>"}` (the `PLATFORM_UNBAN_PUBLIC_KEYS` shape).
    ///
    /// # Errors
    /// A fixed reason for malformed JSON, an empty ring or a key that is not 32 bytes.
    pub fn parse(json: &str) -> Result<Self, &'static str> {
        let raw: HashMap<String, String> =
            serde_json::from_str(json).map_err(|_| "malformed_key_json")?;
        if raw.is_empty() {
            return Err("no_keys");
        }
        let mut keys = HashMap::new();
        for (kid, b64) in raw {
            let bytes = B64.decode(b64.trim()).map_err(|_| "malformed_key")?;
            if kid.is_empty() || bytes.len() != 32 {
                return Err("malformed_key");
            }
            keys.insert(kid, bytes);
        }
        Ok(Self(keys))
    }

    pub(crate) fn verify(&self, kid: &str, msg: &[u8], sig_b64: &str) -> bool {
        let (Some(key), Ok(sig)) = (self.0.get(kid), B64.decode(sig_b64)) else {
            return false;
        };
        UnparsedPublicKey::new(&ED25519, key)
            .verify(msg, &sig)
            .is_ok()
    }
}

/// What handling one entry came to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// At least one floor ban (and/or soft ban) was lifted.
    Lifted {
        /// Communities visited.
        communities: i32,
        /// Floor bans lifted.
        lifted: i32,
        /// Soft bans cleared.
        soft: i32,
    },
    /// Valid event, nothing in force to lift (no ban, a newer ban, a tenant-scope ban only, or no
    /// community of the tenant linked to the guild).
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
    prefix: &'static str,
}

impl ValkeySeen {
    /// Wraps a connected manager.
    #[must_use]
    pub fn new(conn: redis::aio::ConnectionManager) -> Self {
        Self::with_prefix(conn, SEEN_PREFIX)
    }

    /// Wraps a connected manager with a platform-specific marker prefix (so two platforms'
    /// event ids can never collide in the single-use store).
    #[must_use]
    pub fn with_prefix(conn: redis::aio::ConnectionManager, prefix: &'static str) -> Self {
        Self { conn, prefix }
    }
}

impl SeenStore for ValkeySeen {
    fn claim<'a>(&'a self, event_id: &'a str) -> StoreFuture<'a, bool> {
        Box::pin(async move {
            let mut conn = self.conn.clone();
            let set: Option<String> = redis::cmd("SET")
                .arg(format!("{}{event_id}", self.prefix))
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
    keys: VerifyKeys,
    seen: Arc<dyn SeenStore>,
    metrics: Arc<StageMetrics>,
}

pub(crate) fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

impl PlatformUnban {
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
        let Ok(tenant) = i32::try_from(ev.tenant_id) else {
            return self.rejected("gateway_unban_malformed", "bad_scope");
        };
        let Ok(observed) = i64::try_from(ev.observed_ms) else {
            return self.rejected("gateway_unban_malformed", "bad_observed");
        };
        if ev.v != 1
            || ev.platform != PLATFORM
            || tenant < 1
            || ev.connection_id < 1
            || observed < 1
            || Uuid::parse_str(&ev.event_id).is_err()
            || ev.guild_id.is_empty()
            || ev.guild_id.len() > 20
            || !ev.guild_id.bytes().all(|b| b.is_ascii_digit())
        {
            return self.rejected("gateway_unban_malformed", "malformed");
        }
        let Ok(user) = Uuid::parse_str(&ev.user_uuid) else {
            return self.rejected("gateway_unban_malformed", "bad_user");
        };
        let msg = signing_bytes(
            &ev.event_id,
            ev.tenant_id,
            ev.connection_id,
            &ev.platform,
            &ev.guild_id,
            &ev.user_uuid,
            ev.observed_ms,
            ev.exp_ms,
        );
        if !self.keys.verify(&ev.kid, &msg, &ev.sig) {
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
                    "platform unban: single-use store unavailable; event dropped, ban stays"
                );
                return Outcome::Error;
            }
        }
        match self
            .lift
            .lift_guild_from_platform(tenant, PLATFORM, &ev.guild_id, user, SOURCE_APP, observed)
            .await
        {
            Ok((communities, lifted, soft)) if lifted + soft > 0 => {
                self.metrics
                    .record_floor_ban_dispatch("gateway_unban_lifted");
                tracing::info!(
                    tenant_id = tenant,
                    connection_id = ev.connection_id,
                    communities,
                    lifted,
                    soft_cleared = soft,
                    "discord-native unban lifted the Waddles ban (no echo)"
                );
                Outcome::Lifted {
                    communities,
                    lifted,
                    soft,
                }
            }
            Ok((communities, _, _)) => {
                self.metrics
                    .record_floor_ban_dispatch("gateway_unban_no_ban");
                tracing::info!(
                    tenant_id = tenant,
                    communities,
                    "discord-native unban: no community-scope Waddles ban in force to lift"
                );
                Outcome::NoBan
            }
            Err(e) => {
                self.metrics
                    .record_floor_ban_dispatch("gateway_unban_error");
                tracing::error!(
                    tenant_id = tenant,
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
    use ring::signature::{Ed25519KeyPair, KeyPair};
    use std::collections::HashSet;
    use std::sync::Mutex;

    const UUID: &str = "11111111-2222-4333-8444-555555555555";
    const EVENT: &str = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
    const GUILD: &str = "111111111111111111";

    /// `(tenant, guild, user, source app, observed_ms)` of one guild lift.
    type LiftCall = (i32, String, Uuid, String, i64);

    type LiftResult = Result<(i32, i32, i32), String>;

    #[derive(Default)]
    struct FakeLift {
        calls: Mutex<Vec<LiftCall>>,
        result: Mutex<Option<LiftResult>>,
    }
    impl UnbanSync for FakeLift {
        fn lift_from_platform<'a>(
            &'a self,
            _t: i32,
            _c: i32,
            _p: &'a str,
            _u: Uuid,
            _s: &'a str,
            _o: i64,
        ) -> StoreFuture<'a, (i32, i32)> {
            Box::pin(async { Err("per-community lift is not used by this consumer".into()) })
        }
        fn lift_guild_from_platform<'a>(
            &'a self,
            t: i32,
            _p: &'a str,
            g: &'a str,
            u: Uuid,
            s: &'a str,
            o: i64,
        ) -> StoreFuture<'a, (i32, i32, i32)> {
            Box::pin(async move {
                self.calls
                    .lock()
                    .unwrap()
                    .push((t, g.into(), u, s.into(), o));
                self.result.lock().unwrap().clone().unwrap_or(Ok((2, 2, 0)))
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

    fn pair(seed: u8) -> Ed25519KeyPair {
        Ed25519KeyPair::from_seed_unchecked(&[seed; 32]).unwrap()
    }

    fn verify_keys(kp: &Ed25519KeyPair) -> VerifyKeys {
        VerifyKeys::parse(&format!(
            "{{\"k1\":\"{}\"}}",
            B64.encode(kp.public_key().as_ref())
        ))
        .unwrap()
    }

    #[allow(clippy::too_many_arguments)]
    fn event_with(
        kp: &Ed25519KeyPair,
        tenant: i64,
        connection: i64,
        observed: u64,
        exp: u64,
    ) -> String {
        let msg = signing_bytes(
            EVENT, tenant, connection, "discord", GUILD, UUID, observed, exp,
        );
        let sig = B64.encode(kp.sign(&msg).as_ref());
        serde_json::json!({"v":1,"event_id":EVENT,"tenant_id":tenant,"connection_id":connection,
            "platform":"discord","guild_id":GUILD,"user_uuid":UUID,"observed_ms":observed,
            "exp_ms":exp,"kid":"k1","sig":sig})
        .to_string()
    }

    fn handler(lift: Arc<FakeLift>, seen_down: bool) -> PlatformUnban {
        PlatformUnban::new(
            lift,
            verify_keys(&pair(7)),
            Arc::new(FakeSeen(Mutex::default(), seen_down)),
            crate::stage_metrics::stage_metrics(),
        )
    }

    #[test]
    fn the_signing_bytes_have_a_stable_golden_vector_shared_with_ingest() {
        let kp = pair(7);
        let msg = signing_bytes(EVENT, 7, 31, "discord", GUILD, UUID, 1_000, 2_000);
        // Ed25519 is deterministic: this signature must equal ingest's for the same inputs.
        assert_eq!(
            B64.encode(kp.sign(&msg).as_ref()),
            "dvDxEzityNFG2VJA3cWgD7Fe+XXdADdtTeYWQ5ygGkPVhFFvnOGcnrXtQaBclVWN0tVe79+Yc7Xb51+3w+vfDg=="
        );
    }

    #[tokio::test]
    async fn a_verified_event_lifts_guild_wide_under_the_fixed_gateway_identity() {
        let lift = Arc::new(FakeLift::default());
        let h = handler(lift.clone(), false);
        let out = h
            .handle(&event_with(&pair(7), 7, 31, 1_500, 2_000), 1_000)
            .await;
        assert_eq!(
            out,
            Outcome::Lifted {
                communities: 2,
                lifted: 2,
                soft: 0
            }
        );
        assert_eq!(
            lift.calls.lock().unwrap().as_slice(),
            &[(
                7,
                GUILD.into(),
                Uuid::parse_str(UUID).unwrap(),
                SOURCE_APP.into(),
                1_500
            )],
            "tenant comes from the signed event, observed time is the signed one"
        );
    }

    #[tokio::test]
    async fn nothing_in_force_is_a_counted_no_op() {
        let lift = Arc::new(FakeLift::default());
        *lift.result.lock().unwrap() = Some(Ok((2, 0, 0)));
        let out = handler(lift, false)
            .handle(&event_with(&pair(7), 7, 31, 1_500, 2_000), 1_000)
            .await;
        assert_eq!(out, Outcome::NoBan);
    }

    #[tokio::test]
    async fn forged_tampered_stale_and_replayed_events_never_reach_the_lift() {
        let lift = Arc::new(FakeLift::default());
        let h = handler(lift.clone(), false);
        // Signed by a key whose public half this stage does not hold.
        assert_eq!(
            h.handle(&event_with(&pair(9), 7, 31, 1_500, 2_000), 1_000)
                .await,
            Outcome::Rejected("bad_signature")
        );
        // Tenant / connection / observed time tampered after signing.
        let good = event_with(&pair(7), 7, 31, 1_500, 2_000);
        for (from, to) in [
            ("\"tenant_id\":7", "\"tenant_id\":8"),
            ("\"connection_id\":31", "\"connection_id\":32"),
            ("\"observed_ms\":1500", "\"observed_ms\":1900"),
        ] {
            assert_eq!(
                h.handle(&good.replace(from, to), 1_000).await,
                Outcome::Rejected("bad_signature"),
                "{from}"
            );
        }
        // Expired / too far ahead.
        assert_eq!(
            h.handle(&event_with(&pair(7), 7, 31, 100, 500), 1_000)
                .await,
            Outcome::Rejected("expired")
        );
        assert_eq!(
            h.handle(
                &event_with(&pair(7), 7, 31, 100, 1_000 + MAX_AHEAD_MS + 1),
                1_000
            )
            .await,
            Outcome::Rejected("expired")
        );
        // Malformed / oversize / a connection id that is not a real row.
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
        // First delivery lifts, the replay does not.
        assert!(matches!(
            h.handle(&good, 1_000).await,
            Outcome::Lifted { .. }
        ));
        assert_eq!(h.handle(&good, 1_000).await, Outcome::Rejected("replayed"));
        assert_eq!(lift.calls.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn this_stage_holds_public_keys_only_and_rejects_a_bad_ring() {
        assert!(VerifyKeys::parse("{}").is_err());
        assert!(VerifyKeys::parse("not json").is_err());
        assert!(
            VerifyKeys::parse("{\"k1\":\"AAAA\"}").is_err(),
            "not 32 bytes"
        );
        // A private seed (32 bytes) parses as a "public key" but signs nothing: an event signed
        // by the real pair does not verify against the wrong key material.
        let wrong =
            VerifyKeys::parse(&format!("{{\"k1\":\"{}\"}}", B64.encode([1u8; 32]))).unwrap();
        let lift = Arc::new(FakeLift::default());
        let h = PlatformUnban::new(
            lift.clone(),
            wrong,
            Arc::new(FakeSeen::default()),
            crate::stage_metrics::stage_metrics(),
        );
        assert_eq!(
            h.handle(&event_with(&pair(7), 7, 31, 1_500, 2_000), 1_000)
                .await,
            Outcome::Rejected("bad_signature")
        );
    }

    #[tokio::test]
    async fn the_single_use_store_failing_closed_and_a_lift_error_are_errors() {
        let lift = Arc::new(FakeLift::default());
        assert_eq!(
            handler(lift.clone(), true)
                .handle(&event_with(&pair(7), 7, 31, 1_500, 2_000), 1_000)
                .await,
            Outcome::Error
        );
        assert!(lift.calls.lock().unwrap().is_empty());
        *lift.result.lock().unwrap() = Some(Err("db".into()));
        assert_eq!(
            handler(lift, false)
                .handle(&event_with(&pair(7), 7, 31, 1_500, 2_000), 1_000)
                .await,
            Outcome::Error
        );
    }
}
