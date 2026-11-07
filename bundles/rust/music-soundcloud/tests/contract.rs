//! Contract fixtures: the shipped manifests, the registered tool table and the
//! normalized output shape the DJ app consumes must agree. Drift between
//! `bundle.yaml`, `hub-manifest.yaml` and the code fails here, loudly.

use std::fs;
use std::path::Path;
use std::sync::Arc;

use serde_json::{Value, json};
use waddle_bundle_music_soundcloud::logic::fakes::{FakeStore, FakeUpstream, ok, service, status};
use waddle_bundle_music_soundcloud::logic::{TOOL_NAMES, build_registry};

fn read(name: &str) -> String {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join(name);
    fs::read_to_string(&p).unwrap_or_else(|e| panic!("read {p:?}: {e}"))
}

/// The `exported_tools:` block (it runs to end-of-section in both manifests).
fn tools_block(manifest: &str) -> String {
    let start = manifest
        .find("exported_tools:")
        .expect("exported_tools block");
    let end = manifest
        .find("\nconfig_schema:")
        .expect("config_schema after tools");
    manifest[start..end].to_string()
}

#[test]
fn manifests_agree_and_declare_every_registered_tool() {
    let (b, h) = (read("bundle.yaml"), read("hub-manifest.yaml"));
    assert_eq!(
        tools_block(&b),
        tools_block(&h),
        "exported_tools drifted between manifests"
    );
    let block = tools_block(&b);
    let declared = block.matches("\n- name: ").count();
    assert_eq!(declared, TOOL_NAMES.len());
    for t in TOOL_NAMES {
        assert!(
            block.contains(&format!("- name: {t}\n")),
            "{t} missing from manifest"
        );
    }
    // Same egress + permission in both; no credential material anywhere.
    for m in [&b, &h] {
        assert!(m.contains("net.http.fqdn:api.soundcloud.com"));
        assert!(m.contains("justification:"));
        for forbidden in ["client_secret", "Bearer ", "access_token:"] {
            assert!(
                !m.contains(forbidden),
                "{forbidden} must not appear in a manifest"
            );
        }
    }
    assert!(b.contains("app_id: waddles.core.service.music-soundcloud"));
    assert!(h.contains("app_id: waddles.core.service.music-soundcloud"));
}

fn track_fixture(id: u64) -> Value {
    json!({
        "urn": format!("soundcloud:tracks:{id}"), "title": "T", "duration": 61_000,
        "permalink_url": "https://soundcloud.com/a/t", "access": "playable", "streamable": true,
        "downloadable": true, "license": "cc-by-nc", "sharing": "public",
        "user": {"username": "A", "urn": "soundcloud:users:1"}
    })
}

/// The common music-track contract the `dj` app codes against.
fn assert_track_contract(t: &Value) {
    for k in [
        "provider",
        "id",
        "title",
        "artist",
        "url",
        "artwork_url",
        "genre",
        "duration_s",
        "explicit",
        "license",
        "capability",
    ] {
        assert!(t.get(k).is_some(), "track missing {k}: {t}");
    }
    assert_eq!(t["provider"], "soundcloud");
    assert!(t["id"].is_string() && t["title"].is_string() && t["duration_s"].is_u64());
    for k in [
        "type",
        "creative_commons",
        "all_rights_reserved",
        "streamable",
        "downloadable",
        "access",
        "sharing",
    ] {
        assert!(t["license"].get(k).is_some(), "license missing {k}");
    }
    for k in [
        "metadata",
        "embed",
        "full_playback",
        "stream_url",
        "download",
    ] {
        assert!(
            t["capability"][k].is_boolean(),
            "capability.{k} must be bool"
        );
    }
    assert_eq!(t["capability"]["stream_url"], false);
    assert_eq!(t["capability"]["download"], false);
    assert!(
        t.get("stream_url").is_none(),
        "audio stream URLs are never returned"
    );
}

#[test]
fn tool_outputs_satisfy_the_common_track_contract() {
    let up = FakeUpstream::with(vec![
        ok(json!({"collection": [track_fixture(1)]})),
        ok(track_fixture(2)),
        ok(json!({"urn": "soundcloud:playlists:3", "title": "P", "license": "cc-by"})),
        ok(json!([track_fixture(4)])),
        status(302, Some("https://api.soundcloud.com/tracks/5"), None),
        ok(track_fixture(5)),
    ]);
    let sc = service(up, Arc::new(FakeStore::default()));
    let reg = build_registry(sc);
    let call = |n: &str, a: Value| -> Value {
        serde_json::from_slice(&reg.call(n, &serde_json::to_vec(&a).unwrap()).unwrap()).unwrap()
    };
    let s = call("music.soundcloud.search", json!({"q": "x"}));
    assert_track_contract(&s["tracks"][0]);
    assert_track_contract(&call("music.soundcloud.track", json!({"id": "2"})));
    let p = call("music.soundcloud.playlist.get", json!({"id": "3"}));
    assert_track_contract(&p["tracks"][0]);
    assert!(p["truncated"].is_boolean() && p["license"].is_object());
    let r = call(
        "music.soundcloud.resolve",
        json!({"url": "https://soundcloud.com/a/t"}),
    );
    assert_eq!(r["kind"], "track");
    assert_track_contract(&r["track"]);
}
