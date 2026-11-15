//! Parity with the legacy Python action module
//! (`action/pushing/twitch_action_module/services/twitch_service.py`: `ban_user`, `unban_user`,
//! `delete_chat_message`): after the host fills the slots, the connector's templates must hit the
//! same Helix method / path / query / body. The one intended difference is who moderates: the
//! legacy code used the broadcaster as `moderator_id`; the host policy binds the moderator to the
//! community's bot connection (`TWITCH_SENDER_ID`). The test fills both slots with the same id to
//! compare shapes like for like.

use serde_json::{Value, json};
use waddle_bundle_twitch_overlay::sender::build_request;

const BROADCASTER: &str = "111";

/// Fills the host slots exactly as the executor would (one id for both, as in the legacy code).
fn fill(s: &str) -> String {
    s.replace("{{TWITCH_BROADCASTER_ID}}", BROADCASTER)
        .replace("{{TWITCH_SENDER_ID}}", BROADCASTER)
}

/// `(method, path, sorted query pairs, body)` of a filled template.
fn shape(kind: &str, payload: Value) -> (String, String, Vec<(String, String)>, Option<Value>) {
    let t = build_request(kind, "community-1", &payload.to_string()).unwrap();
    let url = fill(&t.url);
    let rest = url.strip_prefix("https://api.twitch.tv/helix").unwrap();
    let (path, q) = rest.split_once('?').unwrap();
    let mut query: Vec<(String, String)> = q
        .split('&')
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap();
            (k.to_string(), v.to_string())
        })
        .collect();
    query.sort();
    let body = t
        .body
        .map(|b| serde_json::from_str(&fill(&String::from_utf8(b).unwrap())).unwrap());
    (t.method, path.to_string(), query, body)
}

fn q(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    let mut v: Vec<_> = pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
    v.sort();
    v
}

#[test]
fn ban_matches_legacy_ban_user_without_duration() {
    let got = shape(
        "mod.ban",
        json!({"user_id":"42","text":json!({"reason":"spam"}).to_string()}),
    );
    assert_eq!(
        got,
        (
            "POST".to_string(),
            "/moderation/bans".to_string(),
            q(&[("broadcaster_id", "111"), ("moderator_id", "111")]),
            Some(json!({"data":{"user_id":"42","reason":"spam"}})),
        )
    );
}

#[test]
fn timeout_matches_legacy_ban_user_with_duration() {
    let got = shape(
        "mod.timeout",
        json!({"user_id":"42","text":json!({"reason":"slow down","duration":600}).to_string()}),
    );
    assert_eq!(
        got,
        (
            "POST".to_string(),
            "/moderation/bans".to_string(),
            q(&[("broadcaster_id", "111"), ("moderator_id", "111")]),
            Some(json!({"data":{"user_id":"42","reason":"slow down","duration":600}})),
        )
    );
}

#[test]
fn unban_matches_legacy_unban_user() {
    let got = shape("mod.unban", json!({"user_id":"42"}));
    assert_eq!(
        got,
        (
            "DELETE".to_string(),
            "/moderation/bans".to_string(),
            q(&[
                ("broadcaster_id", "111"),
                ("moderator_id", "111"),
                ("user_id", "42")
            ]),
            None,
        )
    );
}

#[test]
fn delete_matches_legacy_delete_chat_message() {
    let got = shape("chat.delete", json!({"message_id":"m-1"}));
    assert_eq!(
        got,
        (
            "DELETE".to_string(),
            "/moderation/chat".to_string(),
            q(&[
                ("broadcaster_id", "111"),
                ("moderator_id", "111"),
                ("message_id", "m-1")
            ]),
            None,
        )
    );
}
