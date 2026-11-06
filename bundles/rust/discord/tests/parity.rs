//! Golden-payload parity: every `tests/data_plane_parity/corpus/discord/*.json`
//! vector (the committed oracle of the hardcoded `ingest/discord.rs` +
//! `normalize_discord` path) must produce the SAME normalized event through the
//! connector's `on-frame`, with the vector's resolver context behind the `Host`
//! port. Prints `parity n/total`; a zero-vector run FAILS.

use std::cell::RefCell;
use std::fs;
use std::path::PathBuf;

use serde_json::{Value, json};
use waddle_bundle_discord_overlay::error::{ConnectorError, Result};
use waddle_bundle_discord_overlay::perms::GuildInfo;
use waddle_bundle_discord_overlay::receiver::{ConnCtx, Host, Level, on_frame};

/// Resolver context straight from a corpus vector's `context` object.
struct VectorHost {
    owner: String,
    roles: Vec<(String, u64)>,
    member_roles: Vec<String>,
    logs: RefCell<Vec<String>>,
}

impl Host for VectorHost {
    fn guild(&self, _g: &str) -> Result<GuildInfo> {
        Ok(GuildInfo {
            owner_id: self.owner.clone(),
            roles: self.roles.clone(),
        })
    }
    fn member_roles(&self, _g: &str, _u: &str) -> Result<Vec<String>> {
        Ok(self.member_roles.clone())
    }
    fn identity(&self, id: &str) -> Result<String> {
        Ok(format!("uuid-of-{id}"))
    }
    fn log(&self, _l: Level, m: &str, f: &str) {
        self.logs.borrow_mut().push(format!("{m} {f}"));
    }
}

fn corpus_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/data_plane_parity/corpus/discord")
}

fn host_for(ctx: &Value) -> VectorHost {
    let strs = |k: &str| -> Vec<String> {
        ctx[k]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default()
    };
    let roles = ctx["guild_roles"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|p| Some((p[0].as_str()?.to_string(), p[1].as_u64()?)))
                .collect()
        })
        .unwrap_or_default();
    VectorHost {
        owner: ctx["guild_owner_id"]
            .as_str()
            .unwrap_or_default()
            .to_string(),
        roles,
        member_roles: strs("member_role_ids"),
        logs: RefCell::new(Vec::new()),
    }
}

#[test]
fn every_discord_corpus_vector_matches_the_hardcoded_golden() {
    let mut files: Vec<_> = fs::read_dir(corpus_dir())
        .expect("corpus dir readable")
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    assert_eq!(
        files.len(),
        10,
        "expected the 10 discord vectors, found {}",
        files.len()
    );

    let mut passed = 0usize;
    for path in &files {
        let v: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        let id = v["id"].as_str().unwrap().to_string();
        let host = host_for(&v["context"]);
        let session = json!({"self_user_id": v["context"]["self_user_id"]}).to_string();
        let ctx = ConnCtx {
            platform: "discord".into(),
            source_id: "opaque-src".into(),
            connection_id: "c1".into(),
            session_token: Some(session),
        };
        // The corpus `frame` is the MESSAGE_CREATE `d`; wrap it in the gateway envelope.
        let wire =
            serde_json::to_vec(&json!({"op":0,"s":1,"t":"MESSAGE_CREATE","d":v["frame"]})).unwrap();
        let events = on_frame(&host, &ctx, &wire, "2026-11-05T00:00:00.000Z")
            .unwrap_or_else(|e| panic!("{id}: on_frame failed: {e}"));

        match v["outcome"].as_str().unwrap() {
            "dropped" => assert!(events.is_empty(), "{id}: expected drop, got {events:?}"),
            "event" => {
                assert_eq!(events.len(), 1, "{id}");
                let e = &events[0];
                let g = &v["golden"];
                assert_eq!(e.platform, g["platform"], "{id} platform");
                assert_eq!(e.event_type, g["event_type"], "{id} event_type");
                // The golden actor is the RAW handle (pre-tokenization); the connector must emit the
                // tokenized UUID instead and never leak the handle.
                assert_eq!(
                    e.actor.as_deref(),
                    Some(
                        format!("uuid-of-{}", g["payload"]["author_id"].as_str().unwrap()).as_str()
                    ),
                    "{id} actor"
                );
                assert_ne!(json!(e.actor), g["actor"], "{id} raw handle leaked");
                let payload: Value = serde_json::from_str(&e.payload_json).unwrap();
                assert_eq!(payload, g["payload"], "{id} payload");
                assert_eq!(
                    e.occurred_at, "2026-11-05T00:00:00.000Z",
                    "{id} occurred_at"
                );
                // `source` is stamped by the HOST from the payload; prove it is derivable
                // without loss (account_id = guild or "dm", channel_id from the payload).
                let account = payload["guild_id"].as_str().unwrap_or("dm");
                assert_eq!(account, g["source"]["account_id"], "{id} source.account_id");
                assert_eq!(
                    payload["channel_id"], g["source"]["channel_id"],
                    "{id} source.channel_id"
                );
            }
            other => panic!("{id}: unknown outcome {other}"),
        }
        // Bundle logs stay PII-free: no username/content in any log line.
        for line in host.logs.borrow().iter() {
            assert!(
                !line.contains("someuser") && !line.contains("hello chat"),
                "{id}: PII in log {line}"
            );
        }
        passed += 1;
    }
    println!("parity {passed}/{}", files.len());
    assert_eq!(passed, 10);
}

#[test]
fn a_lookup_failure_diverges_from_the_golden_only_by_failing_closed() {
    // administrator_role normally yields is_mod=true; with the REST lookup down the
    // connector must degrade to false/false (the hardcoded resolver's fail-closed rule).
    struct Down;
    impl Host for Down {
        fn guild(&self, _: &str) -> Result<GuildInfo> {
            Err(ConnectorError::Backend("http timeout".into()))
        }
        fn member_roles(&self, _: &str, _: &str) -> Result<Vec<String>> {
            Err(ConnectorError::Backend("http timeout".into()))
        }
        fn identity(&self, id: &str) -> Result<String> {
            Ok(format!("uuid-of-{id}"))
        }
        fn log(&self, _: Level, _: &str, _: &str) {}
    }
    let v: Value = serde_json::from_str(
        &fs::read_to_string(corpus_dir().join("administrator_role.json")).unwrap(),
    )
    .unwrap();
    let ctx = ConnCtx {
        platform: "discord".into(),
        source_id: "s".into(),
        connection_id: "c".into(),
        session_token: None,
    };
    let wire =
        serde_json::to_vec(&json!({"op":0,"s":1,"t":"MESSAGE_CREATE","d":v["frame"]})).unwrap();
    let evs = on_frame(&Down, &ctx, &wire, "t").unwrap();
    let p: Value = serde_json::from_str(&evs[0].payload_json).unwrap();
    assert_eq!(
        (p["is_mod"].as_bool(), p["is_broadcaster"].as_bool()),
        (Some(false), Some(false))
    );
}
