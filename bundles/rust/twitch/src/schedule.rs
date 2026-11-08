//! Twitch stream-schedule tools: `twitch.schedule.{create,update,delete}` (host-executed,
//! `channel:manage:schedule`) and `twitch.schedule.list` (guarded read).
//!
//! Create/update/delete schemas are the calendar contract fixtures
//! (`bundles/rust/calendar/contracts/twitch.schedule.*.schema.json`) verbatim. Endpoints:
//! `POST|PATCH|DELETE /helix/schedule/segment` (`GET /helix/schedule` for list). The broadcaster
//! is the host slot; for update/delete the segment id rides in the producer parameters and the
//! HOST fills it into the `id` query (segment ids are base64 with `=` padding, which a literal
//! query cannot carry), so the template never contains it.

use serde_json::{Value, json};
use waddle_sdk::tool::ToolError;

use crate::common::FrameErr;
use crate::helix::{HostOp, bad, coded, get_json, is_timestamp, object, status_error, text_arg};
use crate::sender::{HELIX, SLOT_BROADCASTER, Tpl, auth_headers, base_refs, json_headers};
use crate::tools::HelixHost;

/// The outbound kinds this module owns.
pub const KINDS: &[&str] = &["schedule.create", "schedule.update", "schedule.delete"];

/// Helix segment duration bounds, minutes.
pub const DURATION_MIN: u64 = 30;
/// Helix segment duration upper bound, minutes.
pub const DURATION_MAX: u64 = 1380;

/// `[A-Za-z0-9_=+/-]{1,256}`: a Helix schedule segment id (base64 text).
pub fn is_segment_id(s: &str) -> bool {
    (1..=256).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'=' | b'+' | b'/'))
}

fn is_timezone(s: &str) -> bool {
    (1..=64).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'/' | b'+' | b'-'))
}

const FIELDS: [&str; 5] = ["start_time", "timezone", "duration", "title", "is_recurring"];

/// Validates the five segment fields shared by create and update; returns them as a map.
fn fields(obj: &serde_json::Map<String, Value>) -> Result<serde_json::Map<String, Value>, ToolError> {
    let mut out = serde_json::Map::new();
    let st = obj
        .get("start_time")
        .and_then(Value::as_str)
        .filter(|s| is_timestamp(s))
        .ok_or_else(|| bad("start_time must be an RFC 3339 time"))?;
    out.insert("start_time".to_string(), json!(st));
    let tz = obj
        .get("timezone")
        .and_then(Value::as_str)
        .filter(|s| is_timezone(s))
        .ok_or_else(|| bad("timezone must be an IANA timezone name"))?;
    out.insert("timezone".to_string(), json!(tz));
    let d = obj
        .get("duration")
        .and_then(Value::as_u64)
        .filter(|d| (DURATION_MIN..=DURATION_MAX).contains(d))
        .ok_or_else(|| bad("duration must be an integer 30..=1380 minutes"))?;
    out.insert("duration".to_string(), json!(d));
    out.insert(
        "title".to_string(),
        json!(text_arg(obj.get("title").unwrap_or(&Value::Null), "title", 140)?),
    );
    let r = obj
        .get("is_recurring")
        .and_then(Value::as_bool)
        .ok_or_else(|| bad("is_recurring must be a boolean"))?;
    out.insert("is_recurring".to_string(), json!(r));
    Ok(out)
}

fn segment_id(obj: &serde_json::Map<String, Value>) -> Result<&str, ToolError> {
    obj.get("segment_id")
        .and_then(Value::as_str)
        .filter(|s| is_segment_id(s))
        .ok_or_else(|| bad("segment_id has an invalid shape"))
}

/// Validates a `twitch.schedule.{create,update,delete}` call against its contract schema (all
/// fields required, `additionalProperties: false`) and yields the host op.
///
/// # Errors
/// `InvalidArgs` for an unknown tool/key or any value outside the schema.
pub fn host_op(tool: &str, args: &Value) -> Result<HostOp, ToolError> {
    match tool {
        "twitch.schedule.create" => {
            let obj = object(args, &FIELDS)?;
            Ok(HostOp {
                kind: "schedule.create",
                payload: json!({ "text": Value::Object(fields(obj)?).to_string() }),
            })
        }
        "twitch.schedule.update" => {
            let mut allowed = FIELDS.to_vec();
            allowed.push("segment_id");
            let obj = object(args, &allowed)?;
            let mut f = fields(obj)?;
            f.insert("segment_id".to_string(), json!(segment_id(obj)?));
            Ok(HostOp {
                kind: "schedule.update",
                payload: json!({ "text": Value::Object(f).to_string() }),
            })
        }
        "twitch.schedule.delete" => {
            let obj = object(args, &["segment_id"])?;
            let f = json!({ "segment_id": segment_id(obj)? });
            Ok(HostOp {
                kind: "schedule.delete",
                payload: json!({ "text": f.to_string() }),
            })
        }
        _ => Err(bad("unknown schedule tool")),
    }
}

/// Helix template for `schedule.create|update|delete`. For update/delete the host has already
/// removed the segment id from the payload (it fills the `id` query itself); a payload that
/// still carries one is refused so an id can never be baked into a template.
pub fn build(kind: &str, payload: &Value) -> Result<Tpl, FrameErr> {
    let mal = |m: &str| FrameErr::Malformed(m.to_string());
    let broadcaster = format!("broadcaster_id={SLOT_BROADCASTER}");
    let base = format!("{HELIX}/schedule/segment");
    let parsed = || -> Result<serde_json::Map<String, Value>, FrameErr> {
        let raw = payload
            .get("text")
            .and_then(Value::as_str)
            .ok_or_else(|| mal("payload needs the segment fields in `text`"))?;
        match serde_json::from_str::<Value>(raw) {
            Ok(Value::Object(m)) => Ok(m),
            _ => Err(mal("`text` must be a JSON object")),
        }
    };
    match kind {
        "schedule.delete" => {
            if payload.get("text").is_some() {
                return Err(mal("schedule.delete takes no parameters"));
            }
            Ok(Tpl {
                method: "DELETE".to_string(),
                url: format!("{base}?{broadcaster}&id={{{{TWITCH_SEGMENT_ID}}}}"),
                headers: auth_headers(),
                body: None,
                secret_refs: base_refs(),
            })
        }
        "schedule.create" | "schedule.update" => {
            let m = parsed()?;
            if m.keys().any(|k| !FIELDS.contains(&k.as_str())) {
                return Err(mal("unexpected schedule field"));
            }
            let f = fields(&m).map_err(|_| mal("schedule fields are invalid"))?;
            let mut body = f.clone();
            // Helix takes the duration as a string of minutes.
            body.insert(
                "duration".to_string(),
                json!(f["duration"].as_u64().unwrap_or(0).to_string()),
            );
            let (method, url) = if kind == "schedule.create" {
                ("POST", format!("{base}?{broadcaster}"))
            } else {
                (
                    "PATCH",
                    format!("{base}?{broadcaster}&id={{{{TWITCH_SEGMENT_ID}}}}"),
                )
            };
            Ok(Tpl {
                method: method.to_string(),
                url,
                headers: json_headers(),
                body: Some(Value::Object(body).to_string().into_bytes()),
                secret_refs: base_refs(),
            })
        }
        other => Err(FrameErr::Unsupported(format!(
            "twitch {other} is not a schedule kind"
        ))),
    }
}

/// `twitch.schedule.list`: `GET /schedule?broadcaster_id=`. A channel with no schedule answers
/// 404 and maps to an empty list (the platform's real answer, not a fallback).
pub fn list(host: &dyn HelixHost, args: &Value) -> Result<Value, ToolError> {
    let obj = object(args, &["limit", "cursor"])?;
    let mut q = format!("broadcaster_id={SLOT_BROADCASTER}");
    if let Some(l) = obj.get("limit") {
        let l = l
            .as_u64()
            .filter(|l| (1..=25).contains(l))
            .ok_or_else(|| bad("limit must be an integer 1..=25"))?;
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
    let (status, body) = get_json(host, &format!("{HELIX}/schedule?{q}"))?;
    if status == 404 {
        return Ok(json!({ "segments": [] }));
    }
    if !(200..300).contains(&status) {
        return Err(status_error(status));
    }
    let segs = body
        .get("data")
        .and_then(|d| d.get("segments"))
        .and_then(Value::as_array)
        .ok_or_else(|| coded("platform_unavailable", "malformed schedule reply"))?;
    let mut out = Vec::with_capacity(segs.len());
    for s in segs {
        let id = s
            .get("id")
            .and_then(Value::as_str)
            .filter(|i| is_segment_id(i))
            .ok_or_else(|| coded("platform_unavailable", "malformed schedule segment"))?;
        let mut o = serde_json::Map::new();
        o.insert("segment_id".to_string(), json!(id));
        for k in ["start_time", "end_time", "title", "canceled_until"] {
            if let Some(v) = s.get(k).and_then(Value::as_str) {
                o.insert(k.to_string(), json!(v));
            }
        }
        if let Some(r) = s.get("is_recurring").and_then(Value::as_bool) {
            o.insert("is_recurring".to_string(), json!(r));
        }
        if let Some(c) = s
            .get("category")
            .and_then(|c| c.get("name"))
            .and_then(Value::as_str)
        {
            o.insert("category".to_string(), json!(c));
        }
        out.push(Value::Object(o));
    }
    let mut res = json!({ "segments": out });
    if let Some(c) = body
        .get("pagination")
        .and_then(|p| p.get("cursor"))
        .and_then(Value::as_str)
    {
        res["cursor"] = json!(c);
    }
    Ok(res)
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;

    struct Fake(u16, String, Mutex<Vec<String>>);
    impl HelixHost for Fake {
        fn get(&self, url: &str) -> Result<(u16, Vec<u8>), ToolError> {
            self.2.lock().unwrap().push(url.to_string());
            Ok((self.0, self.1.clone().into_bytes()))
        }
    }

    fn create_args() -> Value {
        json!({"start_time":"2026-11-10T18:00:00Z","timezone":"America/Chicago","duration":120,"title":"Game night","is_recurring":true})
    }

    #[test]
    fn host_ops_match_the_calendar_contract_schemas() {
        let c = host_op("twitch.schedule.create", &create_args()).unwrap();
        assert_eq!(c.kind, "schedule.create");
        let mut upd = create_args();
        upd["segment_id"] = json!("ZXhhbXBsZQ==");
        let u = host_op("twitch.schedule.update", &upd).unwrap();
        assert_eq!(u.kind, "schedule.update");
        assert!(u.payload["text"].as_str().unwrap().contains("ZXhhbXBsZQ=="));
        let d = host_op("twitch.schedule.delete", &json!({"segment_id":"ZXhhbXBsZQ=="})).unwrap();
        assert_eq!(d.kind, "schedule.delete");
        // Every contract-required field is required.
        for k in FIELDS {
            let mut a = create_args();
            a.as_object_mut().unwrap().remove(k);
            assert!(host_op("twitch.schedule.create", &a).is_err(), "{k}");
        }
        assert!(host_op("twitch.schedule.update", &create_args()).is_err(), "segment_id required");
    }

    #[test]
    fn contract_bounds_are_enforced() {
        for (k, v) in [
            ("duration", json!(29)),
            ("duration", json!(1381)),
            ("duration", json!("60")),
            ("title", json!("")),
            ("title", json!("x".repeat(141))),
            ("title", json!("a{{TWITCH_TOKEN}}")),
            ("timezone", json!("")),
            ("timezone", json!("A&b")),
            ("start_time", json!("tomorrow")),
            ("is_recurring", json!("yes")),
        ] {
            let mut a = create_args();
            a[k] = v.clone();
            assert!(host_op("twitch.schedule.create", &a).is_err(), "{k}={v}");
        }
        for ok in [30, 1380] {
            let mut a = create_args();
            a["duration"] = json!(ok);
            assert!(host_op("twitch.schedule.create", &a).is_ok());
        }
        let mut a = create_args();
        a["broadcaster_id"] = json!("1");
        assert!(host_op("twitch.schedule.create", &a).is_err());
        assert!(host_op("twitch.schedule.delete", &json!({"segment_id":"a b"})).is_err());
        assert!(host_op("twitch.schedule.delete", &json!({"segment_id":"x","extra":1})).is_err());
    }

    #[test]
    fn templates_use_host_slots_and_a_string_duration() {
        let op = host_op("twitch.schedule.create", &create_args()).unwrap();
        let t = build("schedule.create", &op.payload).unwrap();
        assert_eq!(t.method, "POST");
        assert_eq!(
            t.url,
            "https://api.twitch.tv/helix/schedule/segment?broadcaster_id={{TWITCH_BROADCASTER_ID}}"
        );
        let b: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
        assert_eq!(b["duration"], "120");
        assert_eq!(b["is_recurring"], true);
        assert_eq!(b["title"], "Game night");
        // Update: the host strips segment_id; the template carries only the id SLOT.
        let host_payload = json!({"text": create_args().to_string()});
        let t = build("schedule.update", &host_payload).unwrap();
        assert_eq!(t.method, "PATCH");
        assert!(t.url.ends_with("&id={{TWITCH_SEGMENT_ID}}"));
        let del = build("schedule.delete", &json!({})).unwrap();
        assert_eq!(del.method, "DELETE");
        assert!(del.body.is_none());
    }

    #[test]
    fn a_segment_id_can_never_be_baked_into_a_template() {
        let mut a = create_args();
        a["segment_id"] = json!("evil");
        assert!(build("schedule.update", &json!({"text": a.to_string()})).is_err());
        assert!(build("schedule.delete", &json!({"text": "{\"segment_id\":\"x\"}"})).is_err());
        assert!(build("schedule.create", &json!({"text":"nope"})).is_err());
        assert!(build("schedule.create", &json!({})).is_err());
        assert!(matches!(
            build("schedule.nope", &json!({})),
            Err(FrameErr::Unsupported(_))
        ));
    }

    #[test]
    fn list_maps_segments_and_a_missing_schedule_is_an_empty_list() {
        let body = json!({"data":{"segments":[{"id":"ZXhh==","start_time":"2026-11-10T18:00:00Z","end_time":"2026-11-10T20:00:00Z","title":"t","canceled_until":null,"category":{"id":"1","name":"Chess"},"is_recurring":false}],"broadcaster_name":"x"},"pagination":{"cursor":"c1"}});
        let f = Fake(200, body.to_string(), Mutex::new(vec![]));
        let out = list(&f, &json!({"limit":5})).unwrap();
        assert_eq!(out["segments"][0]["segment_id"], "ZXhh==");
        assert_eq!(out["segments"][0]["category"], "Chess");
        assert_eq!(out["cursor"], "c1");
        assert!(out["segments"][0].get("broadcaster_name").is_none());
        assert!(f.2.lock().unwrap()[0].ends_with("schedule?broadcaster_id={{TWITCH_BROADCASTER_ID}}&first=5"));
        let none = Fake(404, "{}".into(), Mutex::new(vec![]));
        assert_eq!(list(&none, &json!({})).unwrap(), json!({"segments":[]}));
        let e = list(&Fake(429, "{}".into(), Mutex::new(vec![])), &json!({})).unwrap_err();
        assert!(matches!(e, ToolError::InvalidArgs(m) if m.starts_with("rate_limited")));
        assert!(list(&f, &json!({"limit":26})).is_err());
    }
}
