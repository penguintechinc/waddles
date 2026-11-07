//! Contract tests for the music tool surface the `dj` app bundle consumes.
//!
//! Drives every tool through the REAL registry (`build_registry`) with
//! realistic Spotify payloads and pins (a) the exact output keys a consumer may
//! rely on, (b) the manifests listing exactly the registered tools and agreeing
//! with each other, and (c) the credential/egress boundary on the wire.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use serde_json::{Value, json};
use waddle_bundle_music_spotify_service::logic::testing::{Rig, spotify_track};
use waddle_bundle_music_spotify_service::logic::{TOOL_NAMES, build_registry};

const T1: &str = "4uLU6hMCjMI75M1A2tKUQC";
const P1: &str = "37i9dQZF1DXcBWIGoYBM5M";

const TRACK_KEYS: [&str; 11] = [
    "provider",
    "external_id",
    "title",
    "artist",
    "artists",
    "album",
    "duration_ms",
    "artwork_url",
    "url",
    "uri",
    "explicit",
];

fn keys(v: &Value) -> Vec<String> {
    let mut k: Vec<String> = v.as_object().unwrap().keys().cloned().collect();
    k.sort();
    k
}

fn manifest_tool_names(text: &str) -> Vec<String> {
    let mut names: Vec<String> = text
        .lines()
        .filter_map(|l| l.strip_prefix("- name: "))
        .map(str::to_string)
        .collect();
    names.sort();
    names
}

fn exported_block(text: &str) -> String {
    let mut out = Vec::new();
    let mut inside = false;
    for l in text.lines() {
        if l.starts_with("exported_tools:") {
            inside = true;
            continue;
        }
        if inside {
            if !l.is_empty() && !l.starts_with(' ') && !l.starts_with('-') {
                break;
            }
            out.push(l);
        }
    }
    out.join("\n")
}

#[test]
fn manifests_list_exactly_the_registered_tools_and_agree() {
    let bundle =
        std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/bundle.yaml")).unwrap();
    let hub =
        std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/hub-manifest.yaml")).unwrap();
    let mut want: Vec<String> = TOOL_NAMES.iter().map(|s| s.to_string()).collect();
    want.sort();
    assert_eq!(manifest_tool_names(&bundle), want);
    assert_eq!(manifest_tool_names(&hub), want);
    let (a, b) = (exported_block(&bundle), exported_block(&hub));
    assert!(
        a.lines().count() > 100,
        "examined {} lines",
        a.lines().count()
    );
    assert_eq!(
        a, b,
        "bundle.yaml and hub-manifest.yaml exported_tools drifted"
    );
    for t in TOOL_NAMES {
        assert!(
            build_registry(Rig::new().svc).call(t, b"[]").is_err(),
            "{t} must reject non-object args loudly"
        );
    }
}

#[test]
fn manifest_write_tools_are_dangerous_and_reads_are_normal() {
    let bundle =
        std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/bundle.yaml")).unwrap();
    for t in ["play", "pause", "next", "queue_add"] {
        let at = bundle
            .find(&format!("- name: music.spotify.player.{t}\n"))
            .unwrap();
        let head = &bundle[at..at + 120];
        assert!(
            head.contains("access: write") && head.contains("risk: dangerous"),
            "{t}: {head}"
        );
    }
}

#[test]
fn output_shapes_match_the_consumer_contract() {
    let r = Rig::new();
    r.http
        .push(200, &json!({"tracks": {"items": [spotify_track(T1, "S")]}}));
    let out = r
        .call("music.spotify.search", json!({"query": "q"}), None)
        .unwrap();
    assert_eq!(keys(&out), ["capability", "count", "tracks"]);
    assert_eq!(keys(&out["tracks"][0]), {
        let mut k: Vec<String> = TRACK_KEYS.iter().map(|s| s.to_string()).collect();
        k.sort();
        k
    });

    r.http.push(200, &spotify_track(T1, "S"));
    let out = r
        .call("music.spotify.track", json!({"track": T1_URI()}), None)
        .unwrap();
    assert_eq!(keys(&out), ["capability", "track"]);

    r.http
        .push(200, &json!({"name": "M", "tracks": {"total": 1}}));
    r.http
        .push(200, &json!({"items": [{"track": spotify_track(T1, "S")}]}));
    let out = r
        .call(
            "music.spotify.playlist.get",
            json!({"playlist": format!("spotify:playlist:{P1}")}),
            None,
        )
        .unwrap();
    assert_eq!(keys(&out), ["capability", "playlist", "tracks"]);
    assert_eq!(
        keys(&out["playlist"]),
        [
            "id", "limit", "name", "offset", "owner", "returned", "skipped", "total", "url"
        ]
    );

    // resolve of the already-fetched track is served from cache: no upstream push.
    let out = r
        .call("music.spotify.resolve", json!({"url": T1_URI()}), None)
        .unwrap();
    assert_eq!(keys(&out), ["capability", "id", "kind", "track"]);

    r.http.push(204, &Value::Null);
    let out = r
        .call("music.spotify.player.state", json!({}), None)
        .unwrap();
    assert_eq!(keys(&out), ["active", "capability", "is_playing"]);

    r.http.push(204, &Value::Null);
    let out = r
        .call("music.spotify.player.pause", json!({}), Some("k"))
        .unwrap();
    assert_eq!(keys(&out), ["action", "capability", "ok"]);

    assert_eq!(
        keys(&out["capability"]),
        [
            "embed_only",
            "metadata",
            "playback",
            "provider",
            "requires_premium"
        ]
    );
}

#[allow(non_snake_case)]
fn T1_URI() -> String {
    format!("spotify:track:{T1}")
}

#[test]
fn wire_boundary_only_api_host_and_only_secret_ref_names() {
    let r = Rig::new();
    r.http.push(200, &spotify_track(T1, "S"));
    r.http.push(204, &Value::Null);
    r.http.push(204, &Value::Null);
    r.call("music.spotify.track", json!({"track": T1_URI()}), None)
        .unwrap();
    r.call("music.spotify.player.next", json!({}), Some("k1"))
        .unwrap();
    r.call(
        "music.spotify.player.queue_add",
        json!({"track": T1_URI()}),
        Some("k2"),
    )
    .unwrap();
    let reqs = r.http.requests();
    assert_eq!(reqs.len(), 3);
    for q in &reqs {
        assert!(
            q.url.starts_with("https://api.spotify.com/v1/"),
            "{}",
            q.url
        );
        assert!(
            q.headers
                .iter()
                .all(|(k, _)| !k.eq_ignore_ascii_case("authorization"))
        );
    }
    assert_eq!(reqs[0].cred.secret_ref(), "spotify.app");
    assert_eq!(reqs[1].cred.secret_ref(), "spotify.oauth");
}

#[test]
fn caller_cannot_redirect_requests_via_args() {
    let r = Rig::new();
    for a in [
        json!({"track": "https://evil.example/track/4uLU6hMCjMI75M1A2tKUQC"}),
        json!({"track": "https://open.spotify.com.evil.example/track/4uLU6hMCjMI75M1A2tKUQC"}),
        json!({"track": "spotify:track:../../v1/me"}),
        json!({"track": T1_URI(), "url": "https://evil.example"}),
    ] {
        assert!(r.call("music.spotify.track", a, None).is_err());
    }
    assert!(r.http.requests().is_empty());
}

#[test]
fn manifests_gate_player_tools_by_scope_and_metadata_has_no_auth_selector() {
    for f in ["bundle.yaml", "hub-manifest.yaml"] {
        let text = std::fs::read_to_string(format!("{}/{f}", env!("CARGO_MANIFEST_DIR"))).unwrap();
        for (tool, scope) in [
            ("state", "community:moderate"),
            ("play", "community:moderate"),
            ("pause", "community:moderate"),
            ("next", "community:moderate"),
            ("queue_add", "community:member"),
        ] {
            let at = text
                .find(&format!("- name: music.spotify.player.{tool}\n"))
                .unwrap();
            let head = &text[at..at + 160];
            assert!(
                head.contains(&format!("required_scope: {scope}")),
                "{f} {tool}: {head}"
            );
        }
        for t in ["search", "track", "playlist.get", "resolve"] {
            let at = text.find(&format!("- name: music.spotify.{t}\n")).unwrap();
            let end = text[at..].find("output_schema").unwrap();
            assert!(
                !text[at..at + end].contains("auth"),
                "{f} {t} exposes an auth selector"
            );
        }
    }
}
