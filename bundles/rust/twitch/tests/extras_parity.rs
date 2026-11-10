//! Parity with the legacy Python paths: `twitch_service.py::create_clip` (`POST /clips` with only
//! `broadcaster_id`) and `eventsub_handler.py::_create_subscription` (body shape: type, version,
//! condition, webhook transport; `channel.follow` adds `moderator_user_id`). After the host fills
//! its slots the connector's templates must hit the same Helix method / path / query / body.
//! Intended differences: `channel.follow` is v2 (v1 is retired upstream), `channel.raid` names
//! `to_broadcaster_user_id`, and the callback/secret are host-filled, never bundle values.

use serde_json::{Value, json};
use waddle_bundle_twitch_overlay::eventsub_sub::SUPPORTED;
use waddle_bundle_twitch_overlay::sender::build_request;

fn fill(s: &str) -> String {
    s.replace("{{TWITCH_BROADCASTER_ID}}", "111")
        .replace("{{TWITCH_APP_BROADCASTER_ID}}", "111")
        .replace("{{TWITCH_EVENTSUB_CALLBACK}}", "https://cb.example/hook")
        .replace("{{TWITCH_EVENTSUB_SECRET}}", "s3cret")
}

#[test]
fn clip_create_matches_the_legacy_request() {
    let t = build_request("clip.create", "community-1", "{}").unwrap();
    assert_eq!(t.method, "POST");
    assert_eq!(
        fill(&t.url),
        "https://api.twitch.tv/helix/clips?broadcaster_id=111"
    );
    assert!(t.body.is_none());
}

#[test]
fn eventsub_create_matches_the_legacy_body_for_every_type() {
    let mut n = 0;
    for (ty, ver) in SUPPORTED {
        let p = json!({"text": json!({"type": ty, "version": ver}).to_string()});
        let t = build_request("eventsub.create", "community-1", &p.to_string()).unwrap();
        let body: Value =
            serde_json::from_str(&fill(&String::from_utf8(t.body.unwrap()).unwrap())).unwrap();
        let mut condition = json!({"broadcaster_user_id": "111"});
        match *ty {
            "channel.follow" => condition["moderator_user_id"] = json!("111"),
            "channel.raid" => condition = json!({"to_broadcaster_user_id": "111"}),
            _ => {}
        }
        assert_eq!(
            body,
            json!({
                "type": ty,
                "version": ver,
                "condition": condition,
                "transport": {"method": "webhook", "callback": "https://cb.example/hook", "secret": "s3cret"},
            }),
            "{ty}"
        );
        n += 1;
    }
    assert_eq!(n, 7, "denominator: the seven legacy event types");
}
