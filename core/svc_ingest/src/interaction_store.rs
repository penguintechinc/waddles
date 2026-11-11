//! Host-held Discord interaction token store.
//!
//! A Discord interaction carries a 15-minute reply credential (its `token`).
//! Slash-command design anti-goal: that token must NEVER reach bundle code. The
//! connector's normalizer already drops it from the event; this module is the
//! other half: the host captures `(id, application_id, token, guild_id)` from the
//! VERIFIED interaction (gateway frame or Ed25519-verified webhook body), stores
//! it in Valkey keyed by interaction id with the token's own lifetime as TTL, and
//! the executor's `interaction.*` duty resolves it host-side at send time. A
//! bundle only ever holds the interaction id (an opaque handle).
//!
//! * `SET NX` makes capture the idempotency gate: a redelivered interaction
//!   (gateway resume, webhook retry) is reported as a duplicate and is neither
//!   re-acked nor re-routed.
//! * Nothing here logs or `Debug`-prints the token; [`InteractionRecord`] redacts it.
//! * Ids are validated as snowflakes and the token against Discord's URL-safe
//!   charset, so a value that reaches a URL path slot cannot smuggle a separator.

use std::time::Duration;

use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::discord_rest::is_snowflake;

/// Interaction-token lifetime (Discord: 15 minutes); the Valkey TTL.
pub const TOKEN_TTL: Duration = Duration::from_secs(15 * 60);
/// Longest accepted interaction token (Discord tokens are ~80-200 chars).
const MAX_TOKEN_LEN: usize = 512;
/// Valkey key prefix.
const KEY_PREFIX: &str = "waddles.ingest.interaction.";

/// Interaction types the host acks/stores for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InteractionType {
    /// PING (1): answered with PONG on the HTTP path; never stored.
    Ping,
    /// APPLICATION_COMMAND (2).
    Command,
    /// MESSAGE_COMPONENT (3).
    Component,
    /// APPLICATION_COMMAND_AUTOCOMPLETE (4).
    Autocomplete,
    /// MODAL_SUBMIT (5).
    Modal,
}

impl InteractionType {
    /// The initial-response body that satisfies Discord's 3-second ack for this type.
    #[must_use]
    pub fn ack_body(self) -> Value {
        match self {
            Self::Ping => serde_json::json!({ "type": 1 }),
            // DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
            Self::Command => serde_json::json!({ "type": 5 }),
            // DEFERRED_UPDATE_MESSAGE
            Self::Component | Self::Modal => serde_json::json!({ "type": 6 }),
            // APPLICATION_COMMAND_AUTOCOMPLETE_RESULT (empty: bundles do not suggest in v1)
            Self::Autocomplete => serde_json::json!({ "type": 8, "data": { "choices": [] } }),
        }
    }
}

/// What the host keeps for one interaction. `Debug` redacts the token.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InteractionRecord {
    /// Application (client) id the interaction was addressed to.
    pub application_id: String,
    /// The 15-minute reply credential.
    pub token: String,
    /// Guild the interaction happened in (`None` = a DM).
    pub guild_id: Option<String>,
    /// Numeric community the interaction was ROUTED to (set by [`InteractionStore::set_routed`]
    /// once active-context routing resolved it). `None` = not routed yet: no community may
    /// answer it (fail closed).
    #[serde(default)]
    pub routed_community_id: Option<i64>,
    /// Raw Discord user id of the INVOKER, captured from the signature/gateway-verified frame
    /// (`member.user.id`, else `user.id`). The routing step derives the actor from it, so a
    /// guest event naming someone else's interaction cannot be routed (confused deputy).
    /// `None` = not captured: routing refuses. Never logged (`Debug` redacts it).
    #[serde(default)]
    pub invoker_user_id: Option<String>,
}

impl std::fmt::Debug for InteractionRecord {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("InteractionRecord")
            .field("application_id", &self.application_id)
            .field("token", &"[REDACTED]")
            .field("guild_id", &self.guild_id)
            .field("routed_community_id", &self.routed_community_id)
            .field("invoker_user_id", &"[REDACTED]")
            .finish()
    }
}

/// An interaction parsed out of a verified frame/body.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Captured {
    /// Interaction id (the opaque handle bundles see).
    pub id: String,
    /// Interaction type.
    pub kind: InteractionType,
    /// Reply credential + addressing.
    pub record: InteractionRecord,
}

/// Why a frame/body is not a storable interaction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CaptureError {
    /// Valid JSON but not an interaction at all (e.g. another gateway dispatch).
    NotAnInteraction,
    /// An interaction whose id/token/application is malformed: fail closed.
    Malformed(&'static str),
    /// An interaction type this host does not ack.
    UnsupportedType(u64),
}

fn token_ok(t: &str) -> bool {
    !t.is_empty()
        && t.len() <= MAX_TOKEN_LEN
        && t.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.')
}

fn snowflake_field(d: &Value, key: &'static str) -> Result<String, CaptureError> {
    match d.get(key).and_then(Value::as_str) {
        Some(s) if is_snowflake(s) => Ok(s.to_string()),
        _ => Err(CaptureError::Malformed(key)),
    }
}

/// Parses a verified interaction body (webhook POST) or a gateway
/// `INTERACTION_CREATE` envelope.
///
/// # Errors
/// [`CaptureError::NotAnInteraction`] for any other JSON; [`CaptureError::Malformed`]
/// naming the bad field (never its value); [`CaptureError::UnsupportedType`].
pub fn capture(bytes: &[u8]) -> Result<Captured, CaptureError> {
    let v: Value = serde_json::from_slice(bytes).map_err(|_| CaptureError::NotAnInteraction)?;
    let d = match v.get("op") {
        Some(_) => {
            if v.get("t").and_then(Value::as_str) != Some("INTERACTION_CREATE") {
                return Err(CaptureError::NotAnInteraction);
            }
            v.get("d").ok_or(CaptureError::Malformed("d"))?
        }
        None => &v,
    };
    let t = d
        .get("type")
        .and_then(Value::as_u64)
        .ok_or(CaptureError::NotAnInteraction)?;
    let kind = match t {
        1 => InteractionType::Ping,
        2 => InteractionType::Command,
        3 => InteractionType::Component,
        4 => InteractionType::Autocomplete,
        5 => InteractionType::Modal,
        other => return Err(CaptureError::UnsupportedType(other)),
    };
    let id = snowflake_field(d, "id")?;
    let application_id = snowflake_field(d, "application_id")?;
    let token = match d.get("token").and_then(Value::as_str) {
        Some(t) if token_ok(t) => t.to_string(),
        _ => return Err(CaptureError::Malformed("token")),
    };
    let guild_id = match d.get("guild_id") {
        None | Some(Value::Null) => None,
        Some(Value::String(g)) if is_snowflake(g) => Some(g.clone()),
        Some(_) => return Err(CaptureError::Malformed("guild_id")),
    };
    // Guild interactions carry `member.user`; DMs carry `user`. Either is verified-frame data.
    let invoker_user_id = d
        .get("member")
        .and_then(|m| m.get("user"))
        .or_else(|| d.get("user"))
        .and_then(|u| u.get("id"))
        .and_then(Value::as_str)
        .filter(|u| is_snowflake(u))
        .map(str::to_string);
    Ok(Captured {
        id,
        kind,
        record: InteractionRecord {
            application_id,
            token,
            guild_id,
            routed_community_id: None,
            invoker_user_id,
        },
    })
}

/// Store failure (never carries a token).
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// Valkey unreachable / errored.
    #[error("interaction store unavailable")]
    Unavailable,
    /// The stored value is not a record (corruption): fail closed.
    #[error("interaction record corrupt")]
    Corrupt,
}

/// Result of the write-once routing script.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RouteOnce {
    /// Unknown or expired interaction.
    Unknown,
    /// First routing: recorded now.
    Recorded,
    /// Already routed to this same community (idempotent success).
    Same,
    /// Already routed to a DIFFERENT community: refused, record unchanged.
    Conflict,
}

/// Sets `routed_community_id` only while null. Returns 0 unknown, 1 recorded, 2 same,
/// 3 conflict, 4 corrupt record. `KEEPTTL` preserves the interaction's remaining lifetime.
const ROUTE_ONCE_LUA: &str = r#"
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, rec = pcall(cjson.decode, raw)
if not ok or type(rec) ~= 'table' then return 4 end
local want = tonumber(ARGV[1])
local cur = rec['routed_community_id']
if cur == nil or cur == cjson.null then
  rec['routed_community_id'] = want
  redis.call('SET', KEYS[1], cjson.encode(rec), 'KEEPTTL')
  return 1
end
if cur == want then return 2 end
return 3
"#;

/// Valkey-backed store (one multiplexed connection).
#[derive(Clone)]
pub struct InteractionStore {
    conn: redis::aio::ConnectionManager,
    ttl: Duration,
}

fn key(id: &str) -> String {
    format!("{KEY_PREFIX}{id}")
}

impl InteractionStore {
    /// Wraps a connection with the production [`TOKEN_TTL`].
    #[must_use]
    pub fn new(conn: redis::aio::ConnectionManager) -> Self {
        Self::with_ttl(conn, TOKEN_TTL)
    }

    /// Explicit TTL (tests exercise expiry).
    #[must_use]
    pub fn with_ttl(conn: redis::aio::ConnectionManager, ttl: Duration) -> Self {
        Self { conn, ttl }
    }

    /// Stores `rec` for `id` unless one exists. `true` = first sighting (the
    /// caller acks and routes); `false` = duplicate delivery (drop quietly).
    ///
    /// # Errors
    /// [`StoreError::Unavailable`] when Valkey errors; the caller must then NOT
    /// route (a reply could never be sent) and must say so loudly.
    pub async fn put_new(&self, id: &str, rec: &InteractionRecord) -> Result<bool, StoreError> {
        let value = serde_json::to_string(rec).map_err(|_| StoreError::Corrupt)?;
        let mut c = self.conn.clone();
        let set: Option<String> = redis::cmd("SET")
            .arg(key(id))
            .arg(value)
            .arg("NX")
            .arg("EX")
            .arg(self.ttl.as_secs().max(1))
            .query_async(&mut c)
            .await
            .map_err(|_| StoreError::Unavailable)?;
        Ok(set.is_some())
    }

    /// Records the community `id` was routed to, WRITE-ONCE. `true` = `id` is now (or already
    /// was) routed to exactly `community_id`; `false` = unknown/expired OR already routed to a
    /// DIFFERENT community (use [`InteractionStore::route_once`] to tell them apart). Only the
    /// host's routing step calls this; a bundle never can.
    ///
    /// # Errors
    /// [`StoreError::Unavailable`] / [`StoreError::Corrupt`].
    pub async fn set_routed(&self, id: &str, community_id: i64) -> Result<bool, StoreError> {
        Ok(matches!(
            self.route_once(id, community_id).await?,
            RouteOnce::Recorded | RouteOnce::Same
        ))
    }

    /// Atomic write-once routing (one Lua script, so concurrent routers cannot interleave):
    /// sets `routed_community_id` only while it is null, keeps the remaining TTL, treats the
    /// same community as idempotent success and refuses a different one.
    ///
    /// # Errors
    /// [`StoreError::Unavailable`] when Valkey errors; [`StoreError::Corrupt`] when the stored
    /// value is not a record or the script answers unexpectedly.
    pub async fn route_once(&self, id: &str, community_id: i64) -> Result<RouteOnce, StoreError> {
        if !is_snowflake(id) {
            return Ok(RouteOnce::Unknown);
        }
        let mut c = self.conn.clone();
        let code: i64 = redis::Script::new(ROUTE_ONCE_LUA)
            .key(key(id))
            .arg(community_id)
            .invoke_async(&mut c)
            .await
            .map_err(|_| StoreError::Unavailable)?;
        match code {
            0 => Ok(RouteOnce::Unknown),
            1 => Ok(RouteOnce::Recorded),
            2 => Ok(RouteOnce::Same),
            3 => Ok(RouteOnce::Conflict),
            _ => Err(StoreError::Corrupt),
        }
    }

    /// Looks `id` up; `None` = unknown or expired.
    ///
    /// # Errors
    /// [`StoreError::Unavailable`] / [`StoreError::Corrupt`].
    pub async fn get(&self, id: &str) -> Result<Option<InteractionRecord>, StoreError> {
        if !is_snowflake(id) {
            return Ok(None);
        }
        let mut c = self.conn.clone();
        let raw: Option<String> = c.get(key(id)).await.map_err(|_| StoreError::Unavailable)?;
        raw.map(|r| serde_json::from_str(&r).map_err(|_| StoreError::Corrupt))
            .transpose()
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use serde_json::json;

    fn body(extra: Value) -> Vec<u8> {
        let mut v = json!({"id":"777","application_id":"42","type":2,"token":"aB-_.9",
            "guild_id":"111","data":{"name":"ping"}});
        if let (Some(m), Some(e)) = (v.as_object_mut(), extra.as_object()) {
            for (k, x) in e {
                m.insert(k.clone(), x.clone());
            }
        }
        serde_json::to_vec(&v).unwrap()
    }

    #[test]
    fn captures_webhook_and_gateway_forms_identically() {
        let hook = capture(&body(json!({}))).unwrap();
        let gw = capture(
            &serde_json::to_vec(&json!({"op":0,"s":1,"t":"INTERACTION_CREATE",
                "d": serde_json::from_slice::<Value>(&body(json!({}))).unwrap()}))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(hook, gw);
        assert_eq!(hook.kind, InteractionType::Command);
        assert_eq!(hook.record.guild_id.as_deref(), Some("111"));
        assert_eq!(hook.id, "777");
    }

    #[test]
    fn dm_has_no_guild_and_types_map_to_acks() {
        let dm = capture(&body(json!({"guild_id": null}))).unwrap();
        assert_eq!(dm.record.guild_id, None);
        for (t, ack) in [
            (2, json!({"type":5})),
            (3, json!({"type":6})),
            (5, json!({"type":6})),
            (4, json!({"type":8,"data":{"choices":[]}})),
            (1, json!({"type":1})),
        ] {
            assert_eq!(
                capture(&body(json!({"type": t}))).unwrap().kind.ack_body(),
                ack
            );
        }
    }

    #[test]
    fn malformed_and_foreign_input_fails_closed() {
        let bad = [
            json!({"id":"x"}),
            json!({"application_id":"x"}),
            json!({"token":""}),
            json!({"token":"a/b"}),
            json!({"token":"a b"}),
            json!({"token":"t".repeat(513)}),
            json!({"guild_id":"abc"}),
            json!({"guild_id": 5}),
        ];
        for extra in bad {
            assert!(
                matches!(
                    capture(&body(extra.clone())),
                    Err(CaptureError::Malformed(_))
                ),
                "{extra}"
            );
        }
        assert_eq!(
            capture(&body(json!({"type": 9}))),
            Err(CaptureError::UnsupportedType(9))
        );
        assert_eq!(capture(b"nope"), Err(CaptureError::NotAnInteraction));
        assert_eq!(
            capture(&serde_json::to_vec(&json!({"op":0,"t":"MESSAGE_CREATE","d":{}})).unwrap()),
            Err(CaptureError::NotAnInteraction)
        );
        assert_eq!(
            capture(&serde_json::to_vec(&json!({"op":10,"d":{"heartbeat_interval":1}})).unwrap()),
            Err(CaptureError::NotAnInteraction)
        );
    }

    #[test]
    fn debug_never_prints_the_token() {
        let c = capture(&body(json!({"token":"SUPER-SECRET"}))).unwrap();
        assert!(!format!("{c:?}").contains("SUPER-SECRET"));
    }

    async fn store(ttl: Duration) -> InteractionStore {
        let url = std::env::var("VALKEY_URL").expect("VALKEY_URL (a local valkey) is required");
        let client = redis::Client::open(url).unwrap();
        InteractionStore::with_ttl(client.get_connection_manager().await.unwrap(), ttl)
    }

    fn unique_id() -> String {
        let n = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        format!("9{n}")
    }

    #[tokio::test]
    async fn valkey_put_new_is_the_idempotency_gate_and_get_round_trips() {
        let s = store(TOKEN_TTL).await;
        let id = unique_id();
        let rec = capture(&body(json!({"id": id.clone()}))).unwrap().record;
        assert!(s.put_new(&id, &rec).await.unwrap(), "first sighting");
        assert!(!s.put_new(&id, &rec).await.unwrap(), "duplicate delivery");
        assert_eq!(s.get(&id).await.unwrap(), Some(rec));
        assert_eq!(s.get("not-a-snowflake").await.unwrap(), None);
        assert_eq!(s.get(&unique_id()).await.unwrap(), None);
    }

    #[tokio::test]
    async fn valkey_record_expires_with_the_token_ttl() {
        let s = store(Duration::from_secs(1)).await;
        let id = unique_id();
        let rec = capture(&body(json!({"id": id.clone()}))).unwrap().record;
        assert!(s.put_new(&id, &rec).await.unwrap());
        tokio::time::sleep(Duration::from_millis(2200)).await;
        assert_eq!(s.get(&id).await.unwrap(), None);
    }
    #[test]
    fn the_invoker_is_captured_from_the_verified_frame_and_never_debug_printed() {
        let guild = capture(&body(
            json!({"member":{"user":{"id":"700000000000000001"}}}),
        ))
        .unwrap();
        assert_eq!(
            guild.record.invoker_user_id.as_deref(),
            Some("700000000000000001")
        );
        let dm = capture(&body(json!({"user":{"id":"700000000000000002"}}))).unwrap();
        assert_eq!(
            dm.record.invoker_user_id.as_deref(),
            Some("700000000000000002")
        );
        // A non-snowflake or absent user records nothing: routing then refuses (fail closed).
        let bad = capture(&body(json!({"member":{"user":{"id":"not-an-id"}}}))).unwrap();
        assert_eq!(bad.record.invoker_user_id, None);
        assert_eq!(
            capture(&body(json!({}))).unwrap().record.invoker_user_id,
            None
        );
        assert!(!format!("{guild:?}").contains("700000000000000001"));
    }

    #[tokio::test]
    async fn routing_is_write_once_same_is_idempotent_different_is_refused() {
        // regression: a routed interaction must not be re-routable to another community.
        let s = store(Duration::from_secs(60)).await;
        let id = unique_id();
        let rec = capture(&body(json!({"id": id.clone()}))).unwrap().record;
        assert!(s.put_new(&id, &rec).await.unwrap());
        assert_eq!(s.route_once(&id, 5).await.unwrap(), RouteOnce::Recorded);
        assert_eq!(s.route_once(&id, 5).await.unwrap(), RouteOnce::Same);
        assert_eq!(s.route_once(&id, 6).await.unwrap(), RouteOnce::Conflict);
        assert!(s.set_routed(&id, 5).await.unwrap());
        assert!(!s.set_routed(&id, 6).await.unwrap());
        let kept = s.get(&id).await.unwrap().unwrap();
        assert_eq!(kept.routed_community_id, Some(5));
        assert_eq!(kept.token, "aB-_.9", "the rest of the record is untouched");
        assert_eq!(
            s.route_once(&unique_id(), 5).await.unwrap(),
            RouteOnce::Unknown
        );
        assert_eq!(
            s.route_once("not-a-snowflake", 5).await.unwrap(),
            RouteOnce::Unknown
        );
    }

    #[tokio::test]
    async fn concurrent_routers_cannot_both_win() {
        let s = store(Duration::from_secs(60)).await;
        let id = unique_id();
        let rec = capture(&body(json!({"id": id.clone()}))).unwrap().record;
        assert!(s.put_new(&id, &rec).await.unwrap());
        let (a, b) = tokio::join!(s.route_once(&id, 1), s.route_once(&id, 2));
        let outcomes = [a.unwrap(), b.unwrap()];
        assert_eq!(
            outcomes
                .iter()
                .filter(|o| **o == RouteOnce::Recorded)
                .count(),
            1
        );
        assert_eq!(
            outcomes
                .iter()
                .filter(|o| **o == RouteOnce::Conflict)
                .count(),
            1
        );
    }
}
