//! Platform-originated unban intake: Discord `GUILD_BAN_REMOVE` from OUR gateway connection.
//!
//! A moderator who unbans someone in Discord's own UI must lift the matching Waddles floor ban.
//! svc-ingest holds the gateway connection but has no database, and the lift function is granted
//! to svc-action alone, so this module only turns the frame into a *verified, signed event* on a
//! dedicated Valkey list that svc-action consumes (`svc_action::platform_unban`):
//!
//! ```text
//!  gateway frame -> UnbanTap -> guild -> (tenant, community)   [hub-api scope resolver]
//!                            -> user id -> user UUID           [hub-api tokenizer, mint scope]
//!                            -> pin_sig MAC -> LPUSH waddles:platform-unban:discord:events
//! ```
//!
//! **Trust.** The event is believable only because it was produced here, from a frame our own
//! authenticated gateway session received, for a guild hub-api resolves to exactly one community.
//! The MAC (under the dedicated `OUTBOUND_PIN_KEYS` ring, mounted only into svc-ingest and
//! svc-action, never the executor/bundles, with its own domain-separation label) is what makes
//! the list entry unforgeable by anything else with Valkey access. The raw Discord user id never
//! leaves this module: only the tokenized UUID is on the wire, and logs carry numeric ids and
//! fixed reason codes only.
//!
//! The tap never alters the frame's fate: it always answers `Pass`.

use std::sync::{Arc, LazyLock};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use uuid::Uuid;

use crate::commands_reconcile::BoxFut;
use crate::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};
use crate::connector_runtime::scoped::ScopeLookup;
use crate::discord_rest::is_snowflake;
use crate::interaction_tap::{FrameTap, TapVerdict};
use crate::pin_sig::{PinFields, PinKeys};
use crate::scope_resolver::Unresolved;

/// Valkey list the verified events are pushed onto (consumed by svc-action).
pub const EVENTS_LIST: &str = "waddles:platform-unban:discord:events";
/// Domain-separation label in the MAC's `platform` slot (never a real outbound platform).
pub const MAC_PLATFORM: &str = "platform-unban";
/// MAC `op` of the event.
pub const MAC_OP: &str = "ban.removed";
/// MAC `origin_channel` placeholder (the event has no origin channel).
pub const MAC_NO_ORIGIN: &str = "-";
/// Wire platform of the event.
pub const PLATFORM: &str = "discord";
/// Lifetime of an event: svc-action refuses it after this.
pub const EVENT_TTL_MS: u64 = 5 * 60_000;
/// Cap on the list length; the oldest entries are trimmed (a stalled consumer never grows it
/// without bound).
pub const LIST_MAX_LEN: isize = 10_000;
/// Gateway event name this tap consumes.
const EVENT_NAME: &str = "GUILD_BAN_REMOVE";

static UNBAN_TOTAL: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_platform_unban_total",
            "Discord GUILD_BAN_REMOVE events by outcome (received / forwarded / ignored / error) and fixed reason",
        ),
        &["outcome", "reason"],
    )
    .expect("valid metric definition")
});

/// Registers the counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(UNBAN_TOTAL.clone()))
}

/// Count of one `(outcome, reason)` (test/observability helper).
#[must_use]
pub fn count(outcome: &str, reason: &str) -> u64 {
    UNBAN_TOTAL.with_label_values(&[outcome, reason]).get()
}

fn note(outcome: &'static str, reason: &'static str) {
    UNBAN_TOTAL.with_label_values(&[outcome, reason]).inc();
}

/// One parsed `GUILD_BAN_REMOVE` dispatch.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BanRemoved {
    /// The guild the unban happened in (snowflake).
    pub guild_id: String,
    /// The unbanned user (snowflake; raw platform id, never leaves this module).
    pub user_id: String,
}

/// Why a `GUILD_BAN_REMOVE` frame could not be parsed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Malformed;

/// Parses `bytes` as a gateway frame. `Ok(None)` for any other frame; `Err` only for a
/// `GUILD_BAN_REMOVE` whose guild/user ids are missing or not snowflakes.
///
/// # Errors
/// [`Malformed`] for a ban-remove dispatch with bad ids.
pub fn parse_ban_removed(bytes: &[u8]) -> Result<Option<BanRemoved>, Malformed> {
    // Cheap prefilter: this runs on every Discord frame.
    if !bytes
        .windows(EVENT_NAME.len())
        .any(|w| w == EVENT_NAME.as_bytes())
    {
        return Ok(None);
    }
    let Ok(v) = serde_json::from_slice::<serde_json::Value>(bytes) else {
        return Ok(None);
    };
    if v.get("op").and_then(serde_json::Value::as_u64) != Some(0)
        || v.get("t").and_then(serde_json::Value::as_str) != Some(EVENT_NAME)
    {
        return Ok(None);
    }
    let d = v.get("d").ok_or(Malformed)?;
    let guild = d
        .get("guild_id")
        .and_then(serde_json::Value::as_str)
        .filter(|s| is_snowflake(s))
        .ok_or(Malformed)?;
    let user = d
        .get("user")
        .and_then(|u| u.get("id"))
        .and_then(serde_json::Value::as_str)
        .filter(|s| is_snowflake(s))
        .ok_or(Malformed)?;
    Ok(Some(BanRemoved {
        guild_id: guild.to_owned(),
        user_id: user.to_owned(),
    }))
}

/// The signed event as it is placed on [`EVENTS_LIST`].
#[derive(Debug, Serialize)]
pub struct WireEvent<'a> {
    /// Wire version.
    pub v: u32,
    /// Unique event id (single-use marker on the consumer side).
    pub event_id: &'a str,
    /// Numeric tenant resolved from the guild pairing.
    pub tenant_id: i64,
    /// Numeric community resolved from the guild pairing.
    pub community_id: i64,
    /// Always `discord`.
    pub platform: &'a str,
    /// The guild (not PII).
    pub guild_id: &'a str,
    /// The tokenized user UUID.
    pub user_uuid: &'a str,
    /// Deadline, epoch ms.
    pub exp_ms: u64,
    /// `kid:hexmac`.
    pub sig: &'a str,
}

/// The MAC fields of an event (shared shape with the consumer).
#[must_use]
pub fn mac_fields<'a>(
    guild_id: &'a str,
    user_uuid: &'a str,
    event_id: &'a str,
    exp_ms: u64,
    community_id: i64,
    tenant_id: i64,
) -> PinFields<'a> {
    PinFields {
        platform: MAC_PLATFORM,
        op: MAC_OP,
        channel: guild_id,
        origin_channel: MAC_NO_ORIGIN,
        message_id: "",
        text: "",
        user_id: user_uuid,
        op_id: event_id,
        exp_ms,
        community_id,
        tenant_id,
    }
}

/// Where signed events go (the Valkey list in production; a fake in unit tests).
pub trait UnbanSink: Send + Sync {
    /// Appends one serialized event.
    fn push<'a>(&'a self, payload: String) -> BoxFut<'a, Result<(), ()>>;
}

/// Valkey-backed sink: `LPUSH` + `LTRIM` (bounded), consumed with `BRPOP` (FIFO).
pub struct ValkeySink {
    conn: redis::aio::ConnectionManager,
}

impl ValkeySink {
    /// Wraps a connected manager.
    #[must_use]
    pub fn new(conn: redis::aio::ConnectionManager) -> Self {
        Self { conn }
    }
}

impl UnbanSink for ValkeySink {
    fn push<'a>(&'a self, payload: String) -> BoxFut<'a, Result<(), ()>> {
        Box::pin(async move {
            let mut conn = self.conn.clone();
            redis::pipe()
                .cmd("LPUSH")
                .arg(EVENTS_LIST)
                .arg(payload)
                .ignore()
                .cmd("LTRIM")
                .arg(EVENTS_LIST)
                .arg(0)
                .arg(LIST_MAX_LEN - 1)
                .ignore()
                .query_async::<()>(&mut conn)
                .await
                .map_err(|_| ())
        })
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

/// The gateway tap that turns `GUILD_BAN_REMOVE` into a signed platform-unban event.
pub struct UnbanTap {
    scope: Arc<dyn ScopeLookup>,
    tokenizer: Arc<dyn IdentityTokenizer>,
    sink: Arc<dyn UnbanSink>,
    keys: Option<PinKeys>,
}

impl UnbanTap {
    /// Builds the tap. `keys` must carry an active signing kid; without one every event is
    /// ignored loudly (`no_signing_key`) rather than sent unsigned.
    #[must_use]
    pub fn new(
        scope: Arc<dyn ScopeLookup>,
        tokenizer: Arc<dyn IdentityTokenizer>,
        sink: Arc<dyn UnbanSink>,
        keys: Option<PinKeys>,
    ) -> Self {
        Self {
            scope,
            tokenizer,
            sink,
            keys,
        }
    }

    async fn handle(&self, ev: BanRemoved) {
        note("received", "-");
        let scope = match self.scope.lookup(PLATFORM, &ev.guild_id).await {
            Ok(s) => s,
            Err(Unresolved::NotFound) => return ignored("unpaired_guild"),
            Err(Unresolved::Ambiguous) => return ignored("ambiguous_guild"),
            Err(Unresolved::Transient) => return ignored("scope_unavailable"),
            Err(_) => return ignored("scope_invalid"),
        };
        if scope.community_id < 1 {
            return ignored("tenant_wide_scope");
        }
        let call = CallScope {
            platform: PLATFORM,
            community_id: i64::from(scope.community_id),
            tenant_id: scope.tenant_id,
            connection_id: 0,
        };
        let user_uuid = match self.tokenizer.tokenize(&call, &ev.user_id).await {
            Ok(u) if Uuid::parse_str(&u).is_ok() => u,
            Ok(_) => return ignored("tokenize_invalid"),
            Err(TokenizeError::Unavailable | TokenizeError::RateLimited) => {
                return ignored("tokenize_unavailable")
            }
            Err(_) => return ignored("tokenize_refused"),
        };
        let Some(keys) = &self.keys else {
            tracing::error!(
                tenant_id = scope.tenant_id,
                community_id = scope.community_id,
                "platform unban NOT forwarded: no outbound pin signing key configured"
            );
            return ignored("no_signing_key");
        };
        let event_id = Uuid::new_v4().to_string();
        let exp_ms = now_ms().saturating_add(EVENT_TTL_MS);
        let community = i64::from(scope.community_id);
        let fields = mac_fields(
            &ev.guild_id,
            &user_uuid,
            &event_id,
            exp_ms,
            community,
            scope.tenant_id,
        );
        let Ok(sig) = keys.sign(&fields) else {
            tracing::error!("platform unban NOT forwarded: signing failed");
            return ignored("no_signing_key");
        };
        let wire = WireEvent {
            v: 1,
            event_id: &event_id,
            tenant_id: scope.tenant_id,
            community_id: community,
            platform: PLATFORM,
            guild_id: &ev.guild_id,
            user_uuid: &user_uuid,
            exp_ms,
            sig: &sig,
        };
        let Ok(payload) = serde_json::to_string(&wire) else {
            return ignored("serialize");
        };
        match self.sink.push(payload).await {
            Ok(()) => {
                note("forwarded", "-");
                tracing::info!(
                    tenant_id = scope.tenant_id,
                    community_id = scope.community_id,
                    "platform unban event forwarded to svc-action"
                );
            }
            Err(()) => {
                note("error", "publish_failed");
                tracing::error!(
                    tenant_id = scope.tenant_id,
                    community_id = scope.community_id,
                    "ALERT platform unban event could not be queued; the Waddles ban stays in force"
                );
            }
        }
    }
}

fn ignored(reason: &'static str) {
    note("ignored", reason);
    tracing::info!(reason, "platform unban event ignored");
}

impl FrameTap for UnbanTap {
    fn on_frame<'a>(&'a self, platform: &'a str, bytes: &'a [u8]) -> BoxFut<'a, TapVerdict> {
        Box::pin(async move {
            if platform != PLATFORM {
                return TapVerdict::Pass;
            }
            match parse_ban_removed(bytes) {
                Ok(None) => {}
                Ok(Some(ev)) => self.handle(ev).await,
                Err(Malformed) => {
                    note("received", "-");
                    ignored("malformed");
                }
            }
            TapVerdict::Pass
        })
    }
}

/// Runs several taps in order; the first non-`Pass` verdict wins (later taps do not see the
/// frame).
pub struct ChainTap(pub Vec<Arc<dyn FrameTap>>);

impl FrameTap for ChainTap {
    fn on_frame<'a>(&'a self, platform: &'a str, bytes: &'a [u8]) -> BoxFut<'a, TapVerdict> {
        Box::pin(async move {
            for tap in &self.0 {
                match tap.on_frame(platform, bytes).await {
                    TapVerdict::Pass => {}
                    other => return other,
                }
            }
            TapVerdict::Pass
        })
    }
}

/// Reads `OUTBOUND_PIN_KEYS` / `OUTBOUND_PIN_ACTIVE_KID` for signing; `None` (logged) when
/// unusable.
#[must_use]
pub fn signing_keys_from_env() -> Option<PinKeys> {
    let raw = std::env::var("OUTBOUND_PIN_KEYS").ok()?;
    let active = std::env::var("OUTBOUND_PIN_ACTIVE_KID").ok();
    match PinKeys::parse(&raw, active.as_deref()) {
        Ok(k) => Some(k),
        Err(e) => {
            tracing::error!(error = %e, "platform unban signing key unusable; events will be ignored");
            None
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::scope_resolver::ResolvedScope;
    use std::sync::Mutex;

    const GUILD: &str = "111111111111111111";
    const USER: &str = "222222222222222222";
    const UUID: &str = "11111111-2222-4333-8444-555555555555";

    fn frame(t: &str, guild: &str, user: &str) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({
            "op": 0, "s": 3, "t": t,
            "d": {"guild_id": guild, "user": {"id": user, "username": "x"}}
        }))
        .unwrap()
    }

    #[test]
    fn parses_only_guild_ban_remove() {
        assert_eq!(
            parse_ban_removed(&frame("GUILD_BAN_REMOVE", GUILD, USER)).unwrap(),
            Some(BanRemoved {
                guild_id: GUILD.into(),
                user_id: USER.into()
            })
        );
        assert_eq!(
            parse_ban_removed(&frame("GUILD_BAN_ADD", GUILD, USER)).unwrap(),
            None
        );
        assert_eq!(
            parse_ban_removed(br#"{"op":0,"t":"MESSAGE_CREATE","d":{}}"#).unwrap(),
            None
        );
        assert_eq!(
            parse_ban_removed(b"not json GUILD_BAN_REMOVE").unwrap(),
            None
        );
        // op != 0 is never a dispatch.
        assert_eq!(
            parse_ban_removed(br#"{"op":1,"t":"GUILD_BAN_REMOVE","d":{}}"#).unwrap(),
            None
        );
    }

    #[test]
    fn malformed_ids_are_rejected_loudly() {
        for (g, u) in [("", USER), (GUILD, ""), ("12a", USER), (GUILD, "../x")] {
            assert_eq!(
                parse_ban_removed(&frame("GUILD_BAN_REMOVE", g, u)),
                Err(Malformed)
            );
        }
        assert_eq!(
            parse_ban_removed(
                br#"{"op":0,"t":"GUILD_BAN_REMOVE","d":{"guild_id":"111111111111111111"}}"#
            ),
            Err(Malformed)
        );
    }

    struct FixedScope(Result<(i64, i32), Unresolved>);
    impl ScopeLookup for FixedScope {
        fn lookup<'a>(
            &'a self,
            _p: &'a str,
            _s: &'a str,
        ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
            Box::pin(async move {
                self.0.map(|(t, c)| {
                    ResolvedScope::validated(t, i64::from(c), "acme", (c != 0).then_some("main"))
                        .unwrap()
                })
            })
        }
    }
    struct FixedTok(Result<String, TokenizeError>);
    impl IdentityTokenizer for FixedTok {
        fn tokenize<'a>(
            &'a self,
            scope: &'a CallScope,
            id: &'a str,
        ) -> BoxFut<'a, Result<String, TokenizeError>> {
            assert_eq!(scope.platform, "discord");
            assert_eq!(id, USER);
            Box::pin(async move { self.0.clone() })
        }
    }
    #[derive(Default)]
    struct Collect(Mutex<Vec<String>>);
    impl UnbanSink for Collect {
        fn push<'a>(&'a self, p: String) -> BoxFut<'a, Result<(), ()>> {
            Box::pin(async move {
                self.0.lock().unwrap().push(p);
                Ok(())
            })
        }
    }

    type Case = (
        &'static str,
        Result<(i64, i32), Unresolved>,
        Result<String, TokenizeError>,
        Option<PinKeys>,
    );

    fn keys() -> PinKeys {
        PinKeys::parse("k1:00112233445566778899aabbccddeeff", Some("k1")).unwrap()
    }

    fn tap(
        scope: Result<(i64, i32), Unresolved>,
        tok: Result<String, TokenizeError>,
        keys: Option<PinKeys>,
    ) -> (UnbanTap, Arc<Collect>) {
        let sink = Arc::new(Collect::default());
        (
            UnbanTap::new(
                Arc::new(FixedScope(scope)),
                Arc::new(FixedTok(tok)),
                sink.clone(),
                keys,
            ),
            sink,
        )
    }

    #[tokio::test]
    async fn a_paired_guild_forwards_a_signed_event_with_no_raw_user_id() {
        let (t, sink) = tap(Ok((7, 9)), Ok(UUID.into()), Some(keys()));
        let before = count("forwarded", "-");
        assert_eq!(
            t.on_frame("discord", &frame("GUILD_BAN_REMOVE", GUILD, USER))
                .await,
            TapVerdict::Pass
        );
        assert_eq!(count("forwarded", "-"), before + 1);
        let out = sink.0.lock().unwrap();
        assert_eq!(out.len(), 1);
        assert!(!out[0].contains(USER), "raw discord user id on the wire");
        let v: serde_json::Value = serde_json::from_str(&out[0]).unwrap();
        assert_eq!(v["tenant_id"], 7);
        assert_eq!(v["community_id"], 9);
        assert_eq!(v["user_uuid"], UUID);
        let f = mac_fields(
            GUILD,
            UUID,
            v["event_id"].as_str().unwrap(),
            v["exp_ms"].as_u64().unwrap(),
            9,
            7,
        );
        keys().verify(&f, v["sig"].as_str().unwrap()).unwrap();
    }

    #[tokio::test]
    async fn every_ignore_reason_is_counted_and_nothing_is_forwarded() {
        let cases: Vec<Case> = vec![
            (
                "unpaired_guild",
                Err(Unresolved::NotFound),
                Ok(UUID.into()),
                Some(keys()),
            ),
            (
                "ambiguous_guild",
                Err(Unresolved::Ambiguous),
                Ok(UUID.into()),
                Some(keys()),
            ),
            (
                "scope_unavailable",
                Err(Unresolved::Transient),
                Ok(UUID.into()),
                Some(keys()),
            ),
            (
                "tenant_wide_scope",
                Ok((7, 0)),
                Ok(UUID.into()),
                Some(keys()),
            ),
            (
                "tokenize_unavailable",
                Ok((7, 9)),
                Err(TokenizeError::Unavailable),
                Some(keys()),
            ),
            (
                "tokenize_refused",
                Ok((7, 9)),
                Err(TokenizeError::Denied),
                Some(keys()),
            ),
            (
                "tokenize_invalid",
                Ok((7, 9)),
                Ok("not-a-uuid".into()),
                Some(keys()),
            ),
            ("no_signing_key", Ok((7, 9)), Ok(UUID.into()), None),
        ];
        for (reason, scope, tok, k) in cases {
            let (t, sink) = tap(scope, tok, k);
            let before = count("ignored", reason);
            t.on_frame("discord", &frame("GUILD_BAN_REMOVE", GUILD, USER))
                .await;
            assert_eq!(count("ignored", reason), before + 1, "{reason}");
            assert!(sink.0.lock().unwrap().is_empty(), "{reason} forwarded");
        }
    }

    #[tokio::test]
    async fn other_platforms_and_frames_are_untouched() {
        let (t, sink) = tap(Ok((7, 9)), Ok(UUID.into()), Some(keys()));
        let before = count("received", "-");
        t.on_frame("twitch", &frame("GUILD_BAN_REMOVE", GUILD, USER))
            .await;
        t.on_frame("discord", &frame("GUILD_BAN_ADD", GUILD, USER))
            .await;
        assert_eq!(count("received", "-"), before);
        assert!(sink.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn chain_stops_at_the_first_non_pass() {
        struct Refuse;
        impl FrameTap for Refuse {
            fn on_frame<'a>(&'a self, _: &'a str, _: &'a [u8]) -> BoxFut<'a, TapVerdict> {
                Box::pin(async { TapVerdict::Duplicate })
            }
        }
        let (t, sink) = tap(Ok((7, 9)), Ok(UUID.into()), Some(keys()));
        let chain = ChainTap(vec![Arc::new(Refuse), Arc::new(t)]);
        assert_eq!(
            chain
                .on_frame("discord", &frame("GUILD_BAN_REMOVE", GUILD, USER))
                .await,
            TapVerdict::Duplicate
        );
        assert!(sink.0.lock().unwrap().is_empty());
    }
}
