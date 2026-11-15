//! Host-independent galactic-avatars logic.
//!
//! A community-scoped roster of recently active chatters is kept in the bundle's kv, keyed by a
//! one-way hash of the (already tokenized) actor id -- never a name. The published stage strip
//! carries only a faction and a level per avatar: no names, ids or hashes leave the bundle. The
//! action stage re-validates the frame against the surface contract and publishes it to the
//! constant [`SURFACE`] only.

use serde::{Deserialize, Serialize};

/// The only surface this bundle publishes to (namespaced under its app id).
pub const SURFACE: &str = "waddles.core.app.galactic-avatars.stage_strip";
/// kv key of the roster document (community-scoped by the host).
pub const ROSTER_KEY: &str = "galactic.roster";

/// Contract bounds (`bundles/contracts/surfaces.v1.json`).
pub const MAX_AVATARS: usize = 50;
pub const MAX_LEVEL: u32 = 1000;
/// Messages per level step.
const MESSAGES_PER_LEVEL: u32 = 5;
/// Minimum gap between publishes unless the roster shape changed (contract: 60/min).
const MIN_PUBLISH_GAP_MS: u64 = 2_000;

const FACTIONS: [&str; 4] = ["jedi", "sith", "mandalorian", "droid"];

/// One roster entry: hashed chatter key, message count, last activity.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Member {
    pub k: u64,
    pub n: u32,
    pub t: u64,
}

/// The persisted roster document.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Roster {
    pub members: Vec<Member>,
    pub last_pub_ms: u64,
}

/// One published avatar; mirrors the contract schema exactly.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Avatar {
    pub faction: String,
    pub level: u32,
}

/// The published frame; mirrors the contract schema exactly.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Strip {
    pub avatars: Vec<Avatar>,
    pub updated_ms: u64,
}

impl Strip {
    /// Checks the contract bounds; the action stage calls this before every publish.
    pub fn validate(&self) -> Result<(), String> {
        if self.avatars.len() > MAX_AVATARS {
            return Err("too many avatars".into());
        }
        for a in &self.avatars {
            if !FACTIONS.contains(&a.faction.as_str()) || a.level > MAX_LEVEL {
                return Err("bad avatar".into());
            }
        }
        Ok(())
    }
}

/// Host seam for the kv.
pub trait Kv {
    /// Community-scoped kv read.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    /// Community-scoped kv write (no expiry).
    fn set(&self, key: &str, value: &[u8]) -> Result<(), String>;
}

/// FNV-1a 64-bit hash of the actor id (kept only as an opaque roster key).
fn key_of(actor: &str) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in actor.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

fn faction_of(k: u64) -> &'static str {
    FACTIONS[(k % FACTIONS.len() as u64) as usize]
}

fn strip_of(roster: &Roster, now_ms: u64) -> Strip {
    Strip {
        avatars: roster
            .members
            .iter()
            .map(|m| Avatar {
                faction: faction_of(m.k).to_string(),
                level: (m.n / MESSAGES_PER_LEVEL).min(MAX_LEVEL),
            })
            .collect(),
        updated_ms: now_ms,
    }
}

/// Records one chat message and returns the strip to publish, or `None` (no actor, nothing
/// worth publishing yet). Fails closed on any kv error or corrupt roster document.
pub fn on_chat(kv: &dyn Kv, actor: Option<&str>, now_ms: u64) -> Result<Option<Strip>, String> {
    let Some(actor) = actor.filter(|a| !a.is_empty() && a.len() <= 128) else {
        return Ok(None);
    };
    let mut roster: Roster = match kv
        .get(ROSTER_KEY)
        .map_err(|_| "kv unavailable".to_string())?
    {
        None => Roster::default(),
        Some(bytes) => serde_json::from_slice(&bytes).map_err(|_| "corrupt roster".to_string())?,
    };
    let key = key_of(actor);
    let level_before;
    let is_new;
    if let Some(m) = roster.members.iter_mut().find(|m| m.k == key) {
        level_before = m.n / MESSAGES_PER_LEVEL;
        m.n = m.n.saturating_add(1);
        m.t = now_ms;
        is_new = false;
    } else {
        level_before = 0;
        is_new = true;
        if roster.members.len() >= MAX_AVATARS {
            // Evict the least recently active member.
            if let Some(oldest) = roster
                .members
                .iter()
                .enumerate()
                .min_by_key(|(_, m)| m.t)
                .map(|(i, _)| i)
            {
                roster.members.remove(oldest);
            }
        }
        roster.members.push(Member {
            k: key,
            n: 1,
            t: now_ms,
        });
    }
    let level_after = roster
        .members
        .iter()
        .find(|m| m.k == key)
        .map_or(0, |m| m.n / MESSAGES_PER_LEVEL);
    let changed = is_new || level_after != level_before;
    let due = now_ms.saturating_sub(roster.last_pub_ms) >= MIN_PUBLISH_GAP_MS;
    let publish = changed && due;
    if publish {
        roster.last_pub_ms = now_ms;
    }
    let body = serde_json::to_vec(&roster).map_err(|_| "encode roster".to_string())?;
    kv.set(ROSTER_KEY, &body)
        .map_err(|_| "kv unavailable".to_string())?;
    Ok(publish.then(|| strip_of(&roster, now_ms)))
}

/// Publishing seam (the host `overlay.publish-surface` import in the component).
pub trait Publisher {
    /// Publishes `payload_json` to `surface`; `Err((retryable, code))` on refusal.
    fn publish(&self, surface: &str, payload_json: &str) -> Result<(), (bool, String)>;
}

/// Action stage: parses and re-validates the strip, then publishes it to [`SURFACE`] only.
pub fn dispatch(publisher: &dyn Publisher, payload_json: &str) -> Result<(), (bool, String)> {
    let bad = || (false, "BAD_PAYLOAD".to_string());
    let strip: Strip = serde_json::from_str(payload_json).map_err(|_| bad())?;
    strip.validate().map_err(|_| bad())?;
    let body = serde_json::to_string(&strip).map_err(|_| bad())?;
    publisher.publish(SURFACE, &body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::collections::HashMap;

    #[derive(Default)]
    struct FakeKv {
        map: RefCell<HashMap<String, Vec<u8>>>,
        fail: bool,
    }

    impl Kv for FakeKv {
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            if self.fail {
                return Err("x".into());
            }
            Ok(self.map.borrow().get(key).cloned())
        }
        fn set(&self, key: &str, value: &[u8]) -> Result<(), String> {
            if self.fail {
                return Err("x".into());
            }
            self.map
                .borrow_mut()
                .insert(key.to_string(), value.to_vec());
            Ok(())
        }
    }

    #[derive(Default)]
    struct FakePub {
        calls: RefCell<Vec<(String, String)>>,
    }

    impl Publisher for FakePub {
        fn publish(&self, s: &str, p: &str) -> Result<(), (bool, String)> {
            self.calls.borrow_mut().push((s.to_string(), p.to_string()));
            Ok(())
        }
    }

    #[test]
    fn first_message_publishes_an_anonymous_avatar() {
        let kv = FakeKv::default();
        let strip = on_chat(&kv, Some("actor-uuid-1"), 10_000).unwrap().unwrap();
        assert_eq!(strip.avatars.len(), 1);
        let json = serde_json::to_string(&strip).unwrap();
        assert!(!json.contains("actor-uuid-1"));
        assert!(!kv.map.borrow()[ROSTER_KEY].is_empty());
        strip.validate().unwrap();
    }

    #[test]
    fn publish_is_throttled_and_only_on_change() {
        let kv = FakeKv::default();
        assert!(on_chat(&kv, Some("a"), 10_000).unwrap().is_some());
        // Same chatter, no level change: nothing to publish.
        assert!(on_chat(&kv, Some("a"), 20_000).unwrap().is_none());
        // New chatter inside the throttle window: stored but not published yet.
        assert!(on_chat(&kv, Some("b"), 10_500).unwrap().is_none());
        // New chatter after the window publishes both.
        let s = on_chat(&kv, Some("c"), 30_000).unwrap().unwrap();
        assert_eq!(s.avatars.len(), 3);
    }

    #[test]
    fn roster_is_capped_and_evicts_least_recent() {
        let kv = FakeKv::default();
        for i in 0..(MAX_AVATARS as u64 + 10) {
            on_chat(&kv, Some(&format!("actor-{i}")), 10_000 + i * 3_000).unwrap();
        }
        let roster: Roster = serde_json::from_slice(&kv.map.borrow()[ROSTER_KEY]).unwrap();
        assert_eq!(roster.members.len(), MAX_AVATARS);
        let newest = key_of(&format!("actor-{}", MAX_AVATARS as u64 + 9));
        assert!(roster.members.iter().any(|m| m.k == newest));
        assert!(!roster.members.iter().any(|m| m.k == key_of("actor-0")));
    }

    #[test]
    fn no_actor_means_nothing_and_kv_failures_fail_closed() {
        let kv = FakeKv::default();
        assert!(on_chat(&kv, None, 1).unwrap().is_none());
        assert!(on_chat(&kv, Some(""), 1).unwrap().is_none());
        let bad = FakeKv {
            fail: true,
            ..Default::default()
        };
        assert!(on_chat(&bad, Some("a"), 1).is_err());
        let corrupt = FakeKv::default();
        corrupt
            .map
            .borrow_mut()
            .insert(ROSTER_KEY.into(), b"{not json".to_vec());
        assert_eq!(
            on_chat(&corrupt, Some("a"), 1).unwrap_err(),
            "corrupt roster"
        );
    }

    #[test]
    fn dispatch_publishes_only_to_own_surface() {
        let p = FakePub::default();
        let strip = Strip {
            avatars: vec![Avatar {
                faction: "jedi".into(),
                level: 3,
            }],
            updated_ms: 1,
        };
        dispatch(&p, &serde_json::to_string(&strip).unwrap()).unwrap();
        assert_eq!(p.calls.borrow()[0].0, SURFACE);
    }

    /// Negative: forged frames (extra fields, names, bad faction/level, too many) never publish.
    #[test]
    fn dispatch_rejects_forged_payloads() {
        let p = FakePub::default();
        let many: Vec<String> = (0..51)
            .map(|_| r#"{"faction":"jedi","level":1}"#.to_string())
            .collect();
        let bad = [
            r#"{"avatars":[{"faction":"jedi","level":1,"name":"someone"}],"updated_ms":1}"#
                .to_string(),
            r#"{"avatars":[{"faction":"hutt","level":1}],"updated_ms":1}"#.to_string(),
            r#"{"avatars":[{"faction":"jedi","level":1001}],"updated_ms":1}"#.to_string(),
            r#"{"avatars":[],"updated_ms":1,"surface":"alert"}"#.to_string(),
            format!(r#"{{"avatars":[{}],"updated_ms":1}}"#, many.join(",")),
            "not json".to_string(),
        ];
        for b in bad {
            assert_eq!(
                dispatch(&p, &b).unwrap_err(),
                (false, "BAD_PAYLOAD".to_string()),
                "{b}"
            );
        }
        assert!(p.calls.borrow().is_empty());
    }
}
