//! Generic, platform-agnostic self-echo filter at the event sink.
//!
//! A bot that posts into the chat it also reads receives its own message back. Without a drop the
//! bot's replies re-enter the pipeline as user messages (command loops, reputation farming, double
//! counting). The frozen `connector` WIT gives a guest no self identity, so the HOST drops the
//! bot's own inbound `message` events before they reach the spine, for EVERY platform that
//! normalizes to the shared chat shape (Twitch/Kick publish `payload.user_id`; Discord/YouTube
//! publish `payload.author_id`).
//!
//! **Config per binding.** The bot's own platform user id is non-secret binding metadata
//! (`self_user_id`; several ids may be comma-separated, at most [`MAX_SELF_IDS`]). A binding that
//! does not carry it is simply not filtered (opt-in; absence never drops anything). Invalid
//! entries are ignored with a count, never echoed. The registry is rebuilt from every COMPLETE
//! binding discovery ([`SelfEchoRegistry::replace_platform`]), so a metadata change takes effect on
//! the next reconcile without restarting the connection.
//!
//! Logs and metrics carry only the platform label and numeric community id, never an author id.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, LazyLock, RwLock};

use super::feed::ConnKey;
use crate::connector::ConnectorEvent;

/// Binding metadata key: the bot's own platform user id(s), comma-separated.
pub const META_SELF_USER_ID: &str = "self_user_id";
/// Most ids one binding may list.
pub const MAX_SELF_IDS: usize = 8;
/// Longest accepted id (platform ids are far shorter).
const MAX_ID_LEN: usize = 64;
/// The only event type that is filtered.
const MESSAGE: &str = "message";
/// Payload keys a normalizer publishes the author's platform id under.
const AUTHOR_KEYS: [&str; 2] = ["user_id", "author_id"];

static DROPPED: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_self_echo_dropped_total",
            "Inbound chat messages authored by the bound bot identity, dropped before publish, by platform",
        ),
        &["platform"],
    )
    .expect("valid metric definition")
});

/// Registers the drop counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(DROPPED.clone()))
}

/// Drops counted for `platform` (test/observability helper).
#[must_use]
pub fn dropped_count(platform: &str) -> u64 {
    DROPPED.with_label_values(&[platform]).get()
}

/// `true` for a plausible platform user id (`[A-Za-z0-9_.-]`, bounded).
fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= MAX_ID_LEN
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
}

/// The valid self ids a binding's metadata names (empty when it names none). Invalid or surplus
/// entries are skipped and counted in the returned `skipped`.
#[must_use]
pub fn parse_ids(meta: &BTreeMap<String, String>) -> (HashSet<String>, usize) {
    let mut out = HashSet::new();
    let mut skipped = 0;
    let Some(raw) = meta.get(META_SELF_USER_ID) else {
        return (out, 0);
    };
    for part in raw.split(',').map(str::trim).filter(|p| !p.is_empty()) {
        if valid_id(part) && out.len() < MAX_SELF_IDS {
            out.insert(part.to_owned());
        } else {
            skipped += 1;
        }
    }
    (out, skipped)
}

/// The author id of a chat-message payload, whether the normalizer wrote a string or a number.
fn author_of(payload: &serde_json::Value) -> Option<String> {
    AUTHOR_KEYS.iter().find_map(|k| match payload.get(*k)? {
        serde_json::Value::String(s) => Some(s.trim().to_owned()),
        serde_json::Value::Number(n) => Some(n.to_string()),
        _ => None,
    })
}

type Table = HashMap<ConnKey, Arc<HashSet<String>>>;

/// Shared, live map from a connection to its bot identity set. Cheap to clone (one `Arc`).
#[derive(Clone, Default)]
pub struct SelfEchoRegistry {
    table: Arc<RwLock<Table>>,
}

impl SelfEchoRegistry {
    /// An empty registry (filters nothing).
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Atomically replaces every entry of `platform` with `entries` (a connection that left the
    /// binding list, or lost its `self_user_id`, stops being filtered). Entries with no ids are
    /// not stored.
    pub fn replace_platform(&self, platform: &str, entries: Vec<(ConnKey, HashSet<String>)>) {
        let mut table = self
            .table
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        table.retain(|k, _| k.platform != platform);
        for (key, ids) in entries {
            if !ids.is_empty() && key.platform == platform {
                table.insert(key, Arc::new(ids));
            }
        }
    }

    /// The identity set for `key`, if one is configured.
    #[must_use]
    pub fn ids_for(&self, key: &ConnKey) -> Option<Arc<HashSet<String>>> {
        self.table
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(key)
            .cloned()
    }

    /// `true` (and counted) when `event` is a chat message authored by one of `key`'s bot ids.
    /// Anything that is not a `message`, has no recognizable author, or has no configured
    /// identity passes.
    #[must_use]
    pub fn is_self_echo(&self, key: &ConnKey, event: &ConnectorEvent) -> bool {
        if event.event_type != MESSAGE {
            return false;
        }
        let Some(ids) = self.ids_for(key) else {
            return false;
        };
        let Ok(payload) = serde_json::from_str::<serde_json::Value>(&event.payload_json) else {
            return false;
        };
        let hit = author_of(&payload).is_some_and(|a| ids.contains(&a));
        if hit {
            DROPPED.with_label_values(&[key.platform]).inc();
            tracing::debug!(
                platform = key.platform,
                community_id = key.community_id,
                "self-echo message dropped before publish"
            );
        }
        hit
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn key(platform: &'static str, community: i64, conn: i64) -> ConnKey {
        ConnKey {
            platform,
            community_id: community,
            connection_id: conn,
            lane: Default::default(),
        }
    }

    fn msg(platform: &str, event_type: &str, payload: &str) -> ConnectorEvent {
        ConnectorEvent {
            platform: platform.into(),
            event_type: event_type.into(),
            actor: Some("11111111-2222-3333-4444-555555555555".into()),
            payload_json: payload.into(),
            occurred_at: "2026-11-07T00:00:00.000Z".into(),
        }
    }

    fn meta(v: &str) -> BTreeMap<String, String> {
        [(META_SELF_USER_ID.to_string(), v.to_string())].into()
    }

    fn ids(v: &[&str]) -> HashSet<String> {
        v.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn parse_keeps_valid_ids_and_counts_the_rest_without_echoing_them() {
        assert_eq!(parse_ids(&BTreeMap::new()), (HashSet::new(), 0));
        assert_eq!(parse_ids(&meta("123")), (ids(&["123"]), 0));
        assert_eq!(parse_ids(&meta(" 1 , 2,,3 ")), (ids(&["1", "2", "3"]), 0));
        let (got, skipped) = parse_ids(&meta("ok,bad id,<x>,UC_a-b.c"));
        assert_eq!(got, ids(&["ok", "UC_a-b.c"]));
        assert_eq!(skipped, 2);
        let too_long = "9".repeat(MAX_ID_LEN + 1);
        assert_eq!(parse_ids(&meta(&too_long)), (HashSet::new(), 1));
        let many = (0..MAX_SELF_IDS + 3)
            .map(|i| i.to_string())
            .collect::<Vec<_>>()
            .join(",");
        let (got, skipped) = parse_ids(&meta(&many));
        assert_eq!((got.len(), skipped), (MAX_SELF_IDS, 3));
    }

    /// Every platform's chat shape: Twitch/Kick `user_id` (string), Discord/YouTube `author_id`,
    /// and a numeric author id.
    #[test]
    fn the_bots_own_message_is_dropped_on_every_platform_shape() {
        let reg = SelfEchoRegistry::new();
        let cases = [
            ("twitch", r#"{"user_id":"4242","text":"hi"}"#),
            ("kick", r#"{"user_id":"4242","text":"hi"}"#),
            ("discord", r#"{"author_id":"4242","text":"hi"}"#),
            ("youtube", r#"{"author_id":"4242","text":"hi"}"#),
            ("kick", r#"{"user_id":4242,"text":"hi"}"#),
        ];
        for (p, payload) in cases {
            let k = key(p, 7, 9);
            reg.replace_platform(p, vec![(k.clone(), ids(&["4242"]))]);
            let before = dropped_count(p);
            assert!(
                reg.is_self_echo(&k, &msg(p, "message", payload)),
                "{p} {payload}"
            );
            assert_eq!(dropped_count(p), before + 1, "{p}");
            // A different author is kept.
            let other = payload.replace("4242", "5151");
            assert!(
                !reg.is_self_echo(&k, &msg(p, "message", &other)),
                "{p} {other}"
            );
        }
    }

    #[test]
    fn only_messages_with_a_matching_author_are_dropped() {
        let reg = SelfEchoRegistry::new();
        let k = key("twitch", 7, 9);
        reg.replace_platform("twitch", vec![(k.clone(), ids(&["4242"]))]);
        let own = r#"{"user_id":"4242"}"#;
        for t in [
            "channel.follow",
            "channel.subscribe",
            "stream.online",
            "message.edit",
        ] {
            assert!(!reg.is_self_echo(&k, &msg("twitch", t, own)), "{t}");
        }
        for payload in [
            "{}",
            r#"{"user_id":null}"#,
            r#"{"user_id":true}"#,
            "not json",
            "[1]",
        ] {
            assert!(
                !reg.is_self_echo(&k, &msg("twitch", "message", payload)),
                "{payload}"
            );
        }
        // Exact match: a prefix/superstring/case variant is a different user.
        for a in ["424", "42420", " 4243", "4242x"] {
            let p = format!(r#"{{"user_id":"{a}"}}"#);
            assert!(!reg.is_self_echo(&k, &msg("twitch", "message", &p)), "{a}");
        }
    }

    #[test]
    fn identities_are_per_connection_and_per_platform() {
        let reg = SelfEchoRegistry::new();
        let (a, b, c) = (key("kick", 7, 9), key("kick", 8, 10), key("discord", 7, 11));
        reg.replace_platform(
            "kick",
            vec![(a.clone(), ids(&["1"])), (b.clone(), ids(&["2"]))],
        );
        reg.replace_platform("discord", vec![(c.clone(), ids(&["1"]))]);
        let own1 = msg("kick", "message", r#"{"user_id":"1"}"#);
        assert!(reg.is_self_echo(&a, &own1));
        assert!(
            !reg.is_self_echo(&b, &own1),
            "another community's bot id is not ours"
        );
        // An unconfigured connection is never filtered.
        assert!(!reg.is_self_echo(&key("kick", 9, 99), &own1));
        // Replacing one platform leaves the other alone.
        reg.replace_platform("kick", vec![]);
        assert!(reg.ids_for(&a).is_none() && reg.ids_for(&b).is_none());
        assert!(reg.ids_for(&c).is_some());
    }

    #[test]
    fn a_rediscovery_updates_the_set_live_and_ignores_foreign_platform_entries() {
        let reg = SelfEchoRegistry::new();
        let k = key("kick", 7, 9);
        reg.replace_platform("kick", vec![(k.clone(), ids(&["1"]))]);
        reg.replace_platform("kick", vec![(k.clone(), ids(&["2"]))]);
        assert!(!reg.is_self_echo(&k, &msg("kick", "message", r#"{"user_id":"1"}"#)));
        assert!(reg.is_self_echo(&k, &msg("kick", "message", r#"{"user_id":"2"}"#)));
        // An entry keyed under another platform is not stored by `replace_platform("kick")`.
        reg.replace_platform("kick", vec![(key("discord", 7, 9), ids(&["1"]))]);
        assert!(reg.ids_for(&key("discord", 7, 9)).is_none());
        // Empty id sets are not stored.
        reg.replace_platform("kick", vec![(k.clone(), HashSet::new())]);
        assert!(reg.ids_for(&k).is_none());
    }
}
