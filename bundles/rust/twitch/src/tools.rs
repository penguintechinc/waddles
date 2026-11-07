//! Read tools exposed through `tool-provider`: `get_chatters`, `get_followers`,
//! `get_subscriptions`. Requests go out through the guarded `http` import with
//! header secret-refs (the bundle never sees a token); every returned user is
//! mapped to a canonical UUID via `identity.lookup`, so no handle or numeric
//! Twitch id leaves the connector.

use std::sync::Arc;

use serde_json::{Value, json};
use waddle_sdk::tool::{ToolError, ToolRegistry};

use crate::sender::{SLOT_BROADCASTER, SLOT_SENDER};

const HELIX: &str = "https://api.twitch.tv/helix";
/// Header secret-ref names the host injects (header -> secret ref).
pub const SECRET_REFS: &[(&str, &str)] = &[
    ("Authorization", "twitch_bearer"),
    ("Client-Id", "twitch_client_id"),
];

/// Guarded outbound GET (wasm glue: the `http` import).
pub trait HelixHost: Send + Sync {
    /// Sends a GET to `url` with [`SECRET_REFS`]; returns `(status, body)`.
    fn get(&self, url: &str) -> Result<(u16, Vec<u8>), ToolError>;
}

/// Platform-user-id -> canonical UUID (wasm glue: `identity.lookup`).
pub trait IdentityHost: Send + Sync {
    /// Resolves one Twitch numeric user id.
    fn uuid_for(&self, twitch_user_id: &str) -> Result<String, ToolError>;
}

/// Caller-supplied destination ids are refused outright: the channel and
/// moderator come from the host-pinned slots, never from tool arguments.
fn reject_destination_args(args: &Value) -> Result<(), ToolError> {
    for k in crate::sender::IGNORED_TARGET_KEYS {
        if args.get(*k).is_some() {
            return Err(ToolError::InvalidArgs(format!(
                "{k} is host-pinned and may not be supplied"
            )));
        }
    }
    Ok(())
}

/// Host-mediated moderation tools: `(tool name, outbound kind)`. The service bundle DECLARES
/// these (`exported_tools`, with `required_scope: community:moderate`); the guest has no
/// mutating egress, so the HOST serves an authorized call by queueing the pinned outbound op
/// (`kind`) whose Helix template [`crate::moderation`] / `sender.rs` build.
pub const MODERATION_TOOLS: &[(&str, &str)] = &[
    ("twitch.moderation.ban", "mod.ban"),
    ("twitch.moderation.timeout", "mod.timeout"),
    ("twitch.moderation.unban", "mod.unban"),
    ("twitch.moderation.delete_message", "chat.delete"),
];

/// One validated moderation tool call, ready for the host to queue.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModerationOp {
    /// Outbound kind (`mod.ban`, ...).
    pub kind: &'static str,
    /// Producer-envelope fields: `user` (caller's canonical UUID; the host detokenizes it to the
    /// numeric `user_id` the template needs), `text` (JSON-object text of `{duration?, reason?}`)
    /// and `message_id`. Never carries a channel/broadcaster/moderator.
    pub payload: Value,
}

fn is_uuid(s: &str) -> bool {
    s.len() == 36
        && s.bytes().enumerate().all(|(i, b)| match i {
            8 | 13 | 18 | 23 => b == b'-',
            _ => b.is_ascii_hexdigit(),
        })
}

fn bad(msg: &str) -> ToolError {
    ToolError::InvalidArgs(msg.to_string())
}

/// Validates a moderation tool call against its schema and returns the op to queue.
///
/// Schemas (all `additionalProperties: false`): `ban {user, reason?}`, `timeout {user,
/// duration 1..=1209600, reason?}`, `unban {user}`, `delete_message {message_id}`.
/// Destination keys (channel/broadcaster/moderator) are refused outright.
///
/// # Errors
/// `InvalidArgs` for an unknown tool, unknown key, or any value outside its schema.
pub fn moderation_op(tool: &str, args: &Value) -> Result<ModerationOp, ToolError> {
    let (_, kind) = MODERATION_TOOLS
        .iter()
        .find(|(t, _)| *t == tool)
        .ok_or_else(|| bad("unknown moderation tool"))?;
    reject_destination_args(args)?;
    let obj = args
        .as_object()
        .ok_or_else(|| bad("args must be an object"))?;
    let allowed: &[&str] = match *kind {
        "mod.ban" => &["user", "reason"],
        "mod.timeout" => &["user", "duration", "reason"],
        "mod.unban" => &["user"],
        _ => &["message_id"],
    };
    if let Some(k) = obj.keys().find(|k| !allowed.contains(&k.as_str())) {
        return Err(ToolError::InvalidArgs(format!("unknown argument {k}")));
    }
    let mut payload = serde_json::Map::new();
    let mut params = serde_json::Map::new();
    if *kind != "chat.delete" {
        let user = obj
            .get("user")
            .and_then(Value::as_str)
            .filter(|u| is_uuid(u))
            .ok_or_else(|| bad("user must be a canonical UUID"))?;
        payload.insert("user".to_string(), json!(user));
    }
    if *kind == "mod.timeout" {
        let d = obj
            .get("duration")
            .and_then(Value::as_u64)
            .filter(|d| {
                (crate::moderation::TIMEOUT_MIN_SECS..=crate::moderation::TIMEOUT_MAX_SECS)
                    .contains(d)
            })
            .ok_or_else(|| bad("duration must be an integer 1..=1209600 seconds"))?;
        params.insert("duration".to_string(), json!(d));
    }
    if let Some(r) = obj.get("reason") {
        let r = r.as_str().ok_or_else(|| bad("reason must be a string"))?;
        if r.chars().count() > crate::moderation::MAX_REASON_CHARS
            || crate::sender::reject_unsafe("reason", r).is_err()
        {
            return Err(bad("reason is too long or contains unsafe characters"));
        }
        params.insert("reason".to_string(), json!(r));
    }
    if *kind == "chat.delete" {
        let id = crate::sender::id_arg(args, "message_id", false)
            .map_err(|_| bad("message_id has an invalid shape"))?;
        payload.insert("message_id".to_string(), json!(id));
    }
    if !params.is_empty() {
        // The producer envelope has one free-form field for parameters: `text` (JSON object text).
        payload.insert("text".to_string(), json!(Value::Object(params).to_string()));
    }
    Ok(ModerationOp {
        kind,
        payload: Value::Object(payload),
    })
}

fn paging(args: &Value) -> Result<String, ToolError> {
    let mut q = String::new();
    if let Some(first) = args.get("first") {
        let n = first
            .as_u64()
            .filter(|n| (1..=1000).contains(n))
            .ok_or_else(|| ToolError::InvalidArgs("first must be 1..=1000".to_string()))?;
        q.push_str(&format!("&first={n}"));
    }
    if let Some(after) = args.get("after") {
        let a = after
            .as_str()
            .filter(|a| {
                !a.is_empty()
                    && a.len() <= 512
                    && a.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"-_=.".contains(&b))
            })
            .ok_or_else(|| ToolError::InvalidArgs("after has an invalid shape".to_string()))?;
        q.push_str(&format!("&after={a}"));
    }
    Ok(q)
}

fn fetch(host: &dyn HelixHost, url: &str) -> Result<Value, ToolError> {
    let (status, body) = host.get(url)?;
    match status {
        200 => serde_json::from_slice(&body).map_err(|_| ToolError::TargetUnavailable),
        401 | 403 => Err(ToolError::Forbidden),
        400 | 404 => Err(ToolError::InvalidArgs(format!(
            "twitch rejected request ({status})"
        ))),
        429 => Err(ToolError::PoolExhausted),
        _ => Err(ToolError::TargetUnavailable),
    }
}

fn rows(
    resp: &Value,
    ident: &dyn IdentityHost,
    extra: &[&str],
) -> Result<(Vec<Value>, Option<String>), ToolError> {
    let data = resp
        .get("data")
        .and_then(Value::as_array)
        .ok_or(ToolError::TargetUnavailable)?;
    let mut out = Vec::with_capacity(data.len());
    for row in data {
        let id = row
            .get("user_id")
            .and_then(Value::as_str)
            .ok_or(ToolError::TargetUnavailable)?;
        let mut o = serde_json::Map::new();
        o.insert("user".to_string(), Value::String(ident.uuid_for(id)?));
        for k in extra {
            if let Some(v) = row.get(*k) {
                o.insert((*k).to_string(), v.clone());
            }
        }
        out.push(Value::Object(o));
    }
    let cursor = resp
        .get("pagination")
        .and_then(|p| p.get("cursor"))
        .and_then(Value::as_str)
        .map(str::to_string);
    Ok((out, cursor))
}

fn reply(key: &str, resp: &Value, rows: Vec<Value>, cursor: Option<String>) -> Value {
    json!({ key: rows, "total": resp.get("total"), "cursor": cursor })
}

/// Builds the tool table over the given hosts.
pub fn build_registry(helix: Arc<dyn HelixHost>, ident: Arc<dyn IdentityHost>) -> ToolRegistry {
    let mut r = ToolRegistry::new();
    // Host-mediated: reaching the guest is a wiring error, refused loudly (never a no-op Ok).
    for (name, _) in MODERATION_TOOLS {
        r.register(name, |_ctx, _args| {
            Err(ToolError::InvalidArgs(
                "moderation tools are executed by the host, not the guest".to_string(),
            ))
        });
    }
    {
        let (h, i) = (helix.clone(), ident.clone());
        r.register("get_chatters", move |_ctx, args| {
            reject_destination_args(&args)?;
            let url = format!(
                "{HELIX}/chat/chatters?broadcaster_id={SLOT_BROADCASTER}&moderator_id={SLOT_SENDER}{}",
                paging(&args)?
            );
            let resp = fetch(h.as_ref(), &url)?;
            let (rows, c) = rows(&resp, i.as_ref(), &[])?;
            Ok(reply("chatters", &resp, rows, c))
        });
    }
    {
        let (h, i) = (helix.clone(), ident.clone());
        r.register("get_followers", move |_ctx, args| {
            reject_destination_args(&args)?;
            let url = format!(
                "{HELIX}/channels/followers?broadcaster_id={SLOT_BROADCASTER}{}",
                paging(&args)?
            );
            let resp = fetch(h.as_ref(), &url)?;
            let (rows, c) = rows(&resp, i.as_ref(), &["followed_at"])?;
            Ok(reply("followers", &resp, rows, c))
        });
    }
    {
        let (h, i) = (helix, ident);
        r.register("get_subscriptions", move |_ctx, args| {
            reject_destination_args(&args)?;
            let url = format!(
                "{HELIX}/subscriptions?broadcaster_id={SLOT_BROADCASTER}{}",
                paging(&args)?
            );
            let resp = fetch(h.as_ref(), &url)?;
            let (rows, c) = rows(&resp, i.as_ref(), &["tier", "is_gift"])?;
            Ok(reply("subscriptions", &resp, rows, c))
        });
    }
    r
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;

    struct Fake {
        status: u16,
        body: String,
        urls: Mutex<Vec<String>>,
    }
    impl HelixHost for Fake {
        fn get(&self, url: &str) -> Result<(u16, Vec<u8>), ToolError> {
            self.urls.lock().unwrap().push(url.to_string());
            Ok((self.status, self.body.clone().into_bytes()))
        }
    }
    struct Ident;
    impl IdentityHost for Ident {
        fn uuid_for(&self, id: &str) -> Result<String, ToolError> {
            Ok(format!("uuid-{id}"))
        }
    }
    fn reg(status: u16, body: &str) -> (ToolRegistry, Arc<Fake>) {
        let f = Arc::new(Fake {
            status,
            body: body.to_string(),
            urls: Mutex::new(vec![]),
        });
        (build_registry(f.clone(), Arc::new(Ident)), f)
    }
    fn call(r: &ToolRegistry, n: &str, a: Value) -> Result<Value, ToolError> {
        r.call(n, a.to_string().as_bytes())
            .map(|b| serde_json::from_slice(&b).unwrap())
    }

    #[test]
    fn chatters_map_to_uuids_only() {
        let (r, f) = reg(
            200,
            r#"{"data":[{"user_id":"7","user_login":"bob","user_name":"Bob"}],"total":1,"pagination":{"cursor":"c1"}}"#,
        );
        let v = call(&r, "get_chatters", json!({"first":5,"after":"ab-1"})).unwrap();
        assert_eq!(v["chatters"][0]["user"], "uuid-7");
        assert!(!v.to_string().contains("bob"), "no handle may leak");
        assert_eq!(v["cursor"], "c1");
        let u = f.urls.lock().unwrap()[0].clone();
        assert!(u.ends_with("broadcaster_id={{TWITCH_BROADCASTER_ID}}&moderator_id={{TWITCH_SENDER_ID}}&first=5&after=ab-1"), "{u}");
    }

    #[test]
    fn followers_and_subs_carry_extras() {
        let (r, _) = reg(
            200,
            r#"{"data":[{"user_id":"7","followed_at":"t","tier":"1000","is_gift":false}],"total":9}"#,
        );
        let v = call(&r, "get_followers", json!({})).unwrap();
        assert_eq!(v["followers"][0]["followed_at"], "t");
        assert_eq!(v["total"], 9);
        assert_eq!(v["cursor"], Value::Null);
        let v = call(&r, "get_subscriptions", json!({})).unwrap();
        assert_eq!(v["subscriptions"][0]["tier"], "1000");
    }

    /// Pinning: a caller can never name another channel/moderator; the host
    /// substitutes the pinned ids into the slots.
    #[test]
    fn caller_supplied_destination_ids_are_rejected_and_urls_use_slots() {
        for tool in ["get_chatters", "get_followers", "get_subscriptions"] {
            for key in [
                "broadcaster_id",
                "moderator_id",
                "channel",
                "room_id",
                "sender_id",
            ] {
                let (r, f) = reg(200, "{}");
                let e = call(&r, tool, json!({ key: "999" })).unwrap_err();
                assert!(matches!(e, ToolError::InvalidArgs(_)), "{tool}/{key}");
                assert!(f.urls.lock().unwrap().is_empty());
            }
            let (r, f) = reg(200, r#"{"data":[]}"#);
            call(&r, tool, json!({})).unwrap();
            let u = f.urls.lock().unwrap()[0].clone();
            assert!(
                u.contains("broadcaster_id={{TWITCH_BROADCASTER_ID}}"),
                "{u}"
            );
            assert!(!u.contains("999"));
        }
    }

    #[test]
    fn argument_validation() {
        let (r, f) = reg(200, "{}");
        for bad in [
            json!({"first":0}),
            json!({"first":1001}),
            json!({"after":"a&b"}),
            json!({"after":"{{TWITCH_TOKEN}}"}),
        ] {
            assert!(matches!(
                call(&r, "get_chatters", bad),
                Err(ToolError::InvalidArgs(_))
            ));
        }
        assert!(
            f.urls.lock().unwrap().is_empty(),
            "no request on invalid args"
        );
        assert!(matches!(
            call(&r, "nope", json!({})),
            Err(ToolError::InvalidArgs(_))
        ));
    }

    #[test]
    fn upstream_status_mapping() {
        let a = json!({});
        for (status, ok) in [
            (401u16, "forbidden"),
            (403, "forbidden"),
            (400, "invalid"),
            (404, "invalid"),
            (429, "pool"),
            (500, "unavail"),
        ] {
            let (r, _) = reg(status, "{}");
            let e = call(&r, "get_followers", a.clone()).unwrap_err();
            let got = match e {
                ToolError::Forbidden => "forbidden",
                ToolError::InvalidArgs(_) => "invalid",
                ToolError::PoolExhausted => "pool",
                ToolError::TargetUnavailable => "unavail",
                _ => "other",
            };
            assert_eq!(got, ok, "{status}");
        }
        let (r, _) = reg(200, "not json");
        assert!(matches!(
            call(&r, "get_followers", a.clone()),
            Err(ToolError::TargetUnavailable)
        ));
        let (r, _) = reg(200, r#"{"nodata":1}"#);
        assert!(matches!(
            call(&r, "get_followers", a.clone()),
            Err(ToolError::TargetUnavailable)
        ));
        let (r, _) = reg(200, r#"{"data":[{"x":1}]}"#);
        assert!(matches!(
            call(&r, "get_followers", a),
            Err(ToolError::TargetUnavailable)
        ));
    }

    const UID: &str = "123e4567-e89b-12d3-a456-426614174000";

    #[test]
    fn moderation_ops_validate_and_map_to_kinds() {
        let op = moderation_op(
            "twitch.moderation.ban",
            &json!({"user":UID,"reason":"spam"}),
        )
        .unwrap();
        assert_eq!(
            (op.kind, op.payload),
            (
                "mod.ban",
                json!({"user":UID,"text":json!({"reason":"spam"}).to_string()})
            )
        );
        let op = moderation_op(
            "twitch.moderation.timeout",
            &json!({"user":UID,"duration":60}),
        )
        .unwrap();
        assert_eq!(
            (op.kind, op.payload),
            (
                "mod.timeout",
                json!({"user":UID,"text":json!({"duration":60}).to_string()})
            )
        );
        let op = moderation_op("twitch.moderation.unban", &json!({"user":UID})).unwrap();
        assert_eq!(op.kind, "mod.unban");
        let op = moderation_op(
            "twitch.moderation.delete_message",
            &json!({"message_id":"abc-1"}),
        )
        .unwrap();
        assert_eq!(
            (op.kind, op.payload),
            ("chat.delete", json!({"message_id":"abc-1"}))
        );
    }

    #[test]
    fn moderation_rejects_bad_args_destinations_and_unknown_tools() {
        let cases = [
            ("twitch.moderation.ban", json!({})),
            ("twitch.moderation.ban", json!({"user":"1234"})),
            ("twitch.moderation.ban", json!({"user":UID,"extra":1})),
            (
                "twitch.moderation.ban",
                json!({"user":UID,"reason":"{{TWITCH_TOKEN}}"}),
            ),
            ("twitch.moderation.ban", json!({"user":UID,"duration":60})),
            ("twitch.moderation.timeout", json!({"user":UID})),
            (
                "twitch.moderation.timeout",
                json!({"user":UID,"duration":0}),
            ),
            (
                "twitch.moderation.timeout",
                json!({"user":UID,"duration":1209601}),
            ),
            (
                "twitch.moderation.unban",
                json!({"user":UID,"broadcaster_id":"9"}),
            ),
            (
                "twitch.moderation.ban",
                json!({"user":UID,"moderator_id":"9"}),
            ),
            (
                "twitch.moderation.delete_message",
                json!({"message_id":"a&b"}),
            ),
            (
                "twitch.moderation.delete_message",
                json!({"message_id":"x","channel":"c"}),
            ),
            ("twitch.moderation.clear_chat", json!({})),
            ("twitch.moderation.ban", json!([])),
        ];
        for (tool, args) in cases {
            assert!(
                matches!(moderation_op(tool, &args), Err(ToolError::InvalidArgs(_))),
                "{tool} {args}"
            );
        }
    }

    #[test]
    fn moderation_tools_are_host_executed_never_a_guest_ok() {
        let (r, f) = reg(200, "{}");
        for (name, _) in MODERATION_TOOLS {
            let e = call(&r, name, json!({"user":UID,"duration":60,"message_id":"m"})).unwrap_err();
            assert!(
                matches!(e, ToolError::InvalidArgs(m) if m.contains("host")),
                "{name}"
            );
        }
        assert!(f.urls.lock().unwrap().is_empty(), "guest made no request");
    }
}
