//! Rust half of the data-plane golden-vector parity harness (gh #840 PR4).
//!
//! Runs every dual-path vector in `tests/data_plane_parity/corpus/` through
//! the REAL Rust ingest path -- the connector crates' own raw-frame parsers
//! (`penguin_connector_twitch::irc::parse_privmsg_line`,
//! `penguin_connector_discord::gateway::normalize_message_create`) feeding
//! `svc_ingest::normalize::*` -- and asserts the resulting `PlatformEvent`
//! equals the vector's committed `golden`. `normalize.rs` is the oracle: the
//! Python half (`tests/data_plane_parity/test_golden_parity.py`) diffs the
//! legacy Python path against the SAME golden, so Python-vs-Rust is proven
//! transitively through one shared, reviewable artifact.
//!
//! `occurred_at` is an arrival timestamp (both paths stamp "now"), so it is
//! excluded from the golden and checked for shape only.
//!
//! Blessing: `PARITY_BLESS=1 cargo test --test parity_golden` rewrites the
//! `golden` of every vector whose `golden_origin` is `rust` (review the diff).
//! Without it, a missing/stale golden FAILS -- never silently regenerated.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use svc_ingest::ingest::twitch_eventsub::KNOWN_EVENT_TYPES;
use svc_ingest::normalize::{
    compute_discord_permissions, is_self_message, normalize_discord, normalize_twitch_eventsub,
    normalize_twitch_irc, DiscordPermissions,
};

/// Corpus root, shared with the Python harness and `svc_process`'s PII test.
fn corpus_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/data_plane_parity/corpus")
}

/// Every `*.json` vector under the corpus, sorted for deterministic output.
fn load_vectors() -> Vec<(PathBuf, Value)> {
    let mut out = Vec::new();
    let mut stack = vec![corpus_dir()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).unwrap_or_else(|e| panic!("read corpus dir {dir:?}: {e}")) {
            let path = entry.expect("dir entry").path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().and_then(|e| e.to_str()) == Some("json") {
                let text = fs::read_to_string(&path).expect("read vector");
                let value: Value = serde_json::from_str(&text)
                    .unwrap_or_else(|e| panic!("vector {path:?} is not valid JSON: {e}"));
                out.push((path, value));
            }
        }
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
}

/// The outcome of running one frame through the Rust path.
enum Outcome {
    /// The frame was filtered/ignored before normalization (self-message,
    /// non-PRIVMSG line, unhandled EventSub type, ...).
    Dropped,
    /// A normalized event, `occurred_at` stripped (arrival time, shape-checked).
    Event(Value),
}

/// Serializes a `PlatformEvent` to the golden shape (no `occurred_at`),
/// after asserting `occurred_at` is RFC 3339 millis-`Z`.
fn to_golden(event: &penguin_spine::PlatformEvent) -> Value {
    assert!(
        event.occurred_at.ends_with('Z')
            && chrono::DateTime::parse_from_rfc3339(&event.occurred_at).is_ok(),
        "occurred_at must be RFC3339 millis Z, got {:?}",
        event.occurred_at
    );
    let mut value = serde_json::to_value(event).expect("PlatformEvent serializes");
    value
        .as_object_mut()
        .expect("event is an object")
        .remove("occurred_at");
    value
}

/// Runs `vector.frame` through the real Rust path for `vector.path`.
fn run_rust_path(vector: &Value) -> Outcome {
    let id = vector["id"].as_str().expect("id");
    let ctx = &vector["context"];
    match vector["path"].as_str().expect("path") {
        "twitch_irc" => {
            let line = vector["frame"].as_str().expect("irc frame is a string");
            let Some(msg) = penguin_connector_twitch::irc::parse_privmsg_line(line) else {
                return Outcome::Dropped;
            };
            let nick = ctx["self_nick"].as_str().expect("context.self_nick");
            if is_self_message(&msg.sender, nick) {
                return Outcome::Dropped;
            }
            Outcome::Event(to_golden(&normalize_twitch_irc(&msg)))
        }
        "discord_gateway" => {
            let self_id = ctx["self_user_id"].as_str();
            let Some(msg) = penguin_connector_discord::gateway::normalize_message_create(
                &vector["frame"],
                self_id,
            ) else {
                return Outcome::Dropped;
            };
            // Mirrors `ingest::discord`'s dispatch: a DM has no roles to
            // resolve (fail-closed default); a guild message derives
            // is_mod/is_broadcaster from the resolved role set.
            let perms = match msg.guild_id.as_deref() {
                None => DiscordPermissions::default(),
                Some(_) => {
                    let strings = |key: &str| -> Vec<String> {
                        ctx[key]
                            .as_array()
                            .unwrap_or_else(|| panic!("{id}: context.{key} must be an array"))
                            .iter()
                            .map(|v| v.as_str().expect("string").to_string())
                            .collect()
                    };
                    let roles: Vec<(String, u64)> = ctx["guild_roles"]
                        .as_array()
                        .expect("context.guild_roles")
                        .iter()
                        .map(|pair| {
                            (
                                pair[0].as_str().expect("role id").to_string(),
                                pair[1].as_u64().expect("role permissions"),
                            )
                        })
                        .collect();
                    compute_discord_permissions(
                        &msg.author_id,
                        ctx["guild_owner_id"].as_str().expect("guild_owner_id"),
                        &strings("member_role_ids"),
                        &roles,
                    )
                }
            };
            Outcome::Event(to_golden(&normalize_discord(&msg, &perms)))
        }
        "twitch_eventsub" => {
            let sub_type = vector["frame"]["subscription"]["type"]
                .as_str()
                .expect("subscription.type");
            if !KNOWN_EVENT_TYPES.contains(&sub_type) {
                return Outcome::Dropped;
            }
            let broadcaster =
                vector["frame"]["subscription"]["condition"]["broadcaster_user_id"].as_str();
            let event = vector["frame"].get("event").cloned().unwrap_or_default();
            Outcome::Event(to_golden(&normalize_twitch_eventsub(
                sub_type,
                &event,
                broadcaster,
            )))
        }
        other => panic!("{id}: no Rust path registered for vector path {other:?}"),
    }
}

/// Canonical on-disk form: sorted keys (serde_json's default `Map`), 2-space
/// indent, trailing newline -- byte-identical to the Python harness's
/// `json.dumps(sort_keys=True, indent=2, ensure_ascii=False)`.
fn write_vector(path: &Path, value: &Value) {
    let mut text = serde_json::to_string_pretty(value).expect("serialize vector");
    text.push('\n');
    fs::write(path, text).unwrap_or_else(|e| panic!("write {path:?}: {e}"));
}

#[test]
fn rust_normalize_path_matches_every_golden_vector() {
    let bless = std::env::var("PARITY_BLESS").is_ok_and(|v| v == "1");
    let vectors = load_vectors();
    assert!(
        !vectors.is_empty(),
        "corpus is empty -- zero vectors examined is a FAILURE"
    );

    let mut seen_ids = HashSet::new();
    let mut compared = 0usize;
    let mut dropped = 0usize;
    let mut python_only = 0usize;
    let mut platforms = HashSet::new();
    let mut failures: Vec<String> = Vec::new();

    for (path, mut vector) in vectors {
        let id = vector["id"].as_str().expect("vector.id").to_string();
        assert!(seen_ids.insert(id.clone()), "duplicate vector id {id}");
        platforms.insert(vector["platform"].as_str().expect("platform").to_string());

        // Python-only slot: the Rust/bundle path has not landed yet.
        if vector.get("rust_path").is_some_and(Value::is_null) {
            python_only += 1;
            continue;
        }

        let expected_outcome = vector["outcome"].as_str().expect("outcome").to_string();
        match (run_rust_path(&vector), expected_outcome.as_str()) {
            (Outcome::Dropped, "dropped") => {
                dropped += 1;
                compared += 1;
            }
            (Outcome::Dropped, "event") => {
                failures.push(format!(
                    "{id}: Rust path dropped a frame the vector expects to normalize"
                ));
            }
            (Outcome::Event(_), "dropped") => {
                failures.push(format!(
                    "{id}: Rust path normalized a frame the vector expects to be dropped"
                ));
            }
            (Outcome::Event(actual), "event") => {
                compared += 1;
                if vector["golden_origin"] != json!("rust") {
                    failures.push(format!(
                        "{id}: dual-path vector must have golden_origin \"rust\""
                    ));
                } else if bless && vector["golden"] != actual {
                    vector["golden"] = actual;
                    write_vector(&path, &vector);
                } else if vector["golden"] != actual {
                    failures.push(format!(
                        "{id}: golden mismatch (re-bless with PARITY_BLESS=1 if intended)\n  golden: {}\n  actual: {}",
                        vector["golden"], actual
                    ));
                }
            }
            (_, other) => failures.push(format!("{id}: unknown outcome {other:?}")),
        }
    }

    println!(
        "parity_golden: {compared} dual-path vectors compared ({dropped} drop-expected), \
         {python_only} python-only slots skipped, platforms: {platforms:?}"
    );
    assert!(
        failures.is_empty(),
        "Rust parity failures:\n{}",
        failures.join("\n")
    );
    assert!(
        compared >= 1,
        "zero dual-path vectors compared -- gate would be vacuous"
    );
    for required in ["twitch", "discord"] {
        assert!(
            platforms.contains(required),
            "corpus has no {required} vectors"
        );
    }
}
