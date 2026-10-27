//! PII-tokenize acceptance gate for the data-plane parity corpus (gh #840 PR4).
//!
//! Every corpus vector that normalizes to an event is run through
//! `svc_process::pii_tokenize::tokenize_event` -- the REAL closed-schema
//! classifier and substitution loop. This is the gate that a new
//! `normalize` field cannot silently bypass tokenization: an unclassified
//! payload field is rejected (dead-lettered in production), and a vector
//! must declare that outcome (`pii_classifier`) explicitly.
//!
//! `pii_classifier` per vector:
//! * `"pass"` -- the classifier accepts every field AND every identity
//!   field (actor + the raw-name payload fields) comes out as a
//!   `{user:<token>}` placeholder, never the raw handle.
//! * `"reject:a,b"` -- exactly these (sorted, comma-joined) top-level
//!   payload fields (`metadata.<k>` for nested keys) are unclassified today.
//!   Used for Python-only platforms whose Rust port has not landed: the
//!   event would be dead-lettered by svc-process until each field is
//!   classified as IDENTITY or SAFE. A stale expectation FAILS too.
//!
//! The only test double is the `IdentityMinter` seam (hub-api's
//! `MintEphemeralPseudonyms` gRPC); classification, mention scanning and
//! substitution are the production code paths.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use penguin_spine::{PlatformEvent, Source};
use serde_json::Value;
use svc_process::pii_tokenize::{
    tokenize_event, IdentityMinter, MintItem, MintResult, TokenizeError,
};

/// Mints a distinct opaque pseudonym per requested identity -- stands in for
/// hub-api's RPC (a gRPC boundary), never derives anything from the handle.
struct SequentialMinter;

impl IdentityMinter for SequentialMinter {
    fn mint_many<'a>(&'a self, _tenant_id: &'a str, items: Vec<MintItem>) -> MintResult<'a> {
        Box::pin(async move {
            Ok(items
                .into_iter()
                .enumerate()
                .map(|(i, item)| (item.platform_user_id, format!("pseud{i:04}")))
                .collect())
        })
    }
}

fn corpus_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/data_plane_parity/corpus")
}

fn load_vectors() -> Vec<(String, Value)> {
    let mut out = Vec::new();
    let mut stack = vec![corpus_dir()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).unwrap_or_else(|e| panic!("read corpus dir {dir:?}: {e}")) {
            let path = entry.expect("dir entry").path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().and_then(|e| e.to_str()) == Some("json") {
                let value: Value =
                    serde_json::from_str(&fs::read_to_string(&path).expect("read vector"))
                        .unwrap_or_else(|e| panic!("vector {path:?} is not valid JSON: {e}"));
                out.push((value["id"].as_str().expect("id").to_string(), value));
            }
        }
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
}

fn event_from_golden(golden: &Value) -> PlatformEvent {
    let source = golden
        .get("source")
        .filter(|s| !s.is_null())
        .map(|s| Source {
            platform: s["platform"].as_str().expect("source.platform").to_string(),
            account_id: s["account_id"]
                .as_str()
                .expect("source.account_id")
                .to_string(),
            channel_id: s["channel_id"].as_str().map(str::to_string),
        });
    PlatformEvent {
        platform: golden["platform"].as_str().expect("platform").to_string(),
        event_type: golden["event_type"]
            .as_str()
            .expect("event_type")
            .to_string(),
        actor: golden["actor"].as_str().map(str::to_string),
        payload: golden["payload"]
            .as_object()
            .expect("payload object")
            .clone(),
        occurred_at: "2026-10-12T00:00:00.000Z".to_string(),
        source,
    }
}

/// Payload fields that carry a raw personal handle and MUST be tokenized.
const RAW_NAME_FIELDS: &[&str] = &[
    "user_login",
    "user_display_name",
    "display_name",
    "author",
    "broadcaster_login",
];

/// Runs the classifier, peeling off each unclassified field and retrying, so
/// the FULL unclassified set is reported independent of map iteration order.
async fn unclassified_fields(event: &PlatformEvent) -> BTreeSet<String> {
    let mut probe = event.clone();
    let mut rejected = BTreeSet::new();
    for _ in 0..64 {
        match tokenize_event(&probe, "tenant-parity", &SequentialMinter).await {
            Ok(_) => return rejected,
            Err(TokenizeError::UnclassifiedField { field }) => {
                probe.payload.remove(&field);
                rejected.insert(field);
            }
            Err(TokenizeError::UnclassifiedMetadataField { field }) => {
                if let Some(Value::Object(meta)) = probe.payload.get_mut("metadata") {
                    meta.remove(&field);
                }
                rejected.insert(format!("metadata.{field}"));
            }
            Err(other) => panic!("unexpected tokenize error: {other}"),
        }
    }
    panic!("classifier peel loop did not converge");
}

#[tokio::test]
async fn every_corpus_event_is_classified_or_explicitly_rejected_by_pii_tokenize() {
    let vectors = load_vectors();
    assert!(
        !vectors.is_empty(),
        "corpus is empty -- zero vectors examined is a FAILURE"
    );

    let (mut passed, mut rejected_as_declared, mut skipped_drops) = (0usize, 0usize, 0usize);
    let mut failures: Vec<String> = Vec::new();

    for (id, vector) in &vectors {
        if vector["outcome"] == "dropped" {
            skipped_drops += 1;
            continue;
        }
        let event = event_from_golden(&vector["golden"]);
        let expectation = vector["pii_classifier"].as_str().expect("pii_classifier");
        let unclassified = unclassified_fields(&event).await;

        if expectation == "pass" {
            if !unclassified.is_empty() {
                failures.push(format!(
                    "{id}: expected pass, unclassified fields {unclassified:?}"
                ));
                continue;
            }
            let tokenized = tokenize_event(&event, "tenant-parity", &SequentialMinter)
                .await
                .unwrap_or_else(|e| {
                    panic!("{id}: tokenize_event failed after classification: {e}")
                });
            if let Some(raw_actor) = &event.actor {
                match &tokenized.actor {
                    Some(a) if a.starts_with("{user:") && a != raw_actor => {}
                    other => failures.push(format!(
                        "{id}: actor not tokenized ({raw_actor:?} -> {other:?})"
                    )),
                }
            }
            for field in RAW_NAME_FIELDS {
                if let Some(raw) = event.payload.get(*field).and_then(Value::as_str) {
                    match tokenized.payload.get(*field).and_then(Value::as_str) {
                        Some(t) if t.starts_with("{user:") && t != raw => {}
                        other => failures.push(format!(
                            "{id}: payload.{field} not tokenized ({raw:?} -> {other:?})"
                        )),
                    }
                }
            }
            // A free-text @mention / <@id> must never survive verbatim.
            if let Some(text) = event.payload.get("text").and_then(Value::as_str) {
                let has_mention = text.contains("<@")
                    || text
                        .split_whitespace()
                        .any(|w| w.starts_with('@') && w.len() > 1);
                let out_text = tokenized
                    .payload
                    .get("text")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if has_mention && out_text == text {
                    failures.push(format!(
                        "{id}: free-text mention left untokenized: {text:?}"
                    ));
                }
            }
            passed += 1;
        } else if let Some(expected) = expectation.strip_prefix("reject:") {
            let actual = unclassified.into_iter().collect::<Vec<_>>().join(",");
            if actual != expected {
                failures.push(format!(
                    "{id}: pii_classifier expected reject:{expected} but classifier says reject:{actual}"
                ));
            } else {
                rejected_as_declared += 1;
            }
        } else {
            failures.push(format!("{id}: unknown pii_classifier {expectation:?}"));
        }
    }

    println!(
        "parity_pii_tokenize: {} vectors ({passed} accepted+tokenized, {rejected_as_declared} rejected as declared, \
         {skipped_drops} drop-expected skipped)",
        vectors.len()
    );
    assert!(
        failures.is_empty(),
        "PII-tokenize gate failures:\n{}",
        failures.join("\n")
    );
    assert!(
        passed >= 20,
        "only {passed} vectors passed the classifier -- gate would be vacuous"
    );
}
