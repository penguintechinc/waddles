//! Moderator surface, usage/error branches, model sanitization and provider
//! failure mapping.

mod common;

use common::*;
use serde_json::json;
use waddle_bundle_dj_app::engine::{Ctx, Engine};
use waddle_bundle_dj_app::host::ToolErr;
use waddle_bundle_dj_app::model::{Mode, Outcome, Status, Track};
use waddle_bundle_dj_app::music;
use waddle_bundle_dj_app::policy::Config;

fn host() -> FakeHost {
    let h = FakeHost::new();
    h.add(track(
        "youtube",
        "yt1",
        "Alpha Song",
        "Artist One",
        "UC_A",
        200,
    ));
    h.add(track(
        "youtube",
        "yt2",
        "Beta Song",
        "Artist Two",
        "UC_B",
        210,
    ));
    h.add(track(
        "spotify",
        "sp1",
        "Gamma Song",
        "Artist Three",
        "",
        220,
    ));
    h
}

fn m(h: &FakeHost, t: &str) -> String {
    say(h, &user(USER_B, true, "mm"), t)
}

fn cfg(h: &FakeHost) -> Config {
    Engine::new(h, &Ctx::default()).config().unwrap()
}

#[test]
fn usage_and_unknown_branches_reply_helpfully() {
    let h = host();
    for (cmd, needle) in [
        ("!dj", "requests"),
        ("!dj bogus", "usage: !dj"),
        ("!dj set", "usage: !dj set"),
        ("!dj list", "usage: !dj list"),
        ("!dj list deny-artist poke x", "expected add or remove"),
        ("!dj list nonsense add xx", "list must be"),
        ("!dj list deny-artist add x", "too short"),
        ("!dj mode", "usage: !dj mode"),
        ("!dj ai", "usage: !dj ai"),
        ("!dj ai genre", "usage: !dj ai genre"),
        ("!dj ai instructions", "usage: !dj ai instructions"),
        ("!dj policy show", "active: none | pending: none"),
        ("!dj policy confirm", "no pending policy"),
        ("!dj policy zzz", "usage: !dj policy"),
        ("!dj audit", "no audit rows"),
        ("!dj unban", "usage: !dj unban"),
        ("!dj unban youtube/none", "not banned"),
        ("!music bogus", "usage: !music"),
        ("!music now", "nothing is playing"),
        ("!music remove", "usage: !music remove"),
        ("!music remove 9", "no request #9"),
        ("!music approve", "usage: !music approve"),
        ("!music approve 9", "no pending request #9"),
        ("!music ban", "usage: !music ban"),
        ("!music ban 9", "no request #9"),
        ("!music restore", "usage: !music restore"),
        ("!music restore 9", "no removed request #9"),
        ("!music move", "usage: !music move"),
        ("!music move 9 1", "no request #9"),
        ("!dj fill", "AI DJ is off"),
        ("!dj playlist", "usage: !dj playlist"),
        ("!dj playlist list", "no playlists"),
        ("!dj playlist show nope", "no such playlist"),
        ("!dj playlist use nope", "no such playlist"),
        ("!dj playlist delete nope", "no such playlist"),
        ("!dj playlist add nope x", "no such playlist"),
        ("!dj playlist remove nope 1", "no such playlist"),
        ("!dj playlist move nope 1 2", "no such playlist"),
        ("!dj playlist import nope youtube x", "no such playlist"),
        ("!dj playlist create", "1-32 chars"),
    ] {
        let r = m(&h, cmd);
        assert!(r.contains(needle), "{cmd} -> {r}");
    }
}

#[test]
fn settings_lists_modes_and_ai_toggles_round_trip() {
    let h = host();
    for (k, v) in [
        ("requests", "off"),
        ("explicit", "off"),
        ("dmca_safe", "on"),
        ("approval", "on"),
        ("auto_remove", "on"),
        ("max_duration_s", "300"),
        ("max_queue", "20"),
        ("replay_cooldown_s", "10"),
        ("cost", "5"),
        ("ai_hourly_cap", "3"),
        ("ai_batch", "2"),
        ("providers", "youtube,spotify"),
    ] {
        assert_eq!(m(&h, &format!("!dj set {k} {v}")), format!("{k} updated"));
    }
    assert!(m(&h, "!dj set explicit maybe").contains("expected on|off"));
    let c = cfg(&h);
    assert!(!c.requests_enabled && !c.explicit_allowed && c.dmca_safe && c.approval_required);
    assert_eq!(
        (
            c.max_duration_s,
            c.max_queue,
            c.request_cost,
            c.ai.hourly_cap,
            c.ai.batch
        ),
        (300, 20, 5, 3, 2)
    );
    assert_eq!(c.providers, vec!["youtube", "spotify"]);
    assert!(m(&h, "!dj config").contains("requests off"));
    assert_eq!(
        say(&h, &user(USER_A, false, "x"), "!sr alpha"),
        "song requests are closed"
    );

    for l in [
        "allow-channel",
        "allow-artist",
        "deny-artist",
        "deny-term",
        "deny-track",
    ] {
        assert!(m(&h, &format!("!dj list {l} add Some Value")).contains("updated"));
        assert!(m(&h, &format!("!dj list {l} add some value")).contains("updated"));
        assert!(m(&h, &format!("!dj list {l} remove Some Value")).contains("updated"));
    }
    let c = cfg(&h);
    assert!(c.deny_artists.is_empty() && c.sources_allow.is_empty() && c.deny_tracks.is_empty());

    assert!(m(&h, "!dj ai genre synthwave").contains("genre saved"));
    assert!(m(&h, "!dj ai on").contains("AI DJ is on"));
    assert!(m(&h, "!dj mode ai").contains("mode set"));
    assert_eq!(cfg(&h).mode, Mode::Ai);
    assert!(m(&h, "!dj ai off").contains("off"));
    assert_eq!(cfg(&h).mode, Mode::Requests);
    assert!(m(&h, "!dj mode playlist").contains("mode set"));
    assert!(m(&h, "!dj mode requests").contains("mode set"));
    assert_eq!(cfg(&h).ai.genre, "synthwave");
}

#[test]
fn policy_show_discard_and_clear() {
    let h = host();
    h.ai_complete.borrow_mut().push(r#"{"deny_artists":["artist one"],"explicit_allowed":false,"max_duration_s":250,"allow_providers":["youtube","bogus"],"genres":["edm"]}"#.into());
    let r = m(&h, "!dj ai instructions no artist one");
    assert!(
        r.contains("never artists: artist one")
            && r.contains("no explicit")
            && r.contains("max 250s"),
        "{r}"
    );
    assert!(m(&h, "!dj policy show").contains("pending: only providers: youtube"));
    assert_eq!(m(&h, "!dj policy discard"), "pending policy discarded");
    h.ai_complete
        .borrow_mut()
        .push(r#"{"deny_artists":["artist one"]}"#.into());
    m(&h, "!dj ai instructions again");
    m(&h, "!dj policy confirm");
    assert!(m(&h, "!dj audit 3").contains("policy_confirmed"));
    assert_eq!(
        say(&h, &user(USER_A, false, "x"), "!sr alpha song"),
        "request #1 removed: that artist is not allowed here"
    );
    assert_eq!(m(&h, "!dj policy clear"), "active policy cleared");
    assert!(say(&h, &user(USER_A, false, "y"), "!sr alpha song").starts_with("queued"));
}

#[test]
fn ai_compile_failure_modes_surface_the_cause() {
    let h = host();
    h.fail_tools.borrow_mut().push("ai.".into());
    assert!(m(&h, "!dj ai instructions x y").contains("unavailable"));
}

#[test]
fn queue_moderation_move_clear_restore_and_ban_edge_cases() {
    let h = host();
    h.add(track(
        "youtube",
        "yt3",
        "Long One",
        "Artist Four",
        "UC_C",
        400,
    ));
    let mut c = cfg(&h);
    c.cooldown_s = 0;
    c.max_per_user = 5;
    c.max_duration_s = 300;
    h.kv.borrow_mut()
        .insert("dj.config".into(), serde_json::to_vec(&c).unwrap());
    let u = |id: &str| user(USER_A, false, id);
    say(&h, &u("1"), "!sr alpha");
    say(&h, &u("2"), "!sr https://www.youtube.com/watch?v=yt2");
    assert!(say(&h, &u("3"), "!sr long one").contains("removed: songs must be at most"));
    assert!(m(&h, "!music move 1 2").contains("position 2"));
    assert!(m(&h, "!music list").contains("next: #2"));
    // a moderator override restores the removed request, even though it violates policy
    assert_eq!(m(&h, "!music restore 3"), "restored #3");
    assert!(m(&h, "!music ban 3").starts_with("banned"));
    assert!(m(&h, "!dj audit 10").contains("mod_ban"));
    assert_eq!(m(&h, "!music clear"), "cleared 2 requests");
    assert!(m(&h, "!music list").contains("queue is empty"));
    // a banned song cannot be restored
    let mut st = Engine::new(&h, &Ctx::default()).state().unwrap();
    st.banned.push("youtube/yt2".into());
    h.kv.borrow_mut()
        .insert("dj.state".into(), serde_json::to_vec(&st).unwrap());
    let r = say(&h, &u("4"), "!sr https://www.youtube.com/watch?v=yt2");
    assert!(r.contains("banned"), "{r}");
    assert!(m(&h, "!music restore 4").contains("banned"));
}

#[test]
fn the_skip_audits_and_ai_mode_without_ai_fails_loud() {
    let h = host();
    let u = user(USER_A, false, "1");
    say(&h, &u, "!sr alpha");
    m(&h, "!music skip");
    m(&h, "!music skip");
    let rows: Vec<waddle_bundle_dj_app::model::AuditRow> =
        serde_json::from_slice(&h.kv.borrow()["dj.audit"]).unwrap();
    assert!(rows.iter().any(|r| r.outcome == Outcome::Skipped));
    // Mode::Ai with the AI DJ disabled (config edited behind our back) must say so, not idle silently.
    let mut c = cfg(&h);
    c.mode = Mode::Ai;
    h.kv.borrow_mut()
        .insert("dj.config".into(), serde_json::to_vec(&c).unwrap());
    assert!(m(&h, "!music skip").contains("AI DJ is off"));
}

#[test]
fn spotify_handoff_uses_the_player_tool_with_an_idempotency_key_and_failure_is_reported() {
    let h = host();
    m(&h, "!dj set providers spotify");
    say(&h, &user(USER_A, false, "1"), "!sr gamma");
    let r = m(&h, "!music skip");
    assert!(r.starts_with("now playing: Gamma Song"), "{r}");
    let calls = h.calls.borrow();
    let c = calls
        .iter()
        .find(|c| c.1 == "music.spotify.player.play")
        .expect("player.play called");
    assert_eq!(c.2, json!({"uris": ["spotify:track:sp1"]}));
    assert_eq!(c.3.as_deref(), Some("dj.play.1"));
    drop(calls);
    h.add(track(
        "spotify",
        "sp2",
        "Delta Song",
        "Artist Five",
        "",
        200,
    ));
    say(&h, &user(USER_B, false, "2"), "!sr delta");
    h.fail_tools
        .borrow_mut()
        .push("music.spotify.player".into());
    let r = m(&h, "!music skip");
    assert!(
        r.contains("player handoff unavailable"),
        "a failed handoff must be reported: {r}"
    );
}

#[test]
fn providers_without_a_player_tool_make_no_handoff_call() {
    let h = host();
    say(&h, &user(USER_A, false, "1"), "!sr alpha");
    let r = m(&h, "!music skip");
    assert!(r.starts_with("now playing: Alpha Song"), "{r}");
    assert_eq!(h.calls_to("music.youtube.player.play"), 0);
    assert!(h.calls.borrow().iter().all(|c| !c.1.contains("playback")));
}

#[test]
fn playlist_import_follows_next_page_token() {
    let h = host();
    for i in 0..5 {
        h.add(track(
            "youtube",
            &format!("pg{i}"),
            &format!("Paged {i}"),
            "Pager",
            "UC_P",
            100,
        ));
    }
    h.page_size.set(2);
    m(&h, "!dj playlist create paged");
    let r = m(&h, "!dj playlist import paged youtube PLabc");
    assert!(r.contains("imported 7 songs"), "{r}"); // 2 host() + 5 added youtube tracks
    assert!(h.calls_to("music.youtube.playlist.get") >= 4);
    let page_calls: Vec<_> = h
        .calls
        .borrow()
        .iter()
        .filter(|c| c.1 == "music.youtube.playlist.get")
        .map(|c| c.2.clone())
        .collect();
    assert!(page_calls[0].get("page_token").is_none());
    assert_eq!(page_calls[1]["page_token"], "p1");
}

#[test]
fn unresolvable_urls_read_as_not_found_not_as_an_outage() {
    let h = host();
    let r = say(
        &h,
        &user(USER_A, false, "1"),
        "!sr https://www.youtube.com/watch?v=nothere",
    );
    assert!(r.contains("could not find"), "{r}");
}

#[test]
fn unknown_explicit_status_fails_closed_when_explicit_is_disallowed() {
    let h = FakeHost::new();
    let mut t = track("youtube", "unk", "Unknown Flag", "Someone", "UC_Z", 100);
    t["explicit"] = serde_json::Value::Null;
    h.add(t);
    assert!(say(&h, &user(USER_A, false, "1"), "!sr unknown flag").starts_with("queued"));
    m(&h, "!dj set explicit off");
    let r = say(&h, &user(USER_B, false, "2"), "!sr unknown flag");
    assert!(r.contains("cannot confirm"), "{r}");
}

#[test]
fn provider_error_mapping_and_invalid_responses() {
    let h = host();
    assert_eq!(
        music::search(&h, "tidal", "x", 1).unwrap_err(),
        music::MusicError::UnknownProvider
    );
    h.fail_tools
        .borrow_mut()
        .push("music.youtube.search".into());
    assert!(matches!(
        music::search(&h, "youtube", "x", 1),
        Err(music::MusicError::Unavailable(_))
    ));
    struct Denied;
    impl waddle_bundle_dj_app::host::Host for Denied {
        fn kv_get(&self, _: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(None)
        }
        fn kv_set(&self, _: &str, _: &[u8], _: u32) -> Result<(), String> {
            Ok(())
        }
        fn kv_delete(&self, _: &str) -> Result<(), String> {
            Ok(())
        }
        fn now_ms(&self) -> u64 {
            0
        }
        fn invoke(
            &self,
            _: &str,
            tool: &str,
            _: &serde_json::Value,
            _: Option<&str>,
        ) -> Result<serde_json::Value, ToolErr> {
            match tool {
                "music.youtube.search" => Err(ToolErr::NotGranted),
                "music.youtube.track" => Ok(json!({"nope": 1})),
                "music.spotify.search" => Err(ToolErr::InvalidArgs("x".into())),
                _ => Ok(
                    json!({"tracks": [{"provider":"youtube","id":"bad id!","title":"t","artist":"a","duration_ms":1000,"thumbnail":null,"explicit":false,"source_url":"https://x/y"},
                                          {"provider":"spotify","id":"ok1","title":"cross provider","artist":"a","duration_ms":1000,"thumbnail":null,"explicit":false,"source_url":"https://x/y"},
                                          {"provider":"youtube","id":"ok2","title":"   ","artist":"a","duration_ms":1000,"thumbnail":null,"explicit":false,"source_url":"https://x/y"},
                                          {"provider":"youtube","id":"good","title":"Fine\u{0007}   Song","artist":"A","duration_ms":9000,"thumbnail":null,"explicit":null,"source_url":"http://insecure"}]}),
                ),
            }
        }
        fn log(&self, _: waddle_bundle_dj_app::host::Level, _: &str, _: serde_json::Value) {}
    }
    let d = Denied;
    assert!(matches!(
        music::search(&d, "youtube", "x", 1),
        Err(music::MusicError::NotGranted(_))
    ));
    assert!(matches!(
        music::resolve(&d, "youtube", "r"),
        Err(music::MusicError::BadResponse(_))
    ));
    assert!(matches!(
        music::search(&d, "spotify", "x", 1),
        Err(music::MusicError::BadResponse(_))
    ));
    let tracks = music::playlist(&d, "youtube", "pl", 5).unwrap();
    assert_eq!(
        tracks.len(),
        1,
        "bad id, wrong provider and empty title are dropped"
    );
    assert_eq!(tracks[0].title, "Fine Song");
    assert_eq!(tracks[0].url, None, "non-https urls are dropped");
    assert!(
        music::MusicError::NotGranted("x".into())
            .to_string()
            .contains("not available")
    );
}

#[test]
fn sanitization_and_int_enum_codes() {
    let t = Track {
        provider: "youtube".into(),
        id: "a".repeat(129),
        title: "t".into(),
        artist: "a".into(),
        channel_id: Some("  ".into()),
        duration_s: 1,
        explicit: Some(false),
        url: None,
        license: None,
    };
    assert!(t.sanitized("youtube").is_none());
    assert_eq!(serde_json::to_string(&Status::Pending).unwrap(), "1");
    assert!(serde_json::from_str::<Status>("9").is_err());
    assert_eq!(serde_json::from_str::<Mode>("2").unwrap(), Mode::Ai);
    assert_eq!(
        waddle_bundle_dj_app::model::clean_text("a\n\t b   c", 3),
        "a b"
    );
}

#[test]
fn disabled_provider_links_and_unsupported_sites_are_refused() {
    let h = host();
    let u = user(USER_A, false, "1");
    assert!(
        say(&h, &u, "!sr https://open.spotify.com/track/sp1")
            .contains("spotify is not enabled here")
    );
    assert!(say(&h, &u, "!sr").contains("usage: !sr"));
    assert!(say(&h, &user(USER_B, false, "2"), "!sr zzzzzz nothing").contains("no songs found"));
    m(&h, "!dj set providers youtube,spotify");
    m(&h, "!dj set cooldown_s 0");
    assert!(
        say(
            &h,
            &user(USER_B, false, "3"),
            "!sr https://open.spotify.com/track/sp1"
        )
        .starts_with("queued")
    );
    assert!(
        say(
            &h,
            &user(USER_B, false, "4"),
            "!sr https://www.youtube.com/watch?v=missing"
        )
        .contains("could not find")
    );
}

#[test]
fn playlist_errors_and_limits() {
    let h = host();
    m(&h, "!dj playlist create one");
    assert!(m(&h, "!dj playlist create one").contains("already exists"));
    assert!(m(&h, "!dj playlist add one").contains("usage"));
    assert!(m(&h, "!dj playlist add one https://evil.example/x").contains("not a supported"));
    assert!(m(&h, "!dj playlist add one zzzz").contains("no songs found"));
    assert!(
        m(
            &h,
            "!dj playlist add one https://www.youtube.com/watch?v=nope"
        )
        .contains("could not find")
    );
    assert!(m(&h, "!dj playlist add one alpha").contains("added"));
    assert!(m(&h, "!dj playlist remove one 9").contains("usage"));
    assert!(m(&h, "!dj playlist move one 1 9").contains("positions must be"));
    assert!(m(&h, "!dj playlist move one x").contains("usage"));
    assert!(m(&h, "!dj playlist import one").contains("usage"));
    assert!(m(&h, "!dj playlist import one tidal x").contains("provider must be"));
    assert!(m(&h, "!dj playlist show one").contains("1 songs | 1. Alpha Song"));
    assert!(m(&h, "!dj playlist list").contains("one"));
    for i in 0..19 {
        m(&h, &format!("!dj playlist create p{i}"));
    }
    assert!(m(&h, "!dj playlist create toomany").contains("too many"));
    // a playlist loop with nothing playable idles instead of spinning
    m(&h, "!dj playlist use one");
    m(&h, "!dj mode playlist");
    m(&h, "!dj list deny-artist add artist one");
    assert!(m(&h, "!music skip").starts_with("the queue is empty"));
    assert_eq!(cfg(&h).active_playlist.as_deref(), Some("one"));
}

#[test]
fn policy_records_pending_status_and_roundtrips_state() {
    let h = host();
    m(&h, "!dj set approval on");
    say(&h, &user(USER_A, false, "1"), "!sr alpha");
    let st = Engine::new(&h, &Ctx::default()).state().unwrap();
    assert_eq!(st.queue[0].status, Status::Pending);
    assert_eq!(
        st.queue[0].reason.as_deref(),
        Some("waiting for moderator approval")
    );
}
