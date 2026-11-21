//! Platform-originated unban intake: Discord `GUILD_BAN_REMOVE` from OUR gateway connection.
//!
//! A moderator who unbans someone in Discord's own UI must lift the matching Waddles floor ban.
//! svc-ingest holds the gateway connection but has no database, and the lift function is granted
//! to svc-action alone, so this module only turns the frame into a *verified, signed event* on a
//! dedicated Valkey list that svc-action consumes (`svc_action::platform_unban`):
//!
//! ```text
//!  gateway frame -> UnbanTap -> bound to the RECEIVING connection (gateway ws, its guild, its tenant)
//!                            -> user id -> user UUID           [hub-api tokenizer]
//!                            -> Ed25519 sign -> LPUSH waddles:platform-unban:discord:events
//! ```
//!
//! **Connection binding (the trust anchor).** Tenants bring their own Discord application, so a
//! `GUILD_BAN_REMOVE` is honoured only when the frame arrived on the gateway WebSocket of a
//! connection (a) whose host-derived binding names THIS guild, and (b) whose tenant (the scope
//! authority's answer for that connection) owns the guild pairing. A frame for another tenant's
//! guild on this connection, or one that arrived through any HTTP/webhook/poll edge, is dropped
//! and counted (`svc_ingest_platform_unban_rejected_total{reason}`), never forwarded. The signed
//! event carries the connection id and tenant so svc-action re-binds them under the signature.
//!
//! **Keys.** Events are Ed25519-signed with a dedicated key held only here; svc-action holds the
//! PUBLIC half and so cannot mint one; no bundle has either. The raw Discord user id never leaves
//! this module (only the tokenized UUID is on the wire) and logs carry numeric ids and fixed
//! reason codes only. The tap never alters the frame's fate: it always answers `Pass`.

use std::sync::{Arc, LazyLock};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use ring::signature::Ed25519KeyPair;
use serde::Serialize;
use uuid::Uuid;

use crate::commands_reconcile::BoxFut;
use crate::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};
use crate::connector_runtime::scoped::ScopeLookup;
use crate::discord_rest::is_snowflake;
use crate::interaction_tap::{FrameBinding, FrameTap, TapVerdict};
use crate::scope_resolver::Unresolved;

/// Valkey list the verified events are pushed onto (consumed by svc-action).
pub const EVENTS_LIST: &str = "waddles:platform-unban:discord:events";
/// Signature domain-separation label; must equal svc-action's.
pub const DOMAIN: &[u8] = b"waddles.platform-unban.v1";
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

static UNBAN_REJECTED: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_platform_unban_rejected_total",
            "GUILD_BAN_REMOVE frames dropped because they did not arrive on the gateway connection bound to their guild and tenant, by fixed reason",
        ),
        &["reason"],
    )
    .expect("valid metric definition")
});

/// Registers the counters on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(UNBAN_TOTAL.clone()))?;
    registry.register(Box::new(UNBAN_REJECTED.clone()))
}

/// Count of one `(outcome, reason)` (test/observability helper).
#[must_use]
pub fn count(outcome: &str, reason: &str) -> u64 {
    UNBAN_TOTAL.with_label_values(&[outcome, reason]).get()
}

/// Count of one rejection reason (test/observability helper).
#[must_use]
pub fn rejected_count(reason: &str) -> u64 {
    UNBAN_REJECTED.with_label_values(&[reason]).get()
}

fn note(outcome: &'static str, reason: &'static str) {
    UNBAN_TOTAL.with_label_values(&[outcome, reason]).inc();
}

fn ignored(reason: &'static str) {
    note("ignored", reason);
    tracing::info!(reason, "platform unban event ignored");
}

fn rejected(reason: &'static str, binding: Option<&FrameBinding>) {
    UNBAN_REJECTED.with_label_values(&[reason]).inc();
    tracing::warn!(
        reason,
        connection_id = binding.map(|b| b.connection_id),
        tenant_id = binding.map(|b| b.tenant_id),
        "platform unban frame rejected: not bound to the connection it arrived on"
    );
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

/// The exact bytes signed: the domain label, then each field length-prefixed (u32 BE) so
/// boundaries can never shift; numbers as decimal text. Must equal svc-action's `signing_bytes`.
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

/// The Ed25519 signing identity of svc-ingest for platform-unban events.
pub struct Signer {
    kid: String,
    pair: Ed25519KeyPair,
}

impl Signer {
    /// Builds a signer from a key id and the 32-byte private seed.
    ///
    /// # Errors
    /// A fixed reason when the seed is not a valid Ed25519 seed.
    pub fn from_seed(kid: &str, seed: &[u8]) -> Result<Self, &'static str> {
        if kid.is_empty() {
            return Err("empty_kid");
        }
        let pair = Ed25519KeyPair::from_seed_unchecked(seed).map_err(|_| "bad_seed")?;
        Ok(Self {
            kid: kid.to_owned(),
            pair,
        })
    }

    /// Reads `PLATFORM_UNBAN_SIGNING_KEY` (base64 32-byte seed) and `PLATFORM_UNBAN_KEY_ID`;
    /// `None` (logged, no key material) when unset or unusable.
    #[must_use]
    pub fn from_env() -> Option<Self> {
        let (Ok(seed_b64), Ok(kid)) = (
            std::env::var("PLATFORM_UNBAN_SIGNING_KEY"),
            std::env::var("PLATFORM_UNBAN_KEY_ID"),
        ) else {
            tracing::error!(
                "PLATFORM_UNBAN_SIGNING_KEY / PLATFORM_UNBAN_KEY_ID not set; Discord-native unban events will be ignored"
            );
            return None;
        };
        let seed = B64.decode(seed_b64.trim()).ok()?;
        match Self::from_seed(kid.trim(), &seed) {
            Ok(s) => Some(s),
            Err(reason) => {
                tracing::error!(
                    reason,
                    "platform unban signing key unusable; events will be ignored"
                );
                None
            }
        }
    }

    fn sign(&self, msg: &[u8]) -> String {
        B64.encode(self.pair.sign(msg).as_ref())
    }
}

/// The signed event as it is placed on [`EVENTS_LIST`].
#[derive(Debug, Serialize)]
pub struct WireEvent<'a> {
    /// Wire version.
    pub v: u32,
    /// Unique event id (single-use marker on the consumer side).
    pub event_id: &'a str,
    /// Tenant of the RECEIVING connection (not a frame field).
    pub tenant_id: i64,
    /// Hub-api connection row the frame arrived on.
    pub connection_id: i64,
    /// Always `discord`.
    pub platform: &'a str,
    /// The guild (not PII).
    pub guild_id: &'a str,
    /// The tokenized user UUID.
    pub user_uuid: &'a str,
    /// When the unban was observed (host receipt time at the gateway), epoch ms.
    pub observed_ms: u64,
    /// Deadline, epoch ms.
    pub exp_ms: u64,
    /// Signing key id.
    pub kid: &'a str,
    /// Base64 Ed25519 signature over [`signing_bytes`].
    pub sig: &'a str,
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
    signer: Option<Signer>,
}

impl UnbanTap {
    /// Builds the tap. Without a `signer` every event is ignored loudly (`no_signing_key`)
    /// rather than sent unsigned.
    #[must_use]
    pub fn new(
        scope: Arc<dyn ScopeLookup>,
        tokenizer: Arc<dyn IdentityTokenizer>,
        sink: Arc<dyn UnbanSink>,
        signer: Option<Signer>,
    ) -> Self {
        Self {
            scope,
            tokenizer,
            sink,
            signer,
        }
    }

    async fn handle(&self, ev: BanRemoved, binding: Option<&FrameBinding>, observed_ms: u64) {
        note("received", "-");
        // 1. Bound to the connection it arrived on -- before any lookup, so a hostile frame costs
        //    no hub-api call and no tokenization.
        let Some(binding) = binding else {
            return rejected("no_connection_binding", None);
        };
        if !binding.gateway_ws {
            return rejected("not_gateway_transport", Some(binding));
        }
        if binding.bound_guild.as_deref() != Some(ev.guild_id.as_str()) {
            return rejected("guild_not_bound_to_connection", Some(binding));
        }
        if binding.tenant_id < 1 || binding.connection_id < 1 || binding.community_id < 1 {
            return ignored("tenant_wide_scope");
        }
        // 2. The pairing must belong to the SAME tenant as the receiving connection. An ambiguous
        //    guild (several communities of the tenant) is fine: svc-action expands under the signed
        //    tenant and a cross-tenant claim lifts nothing there.
        match self.scope.lookup(PLATFORM, &ev.guild_id).await {
            Ok(s) if s.tenant_id != binding.tenant_id => {
                return rejected("tenant_mismatch", Some(binding));
            }
            Ok(_) | Err(Unresolved::Ambiguous) => {}
            Err(Unresolved::NotFound) => return ignored("unpaired_guild"),
            Err(Unresolved::Transient) => return ignored("scope_unavailable"),
            Err(_) => return ignored("scope_invalid"),
        }
        let call = CallScope {
            platform: PLATFORM,
            community_id: binding.community_id,
            tenant_id: binding.tenant_id,
            connection_id: binding.connection_id,
        };
        let user_uuid = match self.tokenizer.tokenize(&call, &ev.user_id).await {
            Ok(u) if Uuid::parse_str(&u).is_ok() => u,
            Ok(_) => return ignored("tokenize_invalid"),
            Err(TokenizeError::Unavailable | TokenizeError::RateLimited) => {
                return ignored("tokenize_unavailable")
            }
            Err(_) => return ignored("tokenize_refused"),
        };
        let Some(signer) = &self.signer else {
            tracing::error!(
                tenant_id = binding.tenant_id,
                connection_id = binding.connection_id,
                "platform unban NOT forwarded: no signing key configured"
            );
            return ignored("no_signing_key");
        };
        let event_id = Uuid::new_v4().to_string();
        let exp_ms = observed_ms.saturating_add(EVENT_TTL_MS);
        let msg = signing_bytes(
            &event_id,
            binding.tenant_id,
            binding.connection_id,
            PLATFORM,
            &ev.guild_id,
            &user_uuid,
            observed_ms,
            exp_ms,
        );
        let sig = signer.sign(&msg);
        let wire = WireEvent {
            v: 1,
            event_id: &event_id,
            tenant_id: binding.tenant_id,
            connection_id: binding.connection_id,
            platform: PLATFORM,
            guild_id: &ev.guild_id,
            user_uuid: &user_uuid,
            observed_ms,
            exp_ms,
            kid: &signer.kid,
            sig: &sig,
        };
        let Ok(payload) = serde_json::to_string(&wire) else {
            return ignored("serialize");
        };
        match self.sink.push(payload).await {
            Ok(()) => {
                note("forwarded", "-");
                tracing::info!(
                    tenant_id = binding.tenant_id,
                    connection_id = binding.connection_id,
                    "platform unban event forwarded to svc-action"
                );
            }
            Err(()) => {
                note("error", "publish_failed");
                tracing::error!(
                    tenant_id = binding.tenant_id,
                    connection_id = binding.connection_id,
                    "ALERT platform unban event could not be queued; the Waddles ban stays in force"
                );
            }
        }
    }
}

impl FrameTap for UnbanTap {
    /// Without a connection binding a ban-remove frame is refused (never forwarded).
    fn on_frame<'a>(&'a self, platform: &'a str, bytes: &'a [u8]) -> BoxFut<'a, TapVerdict> {
        self.on_frame_bound(platform, None, bytes)
    }

    fn on_frame_bound<'a>(
        &'a self,
        platform: &'a str,
        binding: Option<&'a FrameBinding>,
        bytes: &'a [u8],
    ) -> BoxFut<'a, TapVerdict> {
        Box::pin(async move {
            if platform != PLATFORM {
                return TapVerdict::Pass;
            }
            // Host receipt time at the gateway: dispatches carry no timestamp of their own.
            let observed_ms = now_ms();
            match parse_ban_removed(bytes) {
                Ok(None) => {}
                Ok(Some(ev)) => self.handle(ev, binding, observed_ms).await,
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
/// frame). The receiving connection's binding is passed to every tap.
pub struct ChainTap(pub Vec<Arc<dyn FrameTap>>);

impl FrameTap for ChainTap {
    fn on_frame<'a>(&'a self, platform: &'a str, bytes: &'a [u8]) -> BoxFut<'a, TapVerdict> {
        self.on_frame_bound(platform, None, bytes)
    }

    fn on_frame_bound<'a>(
        &'a self,
        platform: &'a str,
        binding: Option<&'a FrameBinding>,
        bytes: &'a [u8],
    ) -> BoxFut<'a, TapVerdict> {
        Box::pin(async move {
            for tap in &self.0 {
                match tap.on_frame_bound(platform, binding, bytes).await {
                    TapVerdict::Pass => {}
                    other => return other,
                }
            }
            TapVerdict::Pass
        })
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::scope_resolver::ResolvedScope;
    use ring::signature::{KeyPair, UnparsedPublicKey, ED25519};
    use std::sync::Mutex;

    const GUILD: &str = "111111111111111111";
    const OTHER_GUILD: &str = "999999999999999999";
    const USER: &str = "222222222222222222";
    const UUID: &str = "11111111-2222-4333-8444-555555555555";

    fn frame(t: &str, guild: &str, user: &str) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({
            "op": 0, "s": 3, "t": t,
            "d": {"guild_id": guild, "user": {"id": user, "username": "x"}}
        }))
        .unwrap()
    }

    /// Tenant 7's gateway connection 31, bound to `GUILD`.
    fn conn() -> FrameBinding {
        FrameBinding {
            tenant_id: 7,
            community_id: 9,
            connection_id: 31,
            bound_guild: Some(GUILD.into()),
            gateway_ws: true,
        }
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

    /// Resolves `GUILD` to tenant 7 and `OTHER_GUILD` (another tenant's) to tenant 8.
    struct Pairing(Result<(), Unresolved>);
    impl ScopeLookup for Pairing {
        fn lookup<'a>(
            &'a self,
            _p: &'a str,
            source: &'a str,
        ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
            Box::pin(async move {
                self.0?;
                let tenant = if source == GUILD { 7 } else { 8 };
                Ok(ResolvedScope::validated(tenant, 9, "acme", Some("main")).unwrap())
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
            assert_eq!(
                (scope.tenant_id, scope.connection_id),
                (7, 31),
                "the REAL connection id and tenant are used"
            );
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

    fn signer() -> Signer {
        Signer::from_seed("k1", &[7u8; 32]).unwrap()
    }

    fn tap(
        scope: Result<(), Unresolved>,
        tok: Result<String, TokenizeError>,
        signer: Option<Signer>,
    ) -> (UnbanTap, Arc<Collect>) {
        let sink = Arc::new(Collect::default());
        (
            UnbanTap::new(
                Arc::new(Pairing(scope)),
                Arc::new(FixedTok(tok)),
                sink.clone(),
                signer,
            ),
            sink,
        )
    }

    #[test]
    fn the_signing_bytes_have_a_stable_golden_vector_shared_with_svc_action() {
        let msg = signing_bytes(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            7,
            31,
            "discord",
            GUILD,
            UUID,
            1_000,
            2_000,
        );
        assert_eq!(
            signer().sign(&msg),
            "dvDxEzityNFG2VJA3cWgD7Fe+XXdADdtTeYWQ5ygGkPVhFFvnOGcnrXtQaBclVWN0tVe79+Yc7Xb51+3w+vfDg=="
        );
    }

    #[tokio::test]
    async fn the_correct_connection_forwards_a_signed_event_with_no_raw_user_id() {
        let (t, sink) = tap(Ok(()), Ok(UUID.into()), Some(signer()));
        let before = count("forwarded", "-");
        let b = conn();
        assert_eq!(
            t.on_frame_bound("discord", Some(&b), &frame("GUILD_BAN_REMOVE", GUILD, USER))
                .await,
            TapVerdict::Pass
        );
        // Counters are process-global and tests run in parallel: assert growth, not equality.
        assert!(count("forwarded", "-") > before);
        let out = sink.0.lock().unwrap();
        assert_eq!(out.len(), 1);
        assert!(!out[0].contains(USER), "raw discord user id on the wire");
        let v: serde_json::Value = serde_json::from_str(&out[0]).unwrap();
        assert_eq!(v["tenant_id"], 7);
        assert_eq!(v["connection_id"], 31);
        assert_eq!(v["user_uuid"], UUID);
        assert!(
            v.get("community_id").is_none(),
            "svc-action expands guild -> communities"
        );
        let msg = signing_bytes(
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

    #[tokio::test]
    async fn a_frame_for_another_tenants_guild_on_this_connection_forwards_nothing() {
        let (t, sink) = tap(Ok(()), Ok(UUID.into()), Some(signer()));
        let before = rejected_count("guild_not_bound_to_connection");
        let b = conn();
        t.on_frame_bound(
            "discord",
            Some(&b),
            &frame("GUILD_BAN_REMOVE", OTHER_GUILD, USER),
        )
        .await;
        assert_eq!(rejected_count("guild_not_bound_to_connection"), before + 1);
        assert!(sink.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_pairing_owned_by_another_tenant_than_the_connection_is_rejected() {
        // The connection is bound to OTHER_GUILD, but the pairing resolves to tenant 8, not 7.
        let (t, sink) = tap(Ok(()), Ok(UUID.into()), Some(signer()));
        let before = rejected_count("tenant_mismatch");
        let mut b = conn();
        b.bound_guild = Some(OTHER_GUILD.into());
        t.on_frame_bound(
            "discord",
            Some(&b),
            &frame("GUILD_BAN_REMOVE", OTHER_GUILD, USER),
        )
        .await;
        assert_eq!(rejected_count("tenant_mismatch"), before + 1);
        assert!(sink.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_frame_through_an_http_or_unbound_edge_forwards_nothing() {
        let (t, sink) = tap(Ok(()), Ok(UUID.into()), Some(signer()));
        let (http_before, none_before) = (
            rejected_count("not_gateway_transport"),
            rejected_count("no_connection_binding"),
        );
        let mut http = conn();
        http.gateway_ws = false;
        t.on_frame_bound(
            "discord",
            Some(&http),
            &frame("GUILD_BAN_REMOVE", GUILD, USER),
        )
        .await;
        t.on_frame("discord", &frame("GUILD_BAN_REMOVE", GUILD, USER))
            .await;
        assert_eq!(rejected_count("not_gateway_transport"), http_before + 1);
        assert_eq!(rejected_count("no_connection_binding"), none_before + 1);
        assert!(sink.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn every_ignore_reason_is_counted_and_nothing_is_forwarded() {
        type Case = (
            &'static str,
            Result<(), Unresolved>,
            Result<String, TokenizeError>,
            bool,
        );
        let cases: Vec<Case> = vec![
            (
                "unpaired_guild",
                Err(Unresolved::NotFound),
                Ok(UUID.into()),
                true,
            ),
            (
                "scope_unavailable",
                Err(Unresolved::Transient),
                Ok(UUID.into()),
                true,
            ),
            (
                "tokenize_unavailable",
                Ok(()),
                Err(TokenizeError::Unavailable),
                true,
            ),
            ("tokenize_refused", Ok(()), Err(TokenizeError::Denied), true),
            ("tokenize_invalid", Ok(()), Ok("not-a-uuid".into()), true),
            ("no_signing_key", Ok(()), Ok(UUID.into()), false),
        ];
        for (reason, scope, tok, with_key) in cases {
            let (t, sink) = tap(scope, tok, with_key.then(signer));
            let before = count("ignored", reason);
            let b = conn();
            t.on_frame_bound("discord", Some(&b), &frame("GUILD_BAN_REMOVE", GUILD, USER))
                .await;
            assert_eq!(count("ignored", reason), before + 1, "{reason}");
            assert!(sink.0.lock().unwrap().is_empty(), "{reason} forwarded");
        }
    }

    #[tokio::test]
    async fn an_ambiguous_guild_still_forwards_because_action_expands_under_the_tenant() {
        let (t, sink) = tap(Err(Unresolved::Ambiguous), Ok(UUID.into()), Some(signer()));
        let b = conn();
        t.on_frame_bound("discord", Some(&b), &frame("GUILD_BAN_REMOVE", GUILD, USER))
            .await;
        assert_eq!(sink.0.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn other_platforms_and_frames_are_untouched() {
        let (t, sink) = tap(Ok(()), Ok(UUID.into()), Some(signer()));
        let b = conn();
        t.on_frame_bound("twitch", Some(&b), &frame("GUILD_BAN_REMOVE", GUILD, USER))
            .await;
        t.on_frame_bound("discord", Some(&b), &frame("GUILD_BAN_ADD", GUILD, USER))
            .await;
        assert!(sink.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn chain_stops_at_the_first_non_pass_and_passes_the_binding_through() {
        struct Refuse;
        impl FrameTap for Refuse {
            fn on_frame<'a>(&'a self, _: &'a str, _: &'a [u8]) -> BoxFut<'a, TapVerdict> {
                Box::pin(async { TapVerdict::Duplicate })
            }
        }
        let (t, sink) = tap(Ok(()), Ok(UUID.into()), Some(signer()));
        let b = conn();
        let blocked = ChainTap(vec![Arc::new(Refuse), Arc::new(t)]);
        assert_eq!(
            blocked
                .on_frame_bound("discord", Some(&b), &frame("GUILD_BAN_REMOVE", GUILD, USER))
                .await,
            TapVerdict::Duplicate
        );
        assert!(sink.0.lock().unwrap().is_empty());
        let (t, sink) = tap(Ok(()), Ok(UUID.into()), Some(signer()));
        let open = ChainTap(vec![Arc::new(t)]);
        open.on_frame_bound("discord", Some(&b), &frame("GUILD_BAN_REMOVE", GUILD, USER))
            .await;
        assert_eq!(
            sink.0.lock().unwrap().len(),
            1,
            "binding reached the inner tap"
        );
    }
}
