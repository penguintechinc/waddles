//! Contract-fixture tests for every CONSUMED tool. Each adapter payload (all ops, with extreme
//! field lengths and defaults) must satisfy the tool's input schema fixture in `contracts/`, and
//! the Discord fixtures are cross-checked against the real discord bundle manifest so drift in
//! the service bundle surfaces here. All fixtures are snapshots of the producer bundles'
//! `exported_tools` input schemas; scripts/ci/tests/test_bundle_contracts.py fails on drift.

mod common;

use common::{contract, validate};
use waddle_bundle_calendar_rust::model::*;
use waddle_bundle_calendar_rust::sync::{
    DiscordAdapter, Remote, SyncAdapter, TwitchAdapter, WixAdapter,
};

fn event(title: &str, desc: &str, loc: Option<&str>, minutes: i64) -> Event {
    Event {
        id: 1,
        public_id: derive_public_id("t", "1", 1),
        title: title.into(),
        description: desc.into(),
        start: 1_900_000_000,
        end: 1_900_000_000 + minutes * 60,
        tz: "America/New_York".into(),
        location: loc.map(str::to_string),
        channel: None,
        status: Status::Scheduled,
        created_by: "u".into(),
        approved_by: None,
        reject_reason: None,
        version: 1,
    }
}

fn all_ops(a: &dyn SyncAdapter, ev: &Event, rid: &str) {
    all_ops_at(a, ev, Remote::id(rid));
}

fn all_ops_at(a: &dyn SyncAdapter, ev: &Event, remote: Remote<'_>) {
    for op in [
        SyncOp::Create,
        SyncOp::Update,
        SyncOp::Cancel,
        SyncOp::Delete,
    ] {
        let call = a.build(op, ev, remote, &Config::default()).expect("builds");
        validate(&contract(&call.tool), &call.args, "$")
            .unwrap_or_else(|e| panic!("{} {op:?}: {e}", call.tool));
    }
}

#[test]
fn discord_payloads_satisfy_contracts_even_at_the_extremes() {
    for ev in [
        event("t", "", None, 1),
        event(
            &"é".repeat(100),
            &"d".repeat(1000),
            Some(&"l".repeat(100)),
            600,
        ),
    ] {
        all_ops(&DiscordAdapter, &ev, "123456789012345678");
    }
    let long = event(&"x".repeat(100), "", None, 60);
    assert!(
        DiscordAdapter
            .build(SyncOp::Update, &long, Remote::default(), &Config::default())
            .is_err(),
        "update needs a remote id"
    );
}

#[test]
fn twitch_payloads_satisfy_contracts_and_clamp_duration() {
    for m in [1, 30, 90, 5000] {
        let ev = event(&"t".repeat(140), "", None, m);
        all_ops(&TwitchAdapter, &ev, "seg1");
    }
    let c = TwitchAdapter
        .build(
            SyncOp::Create,
            &event("x", "", None, 1),
            Remote::default(),
            &Config::default(),
        )
        .unwrap();
    assert_eq!(c.args["duration"], 30);
    let c = TwitchAdapter
        .build(
            SyncOp::Create,
            &event("x", "", None, 5000),
            Remote::default(),
            &Config::default(),
        )
        .unwrap();
    assert_eq!(c.args["duration"], 1380);
    assert_eq!(c.args["is_recurring"], false);
}

#[test]
fn wix_payloads_satisfy_contracts() {
    for ev in [
        event("t", "", None, 60),
        event(
            &"w".repeat(300),
            &"d".repeat(1000),
            Some(&"h".repeat(80)),
            90,
        ),
    ] {
        all_ops_at(
            &WixAdapter,
            &ev,
            Remote {
                id: Some(WIX_ID),
                revision: Some(3),
            },
        );
    }
}

const WIX_ID: &str = "0b5c1a2e-3f4d-4e5f-8a9b-0c1d2e3f4a5b";

#[test]
fn wix_payloads_use_the_wix_bundle_field_names_and_revision() {
    let ev = event("t", "desc", Some("Hall"), 60);
    let remote = Remote {
        id: Some(WIX_ID),
        revision: Some(7),
    };
    let c = WixAdapter
        .build(SyncOp::Create, &ev, Remote::default(), &Config::default())
        .unwrap();
    assert_eq!(c.tool, "wix.event.create");
    for k in ["starts_at", "ends_at", "timezone", "title", "description"] {
        assert!(c.args.get(k).is_some(), "create missing {k}");
    }
    assert_eq!(c.args["location_name"], "Hall");
    assert_eq!(c.args["location_type"], "VENUE");
    assert!(c.args.get("start_date").is_none() && c.args.get("location").is_none());
    let u = WixAdapter
        .build(SyncOp::Update, &ev, remote, &Config::default())
        .unwrap();
    assert_eq!(u.args["event_id"], WIX_ID);
    assert_eq!(u.args["revision"], 7);
    let empty = event("t", "", None, 60);
    let no_default = Config {
        default_location: String::new(),
        ..Config::default()
    };
    let c = WixAdapter
        .build(SyncOp::Create, &empty, Remote::default(), &no_default)
        .unwrap();
    assert!(c.args.get("description").is_none() && c.args.get("location_name").is_none());
}

#[test]
fn wix_update_without_a_known_revision_fails_loudly() {
    let ev = event("t", "", None, 60);
    for rev in [None, Some(0)] {
        let r = WixAdapter.build(
            SyncOp::Update,
            &ev,
            Remote {
                id: Some(WIX_ID),
                revision: rev,
            },
            &Config::default(),
        );
        assert!(r.is_err(), "revision {rev:?} must not build an update");
    }
}

#[test]
fn remote_id_extraction_follows_each_tools_result_shape() {
    use serde_json::json;
    assert_eq!(
        DiscordAdapter
            .remote_id(&json!({"event_id": "42"}))
            .as_deref(),
        Some("42")
    );
    assert_eq!(
        DiscordAdapter.remote_id(&json!({"id": 42})).as_deref(),
        Some("42")
    );
    assert_eq!(DiscordAdapter.remote_id(&json!({"validated": true})), None);
    assert_eq!(
        TwitchAdapter
            .remote_id(&json!({"segment_id": "s"}))
            .as_deref(),
        Some("s")
    );
    assert_eq!(
        WixAdapter.remote_id(&json!({"event_id": "w"})).as_deref(),
        Some("w")
    );
    assert_eq!(WixAdapter.remote_id(&json!({"event_id": ""})), None);
    let wix = json!({"event": {"id": WIX_ID, "revision": "4"}});
    assert_eq!(WixAdapter.remote_id(&wix).as_deref(), Some(WIX_ID));
    assert_eq!(WixAdapter.remote_revision(&wix), Some(4));
    assert_eq!(WixAdapter.remote_revision(&json!({"revision": 2})), Some(2));
    assert_eq!(
        WixAdapter.remote_revision(&json!({"event": {"revision": "x"}})),
        None
    );
    assert_eq!(WixAdapter.remote_revision(&json!({"revision": 0})), None);
    assert_eq!(DiscordAdapter.remote_revision(&wix), None);
}

#[test]
fn discord_fixtures_match_the_discord_bundle_manifest() {
    let manifest = std::fs::read_to_string(format!(
        "{}/../discord/bundle.yaml",
        env!("CARGO_MANIFEST_DIR")
    ))
    .expect("discord bundle manifest readable");
    for tool in [
        "discord.scheduled_event.create",
        "discord.scheduled_event.update",
        "discord.scheduled_event.cancel",
        "discord.scheduled_event.delete",
    ] {
        assert!(
            manifest.contains(&format!("- name: {tool}")),
            "{tool} no longer exported by discord bundle"
        );
        let schema = contract(tool);
        for field in schema["properties"].as_object().unwrap().keys() {
            assert!(
                manifest.contains(&format!("{field}:")),
                "{tool}: field {field} missing from discord manifest"
            );
        }
    }
    // Required-field set of create must still be the four the fixture encodes.
    let create = contract("discord.scheduled_event.create");
    assert_eq!(create["required"].as_array().unwrap().len(), 4);
    assert!(manifest.contains("- scheduled_end_time") && manifest.contains("- location"));
}

#[test]
fn fixture_validator_actually_rejects_bad_payloads() {
    use serde_json::json;
    let s = contract("discord.scheduled_event.cancel");
    assert!(validate(&s, &json!({"event_id": "12"}), "$").is_ok());
    assert!(validate(&s, &json!({"event_id": "../x"}), "$").is_err());
    assert!(validate(&s, &json!({"event_id": "1", "guild_id": "9"}), "$").is_err());
    assert!(validate(&s, &json!({}), "$").is_err());
}
