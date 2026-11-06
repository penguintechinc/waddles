//! Golden parity: every `tests/data_plane_parity/corpus/twitch/*.json` vector
//! through the connector's logic must equal the hardcoded path's golden
//! (event_type, actor, payload). `source` is host-derived and not compared.
//! `occurred_at` is host-stamped and passed through.

use std::fs;
use std::path::Path;

use serde_json::Value;
use waddle_bundle_twitch_overlay::{eventsub, irc};

const TS: &str = "2026-11-05T00:00:00.000Z";

fn vectors() -> Vec<Value> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/data_plane_parity/corpus/twitch");
    let mut out: Vec<_> = fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("read {dir:?}: {e}"))
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    out.sort();
    out.iter()
        .map(|p| serde_json::from_str(&fs::read_to_string(p).unwrap()).unwrap())
        .collect()
}

/// Runs a vector the way the host would: `self_nick_known` models a host that
/// supplies the bot nick (the guest entry point cannot -- see README).
fn run(v: &Value, self_nick_known: bool) -> Vec<irc_or_es::Ev> {
    match v["path"].as_str().unwrap() {
        "twitch_irc" => {
            let nick = self_nick_known.then(|| v["context"]["self_nick"].as_str().unwrap());
            irc::normalize_frame(v["frame"].as_str().unwrap().as_bytes(), TS, nick)
                .unwrap()
                .into_iter()
                .map(irc_or_es::Ev::from)
                .collect()
        }
        "twitch_eventsub" => eventsub::normalize_eventsub(v["frame"].to_string().as_bytes(), TS)
            .unwrap()
            .into_iter()
            .map(irc_or_es::Ev::from)
            .collect(),
        p => panic!("unknown path {p}"),
    }
}

mod irc_or_es {
    use waddle_bundle_twitch_overlay::common::NormEvent;

    pub struct Ev(pub NormEvent);
    impl From<NormEvent> for Ev {
        fn from(e: NormEvent) -> Self {
            Ev(e)
        }
    }
}

/// The identity key the shared harness expects (`ACTOR_ID_FIELD` in
/// scripts/ci/gen-connector-parity-expected.py): payload `user_id` (IRC) or
/// `user_id`/`broadcaster_id` (EventSub), else the golden login when the wire
/// carries no id; `null` for a system event. The RAW login is never published:
/// the host resolves this key through `identity.lookup`.
fn expected_actor_key(v: &Value) -> Value {
    let g = &v["golden"];
    if g["actor"].is_null() {
        return Value::Null;
    }
    let fields: &[&str] = if v["path"] == "twitch_irc" {
        &["user_id"]
    } else {
        &["user_id", "broadcaster_id"]
    };
    fields
        .iter()
        .find_map(|f| g["payload"].get(*f).filter(|x| !x.is_null()).cloned())
        .unwrap_or_else(|| g["actor"].clone())
}

/// Declared divergences applied to the golden payload: `(vector id, payload
/// field, expected connector value)`. Mirrors tests/data_plane_parity/expected/
/// twitch/divergences.json; each must actually differ from the golden (a stale
/// declaration fails). The hardcoded path published an EMPTY broadcaster id for
/// a raid whose channel is only in `to_broadcaster_user_id`.
const DIVERGENCES: &[(&str, &str, &str)] = &[(
    "twitch/eventsub_raid_documented_condition",
    "broadcaster_id",
    "111",
)];

fn golden_payload(v: &Value) -> Value {
    let mut g = v["golden"]["payload"].clone();
    for (id, field, expected) in DIVERGENCES {
        if v["id"] == *id {
            assert_ne!(g[*field], *expected, "stale divergence {id}/{field}");
            g[*field] = Value::String((*expected).to_string());
        }
    }
    g
}

fn check(v: &Value, self_nick_known: bool) -> Result<(), String> {
    let id = v["id"].as_str().unwrap();
    let got = run(v, self_nick_known);
    match v["outcome"].as_str().unwrap() {
        "dropped" => {
            if got.is_empty() {
                Ok(())
            } else {
                Err(format!("{id}: expected drop, got event"))
            }
        }
        _ => {
            if got.len() != 1 {
                return Err(format!("{id}: expected 1 event, got {}", got.len()));
            }
            let e = &got[0].0;
            let g = &v["golden"];
            if g["platform"] != "twitch"
                || Value::String(e.event_type.clone()) != g["event_type"]
                || serde_json::to_value(&e.actor_key).unwrap() != expected_actor_key(v)
                || Value::Object(e.payload.clone()) != golden_payload(v)
                || e.occurred_at != TS
            {
                return Err(format!(
                    "{id}: mismatch\n golden {g}\n want {} {}\n actual {:?} {:?}",
                    golden_payload(v),
                    expected_actor_key(v),
                    e.actor_key,
                    e.payload
                ));
            }
            Ok(())
        }
    }
}

#[test]
fn connector_matches_every_twitch_golden_vector() {
    let vs = vectors();
    assert_eq!(
        vs.len(),
        23,
        "corpus must hold exactly the 23 twitch vectors"
    );
    let fails: Vec<String> = vs.iter().filter_map(|v| check(v, true).err()).collect();
    println!("parity: {}/{} pass", vs.len() - fails.len(), vs.len());
    assert!(fails.is_empty(), "{}", fails.join("\n"));
}

/// The guest entry point has no self identity: only the self-echo vector may
/// differ, and it must differ in exactly that way (documented gap).
#[test]
fn guest_path_without_self_nick_differs_only_on_self_echo() {
    let vs = vectors();
    let fails: Vec<String> = vs.iter().filter_map(|v| check(v, false).err()).collect();
    assert_eq!(fails.len(), 1, "{fails:?}");
    assert!(fails[0].contains("irc_self_message_dropped"));
}

/// Every published actor is the identity.lookup UUID, never the raw login.
#[test]
fn published_actor_is_the_lookup_uuid_never_the_raw_login() {
    use waddle_bundle_twitch_overlay::common::resolve_actors;
    let mut with_actor = 0;
    for v in vectors() {
        let events: Vec<_> = run(&v, true).into_iter().map(|e| e.0).collect();
        let (resolved, dropped) = resolve_actors(events, &|k| Ok(format!("uuid-for-{k}")));
        assert_eq!(dropped, 0);
        for r in resolved {
            let Some(actor) = r.actor else { continue };
            with_actor += 1;
            let key = expected_actor_key(&v);
            assert_eq!(actor, format!("uuid-for-{}", key.as_str().unwrap()));
            assert_ne!(
                Value::String(actor),
                v["golden"]["actor"],
                "raw handle leaked"
            );
        }
    }
    assert!(with_actor > 0, "zero actors examined is a failure");
}

/// Lookup failure drops every event (fail closed); nothing reaches the sink.
#[test]
fn identity_lookup_failure_drops_everything_with_no_raw_fallback() {
    use waddle_bundle_twitch_overlay::common::resolve_actors;
    let mut examined = 0;
    for v in vectors()
        .iter()
        .filter(|v| v["outcome"] == "event" && !v["golden"]["actor"].is_null())
    {
        let events: Vec<_> = run(v, true).into_iter().map(|e| e.0).collect();
        examined += events.len();
        let (resolved, dropped) = resolve_actors(events, &|_| Err(()));
        assert!(resolved.is_empty());
        assert!(dropped >= 1);
    }
    assert!(examined > 0);
}
