//! Twitch EventSub subscription management: the Helix request templates for
//! `eventsub.{create,delete,list}` and the reconcile pass that drives them from the hub-api
//! desired-state contract (`docs/EVENTSUB_DESIRED_STATE.md`).
//!
//! # Templates (host-executed, APP credential)
//! * `create` -> `POST /helix/eventsub/subscriptions`. The condition names ONLY the
//!   `{{TWITCH_APP_BROADCASTER_ID}}` host slot (the community connection's broadcaster).
//!   `transport.callback` and `transport.secret` are HOST-DUTY slots filled from the work item's
//!   opaque `callbackKey`; this module never receives, builds or sees either value, and the
//!   template carries no `callbackKey` at all.
//! * `delete` -> `DELETE .../subscriptions?id=<twitch subscription id>`; `list` -> first page.
//!
//! # Reconcile ([`reconcile`])
//! One pass: `POST /internal/eventsub/reconcile` -> execute each work item through the host ->
//! `POST /internal/eventsub/results` echoing every item's single-use `nonce`. Orphan GC (deleting
//! Twitch-side subscriptions the ledger no longer lists) runs ONLY when the response says
//! `enabledIdsComplete`, the Twitch listing is a complete single page, and only for
//! subscriptions pointing at our webhook path; it is capped per pass.

use serde::Deserialize;
use serde_json::{Value, json};

use crate::common::FrameErr;
use crate::sender::{Tpl, id_arg, reject_unsafe};

/// The kinds this module owns.
pub const KINDS: &[&str] = &["eventsub.create", "eventsub.delete", "eventsub.list"];

/// Slot: APP access token.
pub const SLOT_APP_TOKEN: &str = "{{TWITCH_APP_TOKEN}}";
/// Slot: APP client id.
pub const SLOT_APP_CLIENT_ID: &str = "{{TWITCH_APP_CLIENT_ID}}";
/// Slot: the connection's broadcaster user id (host-resolved).
pub const SLOT_APP_BROADCASTER: &str = "{{TWITCH_APP_BROADCASTER_ID}}";
/// Duty slot: webhook callback URL (host-built from `callbackKey`).
pub const SLOT_CALLBACK: &str = "{{TWITCH_EVENTSUB_CALLBACK}}";
/// Duty slot: webhook secret (host-fetched by `callbackKey`).
pub const SLOT_SECRET: &str = "{{TWITCH_EVENTSUB_SECRET}}";
/// Duty slots a template may carry without a `secret-refs` handle.
pub const DUTY_SLOTS: &[&str] = &[SLOT_CALLBACK, SLOT_SECRET, "{{TWITCH_SEGMENT_ID}}"];

const HELIX_SUBS: &str = "https://api.twitch.tv/helix/eventsub/subscriptions";
/// Path marker of our webhook callbacks; GC never touches a subscription without it.
const WEBHOOK_MARKER: &str = "/eventsub/twitch/webhook/";
/// Most orphans one pass deletes.
pub const MAX_GC_PER_PASS: usize = 25;

/// `(type, version)` pairs hub-api may ask for (the seven types `eventsub.rs` ingests).
pub const SUPPORTED: &[(&str, &str)] = &[
    ("channel.follow", "2"),
    ("channel.subscribe", "1"),
    ("channel.subscription.gift", "1"),
    ("channel.cheer", "1"),
    ("channel.raid", "1"),
    ("stream.online", "1"),
    ("stream.offline", "1"),
];

fn app_headers(json_body: bool) -> Vec<(String, String)> {
    let mut h = vec![
        ("Authorization".to_string(), format!("Bearer {SLOT_APP_TOKEN}")),
        ("Client-Id".to_string(), SLOT_APP_CLIENT_ID.to_string()),
    ];
    if json_body {
        h.push(("Content-Type".to_string(), "application/json".to_string()));
    }
    h
}

fn app_refs() -> Vec<(String, String)> {
    vec![
        (SLOT_APP_TOKEN.to_string(), "app.token".to_string()),
        (SLOT_APP_CLIENT_ID.to_string(), "app.client_id".to_string()),
        (SLOT_APP_BROADCASTER.to_string(), "app.broadcaster_id".to_string()),
    ]
}

/// The version as the exact string Helix expects; hub-api sends an integer, tests a string.
fn version_str(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => n.as_u64().map(|n| n.to_string()),
        _ => None,
    }
}

/// Builds the template for `kind`. Unknown kinds fail loudly as `Unsupported`.
pub fn build(kind: &str, payload: &Value) -> Result<Tpl, FrameErr> {
    let mal = |m: &str| FrameErr::Malformed(m.to_string());
    match kind {
        "eventsub.create" => {
            let raw = payload
                .get("text")
                .and_then(Value::as_str)
                .ok_or_else(|| mal("payload needs `text` with {type, version}"))?;
            let p: Value = serde_json::from_str(raw).map_err(|_| mal("`text` is not JSON"))?;
            let obj = p
                .as_object()
                .filter(|m| m.keys().all(|k| k == "type" || k == "version"))
                .ok_or_else(|| mal("`text` must carry only type and version"))?;
            let ty = obj
                .get("type")
                .and_then(Value::as_str)
                .ok_or_else(|| mal("type is required"))?;
            let ver = obj
                .get("version")
                .and_then(version_str)
                .ok_or_else(|| mal("version is required"))?;
            if !SUPPORTED.contains(&(ty, ver.as_str())) {
                return Err(mal("unsupported EventSub type/version"));
            }
            let condition = match ty {
                "channel.follow" => json!({
                    "broadcaster_user_id": SLOT_APP_BROADCASTER,
                    "moderator_user_id": SLOT_APP_BROADCASTER,
                }),
                "channel.raid" => json!({ "to_broadcaster_user_id": SLOT_APP_BROADCASTER }),
                _ => json!({ "broadcaster_user_id": SLOT_APP_BROADCASTER }),
            };
            let body = json!({
                "type": ty,
                "version": ver,
                "condition": condition,
                "transport": {
                    "method": "webhook",
                    "callback": SLOT_CALLBACK,
                    "secret": SLOT_SECRET,
                },
            });
            Ok(Tpl {
                method: "POST".to_string(),
                url: HELIX_SUBS.to_string(),
                headers: app_headers(true),
                body: Some(body.to_string().into_bytes()),
                secret_refs: app_refs(),
            })
        }
        "eventsub.delete" => {
            let id = id_arg(payload, "message_id", false)?;
            Ok(Tpl {
                method: "DELETE".to_string(),
                url: format!("{HELIX_SUBS}?id={id}"),
                headers: app_headers(false),
                body: None,
                secret_refs: app_refs()[..2].to_vec(),
            })
        }
        "eventsub.list" => {
            if payload.get("text").is_some() || payload.get("message_id").is_some() {
                return Err(mal("eventsub.list takes no parameters"));
            }
            Ok(Tpl {
                method: "GET".to_string(),
                url: HELIX_SUBS.to_string(),
                headers: app_headers(false),
                body: None,
                secret_refs: app_refs()[..2].to_vec(),
            })
        }
        other => Err(FrameErr::Unsupported(format!(
            "twitch {other} is not an EventSub kind"
        ))),
    }
}

// ----------------------------- reconcile -----------------------------

/// A fixed-code reconcile failure (never content).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReconcileError(pub &'static str);

/// hub-api work-item action codes.
const ACTION_CREATE: u8 = 1;
const ACTION_DELETE: u8 = 2;
/// Result outcome codes.
const OUTCOME_CREATED: u8 = 1;
const OUTCOME_DELETED: u8 = 2;
const OUTCOME_FAILED: u8 = 3;

/// Why a host execution failed, mapped to hub-api `FailureReason` codes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExecFail {
    /// 401/403: the app token or connection was refused (`AUTH_REJECTED` = 1).
    AuthRejected,
    /// 429 (`RATE_LIMITED` = 3).
    RateLimited,
    /// Other 4xx: Twitch rejected the request (`TWITCH_REJECTED` = 4).
    TwitchRejected,
    /// 5xx / transport (`TRANSPORT_ERROR` = 5).
    Transport,
    /// The host could not resolve the connection / app credential (`CONNECTION_UNAVAILABLE` = 7).
    ConnectionUnavailable,
    /// The HTTP status was 404 (only meaningful for delete: already gone).
    NotFound,
}

impl ExecFail {
    fn code(self) -> u8 {
        match self {
            Self::AuthRejected => 1,
            Self::RateLimited => 3,
            Self::TwitchRejected | Self::NotFound => 4,
            Self::Transport => 5,
            Self::ConnectionUnavailable => 7,
        }
    }
}

/// What the reconcile pass needs from its environment: the hub-api internal API and the host
/// outbound executor. (The wasm world has no export/import for either yet; the host side
/// supplies these.)
pub trait ReconcileHost {
    /// `POST <path>` to hub-api with a machine identity; returns the JSON reply.
    fn hub_post(&self, path: &str, body: &Value) -> Result<Value, ReconcileError>;
    /// Executes outbound `kind` for `community_id` with `payload` (the producer envelope);
    /// returns the Helix reply body on success.
    fn execute(&self, community_id: i64, kind: &str, payload: &Value) -> Result<String, ExecFail>;
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct WorkItem {
    subscription_id: String,
    action: u8,
    community_id: i64,
    twitch_type: String,
    version: u64,
    callback_key: String,
    nonce: String,
    #[serde(default)]
    twitch_subscription_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Sweep {
    #[serde(default)]
    next_community_id: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ReconcileReply {
    sweep: Sweep,
    #[serde(default)]
    work: Vec<WorkItem>,
    #[serde(default)]
    enabled_twitch_subscription_ids: Vec<String>,
    #[serde(default)]
    enabled_ids_complete: bool,
}

/// Counts of one reconcile pass (the denominators a caller reports).
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Summary {
    /// Work items received.
    pub items: usize,
    /// Created on Twitch.
    pub created: usize,
    /// Deleted on Twitch.
    pub deleted: usize,
    /// Reported failed.
    pub failed: usize,
    /// Work items refused by validation (reported failed, never executed).
    pub refused: usize,
    /// Orphans deleted by GC.
    pub gc_deleted: usize,
    /// Whether GC was allowed to run (`enabledIdsComplete`).
    pub gc_eligible: bool,
    /// Pages swept.
    pub pages: usize,
}

fn is_uuid(s: &str) -> bool {
    s.len() == 36
        && s.bytes().enumerate().all(|(i, b)| match i {
            8 | 13 | 18 | 23 => b == b'-',
            _ => b.is_ascii_hexdigit(),
        })
}

/// A work item is executable only with a known type/version, a URL-safe callback key and UUID
/// ids; anything else is reported failed without being executed.
fn valid(w: &WorkItem) -> bool {
    let ver = w.version.to_string();
    is_uuid(&w.subscription_id)
        && is_uuid(&w.nonce)
        && (8..=128).contains(&w.callback_key.len())
        && w.callback_key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        && SUPPORTED.contains(&(w.twitch_type.as_str(), ver.as_str()))
        && matches!(w.action, ACTION_CREATE | ACTION_DELETE)
        && w.twitch_subscription_id
            .as_deref()
            .is_none_or(|i| reject_unsafe("id", i).is_ok() && !i.is_empty() && i.len() <= 64)
}

fn failed(w: &WorkItem, reason: u8) -> Value {
    json!({
        "subscriptionId": w.subscription_id,
        "outcome": OUTCOME_FAILED,
        "failureReason": reason,
        "nonce": w.nonce,
    })
}

/// The Twitch subscription id out of a create reply (`{data:[{id}]}`).
fn created_id(body: &str) -> Option<String> {
    let v: Value = serde_json::from_str(body).ok()?;
    let id = v.get("data")?.as_array()?.first()?.get("id")?.as_str()?;
    (!id.is_empty() && id.len() <= 64 && reject_unsafe("id", id).is_ok()).then(|| id.to_string())
}

/// Runs one full reconcile pass (all pages), executing work and reporting results.
///
/// # Errors
/// [`ReconcileError`] when hub-api cannot be reached or answers a malformed reply; item-level
/// failures never abort the pass, they are reported to hub-api as `failed`.
pub fn reconcile<H: ReconcileHost>(host: &H, page_limit: u32) -> Result<Summary, ReconcileError> {
    let mut sum = Summary::default();
    let mut after = 0_i64;
    loop {
        let reply = host.hub_post(
            "/api/v1/internal/eventsub/reconcile",
            &json!({ "afterCommunityId": after, "limit": page_limit }),
        )?;
        let r: ReconcileReply =
            serde_json::from_value(reply).map_err(|_| ReconcileError("bad_reconcile_reply"))?;
        sum.pages += 1;
        sum.items += r.work.len();
        let mut results = Vec::with_capacity(r.work.len());
        for w in &r.work {
            if !valid(w) {
                sum.refused += 1;
                sum.failed += 1;
                results.push(failed(w, ExecFail::TwitchRejected.code()));
                continue;
            }
            results.push(execute_item(host, w, &mut sum));
        }
        if !results.is_empty() {
            host.hub_post(
                "/api/v1/internal/eventsub/results",
                &json!({ "results": results }),
            )?;
        }
        // GC only on a response that is itself a complete, single-page sweep.
        if r.enabled_ids_complete && after == 0 && r.sweep.next_community_id.is_none() {
            sum.gc_eligible = true;
            sum.gc_deleted += gc(host, &r.enabled_twitch_subscription_ids);
        }
        match r.sweep.next_community_id {
            Some(next) if next > after => after = next,
            Some(_) => return Err(ReconcileError("sweep_cursor_not_advancing")),
            None => return Ok(sum),
        }
    }
}

fn execute_item<H: ReconcileHost>(host: &H, w: &WorkItem, sum: &mut Summary) -> Value {
    if w.action == ACTION_CREATE {
        let text = json!({
            "type": w.twitch_type,
            "version": w.version,
            "callbackKey": w.callback_key,
        })
        .to_string();
        match host.execute(w.community_id, "eventsub.create", &json!({ "text": text })) {
            Ok(body) => match created_id(&body) {
                Some(id) => {
                    sum.created += 1;
                    json!({
                        "subscriptionId": w.subscription_id,
                        "outcome": OUTCOME_CREATED,
                        "twitchSubscriptionId": id,
                        "nonce": w.nonce,
                    })
                }
                None => {
                    sum.failed += 1;
                    failed(w, ExecFail::Transport.code())
                }
            },
            Err(e) => {
                sum.failed += 1;
                failed(w, e.code())
            }
        }
    } else {
        let Some(id) = w.twitch_subscription_id.as_deref() else {
            sum.refused += 1;
            sum.failed += 1;
            return failed(w, ExecFail::TwitchRejected.code());
        };
        match host.execute(w.community_id, "eventsub.delete", &json!({ "message_id": id })) {
            // Already gone is the desired end state.
            Ok(_) | Err(ExecFail::NotFound) => {
                sum.deleted += 1;
                json!({
                    "subscriptionId": w.subscription_id,
                    "outcome": OUTCOME_DELETED,
                    "twitchSubscriptionId": id,
                    "nonce": w.nonce,
                })
            }
            Err(e) => {
                sum.failed += 1;
                failed(w, e.code())
            }
        }
    }
}

/// Deletes Twitch-side subscriptions pointing at our webhook path that the ledger no longer
/// lists. Needs a COMPLETE listing (one page, no cursor) or it deletes nothing.
fn gc<H: ReconcileHost>(host: &H, enabled: &[String]) -> usize {
    let Ok(body) = host.execute(0, "eventsub.list", &json!({})) else {
        return 0;
    };
    let Ok(v) = serde_json::from_str::<Value>(&body) else {
        return 0;
    };
    let truncated = v
        .get("pagination")
        .and_then(|p| p.get("cursor"))
        .is_some_and(|c| c.as_str().is_some_and(|s| !s.is_empty()));
    let Some(rows) = v.get("data").and_then(Value::as_array) else {
        return 0;
    };
    if truncated {
        return 0;
    }
    let mut n = 0;
    for row in rows {
        if n >= MAX_GC_PER_PASS {
            break;
        }
        let (Some(id), Some(cb)) = (
            row.get("id").and_then(Value::as_str),
            row.get("transport")
                .and_then(|t| t.get("callback"))
                .and_then(Value::as_str),
        ) else {
            continue;
        };
        if !cb.contains(WEBHOOK_MARKER) || enabled.iter().any(|e| e == id) {
            continue;
        }
        if host
            .execute(0, "eventsub.delete", &json!({ "message_id": id }))
            .is_ok()
        {
            n += 1;
        }
    }
    n
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::sender::build_request;

    fn create_payload(ty: &str, ver: Value) -> String {
        json!({"text": json!({"type": ty, "version": ver}).to_string()}).to_string()
    }

    #[test]
    fn create_template_has_only_host_slots_and_no_secret_or_callback() {
        for (ty, ver) in SUPPORTED {
            let t = build_request("eventsub.create", "s", &create_payload(ty, json!(ver))).unwrap();
            assert_eq!(t.method, "POST");
            assert_eq!(t.url, "https://api.twitch.tv/helix/eventsub/subscriptions");
            let b: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
            assert_eq!(b["type"], *ty);
            assert_eq!(b["version"], *ver);
            assert_eq!(b["transport"]["callback"], SLOT_CALLBACK);
            assert_eq!(b["transport"]["secret"], SLOT_SECRET);
            assert_eq!(b["transport"]["method"], "webhook");
            let cond = serde_json::to_string(&b["condition"]).unwrap();
            assert!(cond.contains(SLOT_APP_BROADCASTER), "{ty}");
            assert!(!cond.chars().any(|c| c.is_ascii_digit()), "no literal ids: {cond}");
        }
        let follow = build_request("eventsub.create", "s", &create_payload("channel.follow", json!(2))).unwrap();
        let b: Value = serde_json::from_slice(&follow.body.unwrap()).unwrap();
        assert!(b["condition"].get("moderator_user_id").is_some());
        let raid = build_request("eventsub.create", "s", &create_payload("channel.raid", json!(1))).unwrap();
        let b: Value = serde_json::from_slice(&raid.body.unwrap()).unwrap();
        assert!(b["condition"].get("to_broadcaster_user_id").is_some());
        assert!(!follow.headers.iter().any(|(_, v)| v.contains("{{TWITCH_TOKEN}}")), "app token only");
    }

    #[test]
    fn create_refuses_unknown_types_versions_and_smuggled_fields() {
        for p in [
            create_payload("channel.follow", json!(1)),
            create_payload("channel.update", json!(1)),
            create_payload("stream.online", json!({})),
            json!({"text": json!({"type":"stream.online","version":"1","callbackKey":"abcdefgh"}).to_string()}).to_string(),
            json!({"text": json!({"type":"stream.online","version":"1","transport":{"secret":"x"}}).to_string()}).to_string(),
            json!({"text": "nope"}).to_string(),
            json!({}).to_string(),
        ] {
            assert!(build_request("eventsub.create", "s", &p).is_err(), "{p}");
        }
    }

    #[test]
    fn delete_and_list_templates() {
        let d = build_request("eventsub.delete", "s", r#"{"message_id":"a1b2-c3"}"#).unwrap();
        assert_eq!(d.method, "DELETE");
        assert!(d.url.ends_with("?id=a1b2-c3") && d.body.is_none());
        for bad in ["a&b", "", "x{{TWITCH_APP_TOKEN}}"] {
            let p = json!({"message_id": bad}).to_string();
            assert!(build_request("eventsub.delete", "s", &p).is_err(), "{bad}");
        }
        let l = build_request("eventsub.list", "s", "{}").unwrap();
        assert_eq!(l.method, "GET");
        assert!(build_request("eventsub.list", "s", r#"{"text":"x"}"#).is_err());
        assert!(matches!(build("eventsub.nope", &json!({})), Err(FrameErr::Unsupported(_))));
    }

    // ------------------------- reconcile -------------------------

    const SUB: &str = "11111111-1111-4111-8111-111111111111";
    const SUB2: &str = "22222222-2222-4222-8222-222222222222";
    const NONCE: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const NONCE2: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

    fn item(sub: &str, action: u8, ty: &str, ver: u64, tw: Option<&str>, nonce: &str) -> Value {
        json!({"subscriptionId":sub,"action":action,"tenantId":1,"communityId":7,"connectionId":3,
               "subType":1,"twitchType":ty,"version":ver,"callbackUrl":"https://hub/x","callbackKey":"key-abcdefgh",
               "nonce":nonce,"twitchSubscriptionId":tw})
    }

    struct Fake {
        pages: RefCell<Vec<Value>>,
        posts: RefCell<Vec<(String, Value)>>,
        execs: RefCell<Vec<(i64, String, Value)>>,
        exec_result: Box<dyn Fn(&str, &Value) -> Result<String, ExecFail>>,
    }
    impl ReconcileHost for Fake {
        fn hub_post(&self, path: &str, body: &Value) -> Result<Value, ReconcileError> {
            self.posts.borrow_mut().push((path.to_string(), body.clone()));
            if path.ends_with("/reconcile") {
                Ok(self.pages.borrow_mut().remove(0))
            } else {
                Ok(json!({"success":true,"applied":1}))
            }
        }
        fn execute(&self, c: i64, kind: &str, payload: &Value) -> Result<String, ExecFail> {
            self.execs.borrow_mut().push((c, kind.to_string(), payload.clone()));
            (self.exec_result)(kind, payload)
        }
    }
    fn fake(pages: Vec<Value>, f: impl Fn(&str, &Value) -> Result<String, ExecFail> + 'static) -> Fake {
        Fake {
            pages: RefCell::new(pages),
            posts: RefCell::new(vec![]),
            execs: RefCell::new(vec![]),
            exec_result: Box::new(f),
        }
    }
    fn page(work: Vec<Value>, enabled: &[&str], complete: bool, next: Option<i64>) -> Value {
        json!({"success":true,"sweep":{"communities":1,"created":0,"markedDelete":0,"removed":0,"markedStale":0,"requeued":0,"nextCommunityId":next},
               "work":work,"enabledTwitchSubscriptionIds":enabled,"enabledIdsComplete":complete})
    }
    fn results(h: &Fake) -> Vec<Value> {
        h.posts
            .borrow()
            .iter()
            .filter(|(p, _)| p.ends_with("/results"))
            .flat_map(|(_, b)| b["results"].as_array().unwrap().clone())
            .collect()
    }

    #[test]
    fn create_reports_the_twitch_id_with_the_nonce_and_never_carries_a_secret() {
        let h = fake(
            vec![page(vec![item(SUB, 1, "stream.online", 1, None, NONCE)], &[], false, None)],
            |_, _| Ok(r#"{"data":[{"id":"tw-sub-1"}]}"#.to_string()),
        );
        let s = reconcile(&h, 100).unwrap();
        assert_eq!((s.items, s.created, s.failed), (1, 1, 0));
        let r = results(&h);
        assert_eq!(
            r[0],
            json!({"subscriptionId":SUB,"outcome":1,"twitchSubscriptionId":"tw-sub-1","nonce":NONCE})
        );
        let execs = h.execs.borrow();
        assert_eq!(execs[0].0, 7, "community scope travels with the op");
        let text: Value = serde_json::from_str(execs[0].2["text"].as_str().unwrap()).unwrap();
        assert_eq!(text["callbackKey"], "key-abcdefgh");
        assert!(!execs[0].2.to_string().contains("secret"));
        // The guest-visible payload the host forwards builds a secret-free template.
        let t = build_request("eventsub.create", "s", &json!({"text": json!({"type":text["type"],"version":text["version"]}).to_string()}).to_string()).unwrap();
        assert!(!String::from_utf8(t.body.unwrap()).unwrap().contains("key-abcdefgh"));
    }

    #[test]
    fn failures_map_to_reasons_and_are_reported_not_aborted() {
        let h = fake(
            vec![page(
                vec![
                    item(SUB, 1, "stream.online", 1, None, NONCE),
                    item(SUB2, 1, "stream.offline", 1, None, NONCE2),
                ],
                &[],
                false,
                None,
            )],
            |_, p| {
                if p["text"].as_str().unwrap().contains("stream.online") {
                    Err(ExecFail::AuthRejected)
                } else {
                    Err(ExecFail::RateLimited)
                }
            },
        );
        let s = reconcile(&h, 100).unwrap();
        assert_eq!((s.created, s.failed), (0, 2));
        let r = results(&h);
        assert_eq!((r[0]["outcome"].as_u64(), r[0]["failureReason"].as_u64()), (Some(3), Some(1)));
        assert_eq!(r[1]["failureReason"], 3);
        assert_eq!(r[1]["nonce"], NONCE2);
    }

    #[test]
    fn invalid_items_are_reported_failed_and_never_executed() {
        let mut bad_key = item(SUB, 1, "stream.online", 1, None, NONCE);
        bad_key["callbackKey"] = json!("a/b");
        let h = fake(
            vec![page(
                vec![
                    bad_key,
                    item(SUB2, 1, "channel.update", 1, None, NONCE2),
                    item(SUB, 2, "stream.online", 1, None, "not-a-uuid"),
                    item(SUB, 2, "stream.online", 1, None, NONCE), // delete without twitch id
                ],
                &[],
                false,
                None,
            )],
            |_, _| panic!("nothing may execute"),
        );
        let s = reconcile(&h, 100).unwrap();
        assert_eq!((s.items, s.refused, s.failed), (4, 4, 4));
        assert!(h.execs.borrow().is_empty());
    }

    #[test]
    fn delete_succeeds_and_already_gone_counts_as_deleted() {
        for res in [Ok(String::new()), Err(ExecFail::NotFound)] {
            let r2 = res.clone();
            let h = fake(
                vec![page(vec![item(SUB, 2, "stream.online", 1, Some("tw-1"), NONCE)], &[], false, None)],
                move |kind, p| {
                    assert_eq!(kind, "eventsub.delete");
                    assert_eq!(p, &json!({"message_id":"tw-1"}));
                    r2.clone()
                },
            );
            let s = reconcile(&h, 100).unwrap();
            assert_eq!(s.deleted, 1);
            assert_eq!(results(&h)[0]["outcome"], 2);
        }
        let h = fake(
            vec![page(vec![item(SUB, 2, "stream.online", 1, Some("tw-1"), NONCE)], &[], false, None)],
            |_, _| Err(ExecFail::Transport),
        );
        assert_eq!(reconcile(&h, 100).unwrap().failed, 1);
    }

    fn listing(rows: &[(&str, &str)], cursor: Option<&str>) -> String {
        let data: Vec<Value> = rows
            .iter()
            .map(|(id, cb)| json!({"id":id,"transport":{"method":"webhook","callback":cb}}))
            .collect();
        json!({"data":data,"pagination":cursor.map(|c| json!({"cursor":c})).unwrap_or(json!({}))}).to_string()
    }

    #[test]
    fn gc_runs_only_when_enabled_ids_are_complete_and_deletes_only_our_orphans() {
        let list = listing(
            &[
                ("keep", "https://h/eventsub/twitch/webhook/k1"),
                ("orphan", "https://h/eventsub/twitch/webhook/k2"),
                ("foreign", "https://other.example/hook"),
            ],
            None,
        );
        let l2 = list.clone();
        let h = fake(vec![page(vec![], &["keep"], true, None)], move |kind, p| {
            if kind == "eventsub.list" { Ok(l2.clone()) } else { assert_eq!(p["message_id"], "orphan"); Ok(String::new()) }
        });
        let s = reconcile(&h, 100).unwrap();
        assert!(s.gc_eligible);
        assert_eq!(s.gc_deleted, 1);

        // Incomplete -> no listing, no deletes.
        let h = fake(vec![page(vec![], &["keep"], false, None)], |_, _| panic!("no GC when incomplete"));
        let s = reconcile(&h, 100).unwrap();
        assert!(!s.gc_eligible);
        assert!(h.execs.borrow().is_empty());

        // A multi-page sweep is never GC-eligible even if a page claims completeness.
        let h = fake(
            vec![page(vec![], &[], true, Some(5)), page(vec![], &[], true, None)],
            |_, _| panic!("no GC on a paged sweep"),
        );
        let s = reconcile(&h, 100).unwrap();
        assert_eq!((s.pages, s.gc_eligible), (2, false));
    }

    #[test]
    fn gc_refuses_a_truncated_twitch_listing() {
        let list = listing(&[("orphan", "https://h/eventsub/twitch/webhook/k")], Some("more"));
        let h = fake(vec![page(vec![], &[], true, None)], move |kind, _| {
            assert_eq!(kind, "eventsub.list", "nothing may be deleted from a partial listing");
            Ok(list.clone())
        });
        assert_eq!(reconcile(&h, 100).unwrap().gc_deleted, 0);
    }

    #[test]
    fn gc_is_capped_per_pass() {
        let rows: Vec<(String, String)> = (0..40)
            .map(|i| (format!("o{i}"), "https://h/eventsub/twitch/webhook/k".to_string()))
            .collect();
        let refs: Vec<(&str, &str)> = rows.iter().map(|(a, b)| (a.as_str(), b.as_str())).collect();
        let list = listing(&refs, None);
        let h = fake(vec![page(vec![], &[], true, None)], move |kind, _| {
            if kind == "eventsub.list" { Ok(list.clone()) } else { Ok(String::new()) }
        });
        assert_eq!(reconcile(&h, 100).unwrap().gc_deleted, MAX_GC_PER_PASS);
    }

    #[test]
    fn paging_follows_next_community_and_a_stuck_cursor_fails_loudly() {
        let h = fake(
            vec![
                page(vec![item(SUB, 1, "stream.online", 1, None, NONCE)], &[], false, Some(10)),
                page(vec![item(SUB2, 1, "stream.offline", 1, None, NONCE2)], &[], false, None),
            ],
            |_, _| Ok(r#"{"data":[{"id":"x"}]}"#.to_string()),
        );
        let s = reconcile(&h, 50).unwrap();
        assert_eq!((s.pages, s.created), (2, 2));
        let posts = h.posts.borrow();
        let recon: Vec<_> = posts.iter().filter(|(p, _)| p.ends_with("/reconcile")).collect();
        assert_eq!(recon[1].1["afterCommunityId"], 10);
        drop(posts);
        let h = fake(vec![page(vec![], &[], false, Some(0))], |_, _| Ok(String::new()));
        assert_eq!(reconcile(&h, 50), Err(ReconcileError("sweep_cursor_not_advancing")));
        let h = fake(vec![json!({"nope":1})], |_, _| Ok(String::new()));
        assert_eq!(reconcile(&h, 50), Err(ReconcileError("bad_reconcile_reply")));
    }
}
