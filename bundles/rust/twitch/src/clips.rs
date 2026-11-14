//! Twitch clip tools: `twitch.clip.create` (host-executed), `twitch.clip.get` and
//! `twitch.clip.list` (guarded reads). Schema: the twitch-clips contract
//! (`bundles/rust/twitch-clips/contract/twitch-clip-tools.json`).
//!
//! `create` is `POST /helix/clips?broadcaster_id=` (scope `clips:edit`); the broadcaster is the
//! host slot, no tool takes a channel. Parity with the legacy `create_clip`: the broadcaster is
//! the only query parameter. `title` / `duration_s` are validated against the contract but the
//! build REFUSES them (`Unsupported`): Helix carries both in the query string, which the host
//! verifier's literal-query grammar cannot express for free text (see README "Needs from S0").

use serde_json::{Value, json};
use waddle_sdk::tool::ToolError;

use crate::common::FrameErr;
use crate::helix::{
    HostOp, bad, clip_chars, coded, get_json, is_clip_id, is_timestamp, object, status_error,
    text_arg,
};
use crate::sender::{HELIX, SLOT_BROADCASTER, Tpl, auth_headers, base_refs};
use crate::tools::HelixHost;

/// The outbound kind `twitch.clip.create` maps to.
pub const CREATE_KIND: &str = "clip.create";

/// Validates `twitch.clip.create` args (`{title?, duration_s?}`) and yields the host op.
///
/// # Errors
/// `InvalidArgs` for an unknown key or a value outside the contract schema.
pub fn create_op(args: &Value) -> Result<HostOp, ToolError> {
    let obj = object(args, &["title", "duration_s"])?;
    let mut params = serde_json::Map::new();
    if let Some(t) = obj.get("title") {
        params.insert("title".to_string(), json!(text_arg(t, "title", 100)?));
    }
    if let Some(d) = obj.get("duration_s") {
        let d = d
            .as_u64()
            .filter(|d| (5..=60).contains(d))
            .ok_or_else(|| bad("duration_s must be an integer 5..=60"))?;
        params.insert("duration_s".to_string(), json!(d));
    }
    let payload = if params.is_empty() {
        json!({})
    } else {
        json!({ "text": Value::Object(params).to_string() })
    };
    Ok(HostOp {
        kind: CREATE_KIND,
        payload,
    })
}

/// Helix template for `clip.create`.
pub fn build_create(payload: &Value) -> Result<Tpl, FrameErr> {
    if payload.get("text").is_some() {
        return Err(FrameErr::Unsupported(
            "clip title/duration_s need query values the host verifier cannot carry yet".to_string(),
        ));
    }
    Ok(Tpl {
        method: "POST".to_string(),
        url: format!("{HELIX}/clips?broadcaster_id={SLOT_BROADCASTER}"),
        headers: auth_headers(),
        body: None,
        secret_refs: base_refs(),
    })
}

/// One Helix clip row mapped to the contract's `clip` object; `None` when the row lacks the
/// required fields or names a non-Twitch URL (loud at the caller, never defaulted).
fn clip_row(row: &Value) -> Option<Value> {
    let id = row.get("id")?.as_str().filter(|i| is_clip_id(i))?;
    let url = row.get("url")?.as_str().filter(|u| {
        ["https://clips.twitch.tv/", "https://www.twitch.tv/", "https://twitch.tv/"]
            .iter()
            .any(|p| u.starts_with(p))
    })?;
    let mut o = serde_json::Map::new();
    o.insert("clip_id".to_string(), json!(id));
    o.insert("url".to_string(), json!(url));
    if let Some(t) = row.get("title").and_then(Value::as_str) {
        o.insert("title".to_string(), json!(clip_chars(t, 100)));
    }
    if let Some(c) = row.get("created_at").and_then(Value::as_str) {
        o.insert("created_at".to_string(), json!(c));
    }
    Some(Value::Object(o))
}

fn rows(body: &Value) -> Result<Vec<Value>, ToolError> {
    body.get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| coded("platform_unavailable", "twitch reply has no data"))?
        .iter()
        .map(|r| clip_row(r).ok_or_else(|| coded("platform_unavailable", "malformed clip row")))
        .collect()
}

/// `twitch.clip.get`: `GET /clips?id=`; empty Helix data means the clip is not ready yet.
pub fn get(host: &dyn HelixHost, args: &Value) -> Result<Value, ToolError> {
    let obj = object(args, &["clip_id"])?;
    let id = obj
        .get("clip_id")
        .and_then(Value::as_str)
        .filter(|i| is_clip_id(i))
        .ok_or_else(|| bad("clip_id must match ^[A-Za-z0-9_-]{1,100}$"))?;
    let (status, body) = get_json(host, &format!("{HELIX}/clips?id={id}"))?;
    if !(200..300).contains(&status) {
        return Err(status_error(status));
    }
    let mut clips = rows(&body)?;
    Ok(match clips.pop() {
        Some(clip) => json!({ "ready": true, "clip": clip }),
        None => json!({ "ready": false }),
    })
}

/// `twitch.clip.list`: `GET /clips?broadcaster_id=` (host slot) with optional paging/range.
pub fn list(host: &dyn HelixHost, args: &Value) -> Result<Value, ToolError> {
    let obj = object(args, &["limit", "cursor", "started_at", "ended_at"])?;
    let mut q = format!("broadcaster_id={SLOT_BROADCASTER}");
    if let Some(l) = obj.get("limit") {
        let l = l
            .as_u64()
            .filter(|l| (1..=100).contains(l))
            .ok_or_else(|| bad("limit must be an integer 1..=100"))?;
        q.push_str(&format!("&first={l}"));
    }
    if let Some(c) = obj.get("cursor") {
        let c = c
            .as_str()
            .filter(|c| {
                !c.is_empty()
                    && c.len() <= 512
                    && c.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"-_=.".contains(&b))
            })
            .ok_or_else(|| bad("cursor has an invalid shape"))?;
        q.push_str(&format!("&after={c}"));
    }
    for key in ["started_at", "ended_at"] {
        if let Some(v) = obj.get(key) {
            let v = v
                .as_str()
                .filter(|v| is_timestamp(v))
                .ok_or_else(|| ToolError::InvalidArgs(format!("{key} must be an RFC 3339 time")))?;
            q.push_str(&format!("&{key}={v}"));
        }
    }
    let (status, body) = get_json(host, &format!("{HELIX}/clips?{q}"))?;
    if !(200..300).contains(&status) {
        return Err(status_error(status));
    }
    let clips = rows(&body)?;
    let mut out = json!({ "clips": clips });
    if let Some(c) = body
        .get("pagination")
        .and_then(|p| p.get("cursor"))
        .and_then(Value::as_str)
    {
        out["cursor"] = json!(c);
    }
    Ok(out)
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
    fn fake(status: u16, body: Value) -> Fake {
        Fake {
            status,
            body: body.to_string(),
            urls: Mutex::new(vec![]),
        }
    }
    fn code(e: ToolError) -> String {
        match e {
            ToolError::InvalidArgs(m) => m,
            other => panic!("{other:?}"),
        }
    }
    const ROW: &str = r#"{"id":"AbC-1","url":"https://clips.twitch.tv/AbC-1","title":"nice","created_at":"2026-11-07T10:00:00Z","creator_name":"someone"}"#;

    #[test]
    fn create_template_is_the_legacy_shape_with_only_the_host_broadcaster() {
        let t = build_create(&json!({})).unwrap();
        assert_eq!(t.method, "POST");
        assert_eq!(
            t.url,
            "https://api.twitch.tv/helix/clips?broadcaster_id={{TWITCH_BROADCASTER_ID}}"
        );
        assert!(t.body.is_none());
        assert_eq!(create_op(&json!({})).unwrap().payload, json!({}));
    }

    #[test]
    fn create_validates_the_contract_then_the_build_refuses_unsupported_params_loudly() {
        let op = create_op(&json!({"title":"gg","duration_s":30})).unwrap();
        assert_eq!(op.kind, "clip.create");
        assert!(matches!(
            build_create(&op.payload),
            Err(FrameErr::Unsupported(_))
        ));
        for bad in [
            json!({"title":""}),
            json!({"title":"x".repeat(101)}),
            json!({"title":"a{{TWITCH_TOKEN}}"}),
            json!({"duration_s":4}),
            json!({"duration_s":61}),
            json!({"duration_s":"30"}),
            json!({"broadcaster_id":"1"}),
            json!({"channel":"x"}),
            json!({"extra":1}),
            json!([]),
        ] {
            assert!(create_op(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn get_maps_ready_and_not_ready() {
        let f = fake(200, json!({"data":[serde_json::from_str::<Value>(ROW).unwrap()]}));
        let out = get(&f, &json!({"clip_id":"AbC-1"})).unwrap();
        assert_eq!(
            out,
            json!({"ready":true,"clip":{"clip_id":"AbC-1","url":"https://clips.twitch.tv/AbC-1","title":"nice","created_at":"2026-11-07T10:00:00Z"}})
        );
        assert!(
            out["clip"].get("creator_name").is_none(),
            "no creator handle (PII)"
        );
        assert_eq!(f.urls.lock().unwrap()[0], "https://api.twitch.tv/helix/clips?id=AbC-1");
        let empty = fake(200, json!({"data":[]}));
        assert_eq!(
            get(&empty, &json!({"clip_id":"x"})).unwrap(),
            json!({"ready":false})
        );
    }

    #[test]
    fn get_rejects_bad_ids_and_maps_status_codes() {
        let f = fake(200, json!({"data":[]}));
        for bad in [json!({}), json!({"clip_id":"a b"}), json!({"clip_id":"a&b=1"}), json!({"clip_id":"x".repeat(101)})] {
            assert!(get(&f, &bad).is_err());
        }
        assert!(f.urls.lock().unwrap().is_empty(), "nothing sent for bad input");
        for (s, want) in [
            (401, "not_connected"),
            (403, "not_connected"),
            (429, "rate_limited"),
            (500, "platform_unavailable"),
            (404, "platform_unavailable"),
        ] {
            let e = code(get(&fake(s, json!({})), &json!({"clip_id":"x"})).unwrap_err());
            assert!(e.starts_with(want), "{s}: {e}");
        }
    }

    #[test]
    fn get_refuses_malformed_or_foreign_rows() {
        for row in [
            json!({"id":"x","url":"https://evil.example/x"}),
            json!({"id":"x"}),
            json!({"id":"bad id","url":"https://clips.twitch.tv/x"}),
        ] {
            let e = get(&fake(200, json!({"data":[row]})), &json!({"clip_id":"x"})).unwrap_err();
            assert!(code(e).starts_with("platform_unavailable"));
        }
        let e = get(&fake(200, json!({})), &json!({"clip_id":"x"})).unwrap_err();
        assert!(code(e).starts_with("platform_unavailable"));
    }

    #[test]
    fn list_builds_a_slot_pinned_query_and_pages() {
        let f = fake(
            200,
            json!({"data":[serde_json::from_str::<Value>(ROW).unwrap()],"pagination":{"cursor":"abc="}}),
        );
        let out = list(
            &f,
            &json!({"limit":5,"cursor":"zz==","started_at":"2026-11-01T00:00:00Z","ended_at":"2026-11-07T00:00:00Z"}),
        )
        .unwrap();
        assert_eq!(out["clips"].as_array().unwrap().len(), 1);
        assert_eq!(out["cursor"], "abc=");
        let url = f.urls.lock().unwrap()[0].clone();
        assert!(url.starts_with("https://api.twitch.tv/helix/clips?broadcaster_id={{TWITCH_BROADCASTER_ID}}&first=5&after=zz=="));
        assert!(url.ends_with("&ended_at=2026-11-07T00:00:00Z"));
        let none = list(&fake(200, json!({"data":[]})), &json!({})).unwrap();
        assert_eq!(none, json!({"clips":[]}));
    }

    #[test]
    fn list_rejects_bad_args_and_destinations() {
        let f = fake(200, json!({"data":[]}));
        for bad in [
            json!({"limit":0}),
            json!({"limit":101}),
            json!({"cursor":"a&b"}),
            json!({"started_at":"yesterday"}),
            json!({"ended_at":"2026-11-07T00:00:00Z&x=1"}),
            json!({"broadcaster_id":"5"}),
            json!({"x":1}),
        ] {
            assert!(list(&f, &bad).is_err(), "{bad}");
        }
        assert!(f.urls.lock().unwrap().is_empty());
    }
}
