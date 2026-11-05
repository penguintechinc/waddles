//! Read tools exposed through `tool-provider`: `get_chatters`, `get_followers`,
//! `get_subscriptions`. Requests go out through the guarded `http` import with
//! header secret-refs (the bundle never sees a token); every returned user is
//! mapped to a canonical UUID via `identity.lookup`, so no handle or numeric
//! Twitch id leaves the overlay.

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
    json!({ key: rows, "total": resp.get("total").cloned().unwrap_or(Value::Null), "cursor": cursor })
}

/// Builds the tool table over the given hosts.
pub fn build_registry(helix: Arc<dyn HelixHost>, ident: Arc<dyn IdentityHost>) -> ToolRegistry {
    let mut r = ToolRegistry::new();
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
}
