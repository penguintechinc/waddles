//! Contract-fixture tests for every CONSUMED tool. Each adapter payload (all ops, with extreme
//! field lengths and defaults) must satisfy the tool's input schema fixture in `contracts/`, and
//! the Discord fixtures are cross-checked against the real discord bundle manifest so drift in
//! the service bundle surfaces here. Twitch/Wix fixtures encode the catalog-spec contract the
//! other agents are implementing.

mod common;

use common::{contract, validate};
use waddle_bundle_calendar_rust::model::*;
use waddle_bundle_calendar_rust::sync::{DiscordAdapter, SyncAdapter, TwitchAdapter, WixAdapter};

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
    for op in [
        SyncOp::Create,
        SyncOp::Update,
        SyncOp::Cancel,
        SyncOp::Delete,
    ] {
        let call = a
            .build(op, ev, Some(rid), &Config::default())
            .expect("builds");
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
            .build(SyncOp::Update, &long, None, &Config::default())
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
            None,
            &Config::default(),
        )
        .unwrap();
    assert_eq!(c.args["duration"], 30);
    let c = TwitchAdapter
        .build(
            SyncOp::Create,
            &event("x", "", None, 5000),
            None,
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
        event(&"w".repeat(100), &"d".repeat(1000), Some("Hall"), 90),
    ] {
        all_ops(&WixAdapter, &ev, "wix-1");
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
