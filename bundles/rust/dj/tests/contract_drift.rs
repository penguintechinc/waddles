//! Fails when the bundle's manifest and the shared contracts
//! (`bundles/contracts/*.v1.json`) disagree. The schemas the other tests
//! enforce are `include_str!`-derived from the same files, so a contract edit
//! cannot pass here without the bundle being updated.

mod common;

use common::{AI_CONTRACT, MUSIC_CONTRACT};
use serde_json::Value;

const HUB_MANIFEST: &str = include_str!("../hub-manifest.yaml");
const BUNDLE_MANIFEST: &str = include_str!("../bundle.yaml");

/// `tool.call:<target>:<tool>` ids requested by a manifest (denominator returned too).
fn requested(manifest: &str) -> Vec<(String, String)> {
    manifest
        .lines()
        .filter_map(|l| l.trim().strip_prefix("- id: tool.call:"))
        .map(|rest| {
            let (target, tool) = rest.split_once(':').expect("target:tool");
            (target.to_string(), tool.to_string())
        })
        .collect()
}

#[test]
fn every_requested_tool_exists_in_its_producers_contract() {
    let music: Value = serde_json::from_str(MUSIC_CONTRACT).unwrap();
    let ai: Value = serde_json::from_str(AI_CONTRACT).unwrap();
    assert_eq!(music["version"], 1);
    assert_eq!(ai["version"], 1);
    let ai_target = ai["target_app_id"].as_str().unwrap();
    let reqs = requested(HUB_MANIFEST);
    assert!(
        reqs.len() >= 13,
        "examined too few tool grants: {}",
        reqs.len()
    );
    for (target, tool) in &reqs {
        if let Some(rest) = tool.strip_prefix("music.") {
            let (provider, op) = rest.split_once('.').unwrap();
            assert_eq!(target, &format!("waddles.core.service.music-{provider}"));
            // Spotify player control is the single non-contract tool (producer manifest, PR #1111).
            if provider == "spotify" && op == "player.play" {
                continue;
            }
            assert!(
                music["tools"].get(op).is_some(),
                "{tool}: op `{op}` is not in music-tool-contract.v1 (generic `playback` does not exist)"
            );
        } else if tool.starts_with("ai.") {
            assert_eq!(target, ai_target);
            assert!(ai["tools"].get(tool).is_some(), "{tool} not in ai-tools.v1");
        } else {
            assert_eq!(
                (target.as_str(), tool.as_str()),
                ("waddles.core.service.economy", "adjust_balance"),
                "unexpected grant {target}:{tool}"
            );
        }
    }
}

#[test]
fn bundle_and_hub_manifests_request_the_same_tools() {
    assert_eq!(requested(BUNDLE_MANIFEST), requested(HUB_MANIFEST));
    assert!(
        !requested(HUB_MANIFEST)
            .iter()
            .any(|(_, t)| t.ends_with(".playback"))
    );
}

#[test]
fn contract_limits_the_bundle_relies_on_still_hold() {
    let music: Value = serde_json::from_str(MUSIC_CONTRACT).unwrap();
    let ai: Value = serde_json::from_str(AI_CONTRACT).unwrap();
    let max = |v: &Value| v.as_u64().unwrap();
    // search limit cap, playlist page cap, ai prompt/system bounds used by `music.rs` / `ai.rs`.
    assert!(max(&music["tools"]["search"]["input_schema"]["properties"]["limit"]["maximum"]) >= 25);
    assert!(
        max(&music["tools"]["playlist.get"]["input_schema"]["properties"]["limit"]["maximum"])
            >= 50
    );
    let c = &ai["tools"]["ai.complete"]["input_schema"]["properties"];
    assert!(max(&c["prompt"]["maxLength"]) >= 3500 && max(&c["system"]["maxLength"]) >= 1200);
    assert!(max(&c["max_tokens"]["maximum"]) >= 512);
    assert!(
        ai["tools"]["ai.classify"]["output_schema"]["required"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r == "confidence")
    );
}
