//! Read tools exposed through `tool-provider`: `kick.live.channels`.
//!
//! `kick.live.channels` implements the shared `*.live.channels` contract of the bundle catalog
//! (docs/superpowers/specs/2026-11-07-bundle-catalog-target.md S8):
//!
//! ```text
//! in : { scope: "community"|"linked", cursor?: string, limit?: 1-100 }
//! out: { channels: [ { channel_ref, display_name, is_live, title?, category?, viewers?,
//!        started_at?, thumbnail_ref? } ], cursor?, as_of }
//! err: platform_unavailable | rate_limited | not_connected   (loud; never empty-success)
//! ```
//!
//! The request is `GET https://api.kick.com/public/v1/channels` with NO query: per
//! <https://docs.kick.com/apis/channels> that returns the channel of the authenticated user,
//! i.e. exactly the community's own connection -- the destination is the host-resolved
//! credential, never a caller-supplied id. Only an explicit allowlist of response fields is
//! copied out: the same response carries the channel's stream KEY and ingest URL, which must
//! never leave the bundle. `channel_ref` is the broadcaster's `identity.lookup` UUID (opaque,
//! no numeric Kick id).

use std::sync::Arc;

use serde_json::{Value, json};
use waddle_sdk::tool::{ToolError, ToolRegistry};

/// The exported tool name.
pub const LIVE_CHANNELS: &str = "kick.live.channels";
const CHANNELS_URL: &str = "https://api.kick.com/public/v1/channels";
/// Header secret-ref the host injects (header -> secret ref). No client id: Kick's public API
/// authenticates with the bearer token alone.
pub const SECRET_REFS: &[(&str, &str)] = &[("Authorization", "kick_bearer")];

/// Guarded outbound GET (wasm glue: the `http` import) plus the host clock.
pub trait KickHost: Send + Sync {
    /// Sends a GET to `url` with [`SECRET_REFS`]; returns `(status, body)`.
    fn get(&self, url: &str) -> Result<(u16, Vec<u8>), ToolError>;
    /// Current time, RFC 3339 UTC (`clock.now-rfc3339`).
    fn now_rfc3339(&self) -> String;
}

/// Platform-user-id -> canonical UUID (wasm glue: `identity.lookup`).
pub trait IdentityHost: Send + Sync {
    /// Resolves one Kick numeric user id.
    fn uuid_for(&self, kick_user_id: &str) -> Result<String, ToolError>;
}

fn invalid(m: &str) -> ToolError {
    ToolError::InvalidArgs(m.to_string())
}

/// Validates the input against the contract; any other key (notably a destination id) is
/// refused outright.
fn check_args(args: &Value) -> Result<(), ToolError> {
    let obj = args
        .as_object()
        .ok_or_else(|| invalid("args must be an object"))?;
    for (k, v) in obj {
        match k.as_str() {
            "scope" => match v.as_str() {
                Some("community") => {}
                Some("linked") => {
                    return Err(invalid(
                        "scope `linked` is not supported by kick: the host exposes one pinned channel",
                    ));
                }
                _ => return Err(invalid("scope must be `community` or `linked`")),
            },
            "cursor" => {
                let ok = v.as_str().is_some_and(|c| {
                    !c.is_empty()
                        && c.len() <= 512
                        && c.bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"-_=.".contains(&b))
                });
                if !ok {
                    return Err(invalid("cursor has an invalid shape"));
                }
            }
            "limit" => {
                if !v.as_u64().is_some_and(|n| (1..=100).contains(&n)) {
                    return Err(invalid("limit must be 1..=100"));
                }
            }
            other => {
                return Err(invalid(&format!(
                    "{other} is not an accepted argument (channels are host-resolved)"
                )));
            }
        }
    }
    Ok(())
}

fn fetch(host: &dyn KickHost) -> Result<Value, ToolError> {
    let (status, body) = host.get(CHANNELS_URL)?;
    match status {
        200 => serde_json::from_slice(&body).map_err(|_| ToolError::TargetUnavailable),
        // No usable credential for the community: the contract's `not_connected`.
        401 | 403 => Err(ToolError::Forbidden),
        429 => Err(ToolError::PoolExhausted),
        _ => Err(ToolError::TargetUnavailable),
    }
}

/// Maps the Kick channel record to one contract row. Allowlist copy only.
fn channel_row(row: &Value, ident: &dyn IdentityHost) -> Result<Value, ToolError> {
    let id = row
        .get("broadcaster_user_id")
        .and_then(crate::common::id_string)
        .ok_or(ToolError::TargetUnavailable)?;
    let slug = row
        .get("slug")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or(ToolError::TargetUnavailable)?;
    let stream = row.get("stream").filter(|s| s.is_object());
    let is_live = stream
        .and_then(|s| s.get("is_live"))
        .and_then(Value::as_bool)
        .ok_or(ToolError::TargetUnavailable)?;
    let mut out = serde_json::Map::new();
    out.insert("channel_ref".into(), Value::String(ident.uuid_for(&id)?));
    out.insert("display_name".into(), Value::String(slug.to_string()));
    out.insert("is_live".into(), Value::Bool(is_live));
    if let Some(t) = row
        .get("stream_title")
        .and_then(Value::as_str)
        .filter(|t| !t.is_empty())
    {
        out.insert("title".into(), Value::String(t.to_string()));
    }
    if let Some(c) = row
        .get("category")
        .and_then(|c| c.get("name"))
        .and_then(Value::as_str)
        .filter(|c| !c.is_empty())
    {
        out.insert("category".into(), Value::String(c.to_string()));
    }
    if is_live {
        if let Some(v) = stream
            .and_then(|s| s.get("viewer_count"))
            .and_then(Value::as_u64)
        {
            out.insert("viewers".into(), json!(v));
        }
        if let Some(t) = stream
            .and_then(|s| s.get("start_time"))
            .and_then(Value::as_str)
            .filter(|t| !t.is_empty())
        {
            out.insert("started_at".into(), Value::String(t.to_string()));
        }
    }
    Ok(Value::Object(out))
}

/// Builds the tool table over the given hosts.
pub fn build_registry(host: Arc<dyn KickHost>, ident: Arc<dyn IdentityHost>) -> ToolRegistry {
    let mut r = ToolRegistry::new();
    r.register(LIVE_CHANNELS, move |_ctx, args| {
        check_args(&args)?;
        let resp = fetch(host.as_ref())?;
        let data = resp
            .get("data")
            .and_then(Value::as_array)
            .ok_or(ToolError::TargetUnavailable)?;
        // A successful reply with no channel means the token has none: loud, never an empty list.
        if data.is_empty() {
            return Err(ToolError::TargetUnavailable);
        }
        let channels = data
            .iter()
            .map(|row| channel_row(row, ident.as_ref()))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(json!({ "channels": channels, "as_of": host.now_rfc3339() }))
    });
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
    impl KickHost for Fake {
        fn get(&self, url: &str) -> Result<(u16, Vec<u8>), ToolError> {
            self.urls.lock().unwrap().push(url.to_string());
            Ok((self.status, self.body.clone().into_bytes()))
        }
        fn now_rfc3339(&self) -> String {
            "2026-11-07T00:00:00.000Z".to_string()
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
    fn call(r: &ToolRegistry, a: Value) -> Result<Value, ToolError> {
        r.call(LIVE_CHANNELS, a.to_string().as_bytes())
            .map(|b| serde_json::from_slice(&b).unwrap())
    }

    const LIVE: &str = r#"{"data":[{"active_subscribers_count":150,"broadcaster_user_id":123,
        "category":{"id":101,"name":"Old School Runescape","thumbnail":"https://x/t.jpg"},
        "channel_description":"d","slug":"john-doe",
        "stream":{"custom_tags":["a"],"is_live":true,"is_mature":true,
          "key":"super-secret-stream-key","language":"en",
          "start_time":"2026-11-07T10:00:00Z","thumbnail":"https://x/s.jpg",
          "url":"rtmps://stream.kick.com/1234567890","viewer_count":67},
        "stream_title":"My first stream"}],"message":"ok"}"#;

    #[test]
    fn live_channel_maps_to_the_contract_row_and_never_leaks_the_stream_key() {
        let (r, f) = reg(200, LIVE);
        let v = call(&r, json!({"scope": "community", "limit": 5})).unwrap();
        assert_eq!(
            v,
            json!({"channels": [{"channel_ref": "uuid-123", "display_name": "john-doe",
                "is_live": true, "title": "My first stream", "category": "Old School Runescape",
                "viewers": 67, "started_at": "2026-11-07T10:00:00Z"}],
                "as_of": "2026-11-07T00:00:00.000Z"})
        );
        let text = v.to_string();
        for leak in [
            "super-secret-stream-key",
            "rtmps://",
            "123456",
            ":123",
            "\"123\"",
        ] {
            assert!(!text.contains(leak), "{leak}");
        }
        // Destination is the credential: the URL carries no id and no slot.
        assert_eq!(
            f.urls.lock().unwrap().as_slice(),
            ["https://api.kick.com/public/v1/channels"]
        );
    }

    #[test]
    fn offline_channel_omits_live_only_fields() {
        let body = r#"{"data":[{"broadcaster_user_id":9,"slug":"s",
            "stream":{"is_live":false,"start_time":"0001-01-01T00:00:00Z","viewer_count":0},
            "stream_title":"","category":{"name":""}}]}"#;
        let (r, _) = reg(200, body);
        let v = call(&r, json!({})).unwrap();
        let c = &v["channels"][0];
        assert_eq!(c["is_live"], false);
        for k in ["viewers", "started_at", "title", "category"] {
            assert!(c.get(k).is_none(), "{k}");
        }
    }

    #[test]
    fn argument_validation_refuses_destinations_and_bad_shapes() {
        let (r, f) = reg(200, LIVE);
        for bad in [
            json!({"broadcaster_user_id": 1}),
            json!({"slug": "x"}),
            json!({"scope": "linked"}),
            json!({"scope": "everyone"}),
            json!({"limit": 0}),
            json!({"limit": 101}),
            json!({"cursor": "a&b"}),
            json!({"cursor": ""}),
            json!([1]),
        ] {
            assert!(
                matches!(call(&r, bad.clone()), Err(ToolError::InvalidArgs(_))),
                "{bad}"
            );
        }
        assert!(
            f.urls.lock().unwrap().is_empty(),
            "no request on invalid args"
        );
        assert!(call(&r, json!({"cursor": "ab-1_=."})).is_ok());
    }

    #[test]
    fn upstream_failures_are_loud_never_empty_success() {
        for (status, want) in [
            (401u16, "forbidden"),
            (403, "forbidden"),
            (429, "pool"),
            (500, "unavail"),
            (400, "unavail"),
        ] {
            let (r, _) = reg(status, "{}");
            let got = match call(&r, json!({})).unwrap_err() {
                ToolError::Forbidden => "forbidden",
                ToolError::PoolExhausted => "pool",
                ToolError::TargetUnavailable => "unavail",
                _ => "other",
            };
            assert_eq!(got, want, "{status}");
        }
        for body in [
            "not json",
            r#"{"nodata":1}"#,
            r#"{"data":[]}"#,
            r#"{"data":[{"slug":"s"}]}"#,
            r#"{"data":[{"broadcaster_user_id":1,"slug":"s"}]}"#,
        ] {
            let (r, _) = reg(200, body);
            assert!(
                matches!(call(&r, json!({})), Err(ToolError::TargetUnavailable)),
                "{body}"
            );
        }
    }

    #[test]
    fn identity_failure_propagates_and_no_raw_id_fallback_exists() {
        struct Deny;
        impl IdentityHost for Deny {
            fn uuid_for(&self, _: &str) -> Result<String, ToolError> {
                Err(ToolError::NotGranted)
            }
        }
        let f = Arc::new(Fake {
            status: 200,
            body: LIVE.to_string(),
            urls: Mutex::new(vec![]),
        });
        let r = build_registry(f, Arc::new(Deny));
        assert!(matches!(call(&r, json!({})), Err(ToolError::NotGranted)));
    }

    /// Contract fixture: every field of every returned row is declared by the catalog contract.
    #[test]
    fn output_conforms_to_the_catalog_live_channels_contract() {
        let contract: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/live_channels_contract.json"
        ))
        .unwrap();
        let allowed: Vec<&str> = contract["row_fields"]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        let required: Vec<&str> = contract["row_required"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        let (r, _) = reg(200, LIVE);
        let v = call(&r, json!({})).unwrap();
        for k in v.as_object().unwrap().keys() {
            assert!(
                contract["top_level"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|t| t == k),
                "{k}"
            );
        }
        for row in v["channels"].as_array().unwrap() {
            for k in row.as_object().unwrap().keys() {
                assert!(allowed.contains(&k.as_str()), "undeclared field {k}");
            }
            for k in &required {
                assert!(row.get(*k).is_some(), "missing {k}");
            }
        }
    }
}
