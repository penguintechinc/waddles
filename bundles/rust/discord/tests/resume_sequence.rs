//! Resume-sequence tests: a scripted multi-connection session in which the test
//! plays the HOST (the party that owns the session token), driving the connector
//! through IDENTIFY -> READY -> dispatches -> disconnect -> RESUME ->
//! INVALID_SESSION -> IDENTIFY, and asserting no step double-identifies.

use serde_json::Value;
use waddle_bundle_discord_overlay::error::Result;
use waddle_bundle_discord_overlay::gateway::{Lifecycle, classify, parse_frame};
use waddle_bundle_discord_overlay::perms::GuildInfo;
use waddle_bundle_discord_overlay::receiver::{
    ConnCtx, Host, Level, on_connect, on_frame, on_heartbeat_due,
};
use waddle_bundle_discord_overlay::session::SessionState;

struct NoRest;
impl Host for NoRest {
    fn guild(&self, _: &str) -> Result<GuildInfo> {
        Ok(GuildInfo {
            owner_id: "900".into(),
            roles: vec![],
        })
    }
    fn member_roles(&self, _: &str, _: &str) -> Result<Vec<String>> {
        Ok(vec![])
    }
    fn identity(&self, id: &str) -> Result<String> {
        Ok(format!("uuid-of-{id}"))
    }
    fn log(&self, _: Level, _: &str, _: &str) {}
}

fn ctx(state: &SessionState) -> ConnCtx {
    let token = if *state == SessionState::default() {
        None
    } else {
        Some(state.to_token().unwrap())
    };
    ConnCtx {
        platform: "discord".into(),
        source_id: "src".into(),
        connection_id: "conn".into(),
        session_token: token,
    }
}

fn op(bytes: &[u8]) -> u64 {
    serde_json::from_slice::<Value>(bytes).unwrap()["op"]
        .as_u64()
        .unwrap()
}

/// What the host does with a frame: persist READY, advance `seq`, clear on a
/// non-resumable INVALID_SESSION. (The connector's `classify` is the single source
/// of truth for what each frame means.)
fn host_apply(state: &mut SessionState, raw: &str) {
    let f = parse_frame(raw.as_bytes()).unwrap();
    if let Some(s) = f.s {
        state.seq = Some(s);
    }
    match classify(&f).unwrap() {
        Lifecycle::Ready(r) => {
            state.session_id = r.session_id;
            state.resume_gateway_url = r.resume_gateway_url;
            state.self_user_id = r.self_user_id;
        }
        Lifecycle::InvalidSession { resumable: false } => *state = SessionState::default(),
        _ => {}
    }
}

#[test]
fn identify_then_resume_then_fall_back_to_identify() {
    let h = NoRest;
    let mut state = SessionState::default();
    let mut identifies = 0;

    // Connection 1: fresh -> IDENTIFY.
    let hs = on_connect(&h, &ctx(&state)).unwrap();
    assert_eq!(op(&hs.bytes), 2);
    identifies += 1;

    for raw in [
        r#"{"op":10,"d":{"heartbeat_interval":41250}}"#,
        r#"{"op":0,"s":1,"t":"READY","d":{"session_id":"sess1","resume_gateway_url":"wss://resume","user":{"id":"999"}}}"#,
        r#"{"op":0,"s":2,"t":"GUILD_CREATE","d":{"id":"111"}}"#,
    ] {
        assert!(
            on_frame(&h, &ctx(&state), raw.as_bytes(), "t")
                .unwrap()
                .is_empty()
        );
        host_apply(&mut state, raw);
    }
    // A message from another user, then one from the bot itself (filtered via READY's id).
    let mc = |s: u64, a: &str| {
        format!(
            r#"{{"op":0,"s":{s},"t":"MESSAGE_CREATE","d":{{"author":{{"id":"{a}","username":"u"}},"channel_id":"2","id":"{s}","content":"x","guild_id":"111"}}}}"#
        )
    };
    assert_eq!(
        on_frame(&h, &ctx(&state), mc(3, "444").as_bytes(), "t")
            .unwrap()
            .len(),
        1
    );
    host_apply(&mut state, &mc(3, "444"));
    assert!(
        on_frame(&h, &ctx(&state), mc(4, "999").as_bytes(), "t")
            .unwrap()
            .is_empty()
    );
    host_apply(&mut state, &mc(4, "999"));
    assert_eq!(state.seq, Some(4));

    // The heartbeat carries the tracked sequence.
    let hb = on_heartbeat_due(&ctx(&state)).unwrap().unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&hb).unwrap()["d"], 4);

    // Connection 2 (transport dropped): RESUME with session + seq, NOT an IDENTIFY.
    let hs = on_connect(&h, &ctx(&state)).unwrap();
    let v: Value = serde_json::from_slice(&hs.bytes).unwrap();
    assert_eq!(
        (
            v["op"].as_u64(),
            v["d"]["session_id"].as_str(),
            v["d"]["seq"].as_u64()
        ),
        (Some(6), Some("sess1"), Some(4))
    );

    // Reconnect again before any new dispatch: still RESUME (idempotent, same seq).
    let again: Value =
        serde_json::from_slice(&on_connect(&h, &ctx(&state)).unwrap().bytes).unwrap();
    assert_eq!(again, v);

    // Discord says the session is dead (op 9 d=false): host clears, next connect IDENTIFYs.
    let dead = r#"{"op":9,"d":false}"#;
    assert!(
        on_frame(&h, &ctx(&state), dead.as_bytes(), "t")
            .unwrap()
            .is_empty()
    );
    host_apply(&mut state, dead);
    assert_eq!(state, SessionState::default());
    let hs = on_connect(&h, &ctx(&state)).unwrap();
    assert_eq!(op(&hs.bytes), 2);
    identifies += 1;

    // Exactly two IDENTIFYs across the whole sequence (start + after the dead session).
    assert_eq!(identifies, 2);
}

#[test]
fn resumable_invalid_session_keeps_the_session_for_one_more_resume() {
    let mut state = SessionState {
        session_id: Some("s".into()),
        seq: Some(10),
        ..SessionState::default()
    };
    host_apply(&mut state, r#"{"op":9,"d":true}"#);
    assert_eq!(state.session_id.as_deref(), Some("s"));
    let v: Value =
        serde_json::from_slice(&on_connect(&NoRest, &ctx(&state)).unwrap().bytes).unwrap();
    assert_eq!(
        (v["op"].as_u64(), v["d"]["seq"].as_u64()),
        (Some(6), Some(10))
    );
}

#[test]
fn the_credential_never_appears_in_any_handshake() {
    let h = NoRest;
    for st in [
        SessionState::default(),
        SessionState {
            session_id: Some("s".into()),
            seq: Some(1),
            ..SessionState::default()
        },
    ] {
        let hs = on_connect(&h, &ctx(&st)).unwrap();
        let text = String::from_utf8(hs.bytes).unwrap();
        assert_eq!(text.matches("{{DISCORD_BOT_TOKEN}}").count(), 1);
        assert_eq!(hs.secret_refs.len(), 1);
        assert_eq!(hs.secret_refs[0].1, "discord-bot");
    }
}
