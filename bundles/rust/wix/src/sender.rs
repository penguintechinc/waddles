//! `sender.build-request`: Wix Events REST request TEMPLATES (outbound event tools).
//!
//! The connector never holds a credential and never picks a site:
//!
//! * the Wix API credential and the site id are `secret-refs` entries
//!   (`Authorization` -> [`API_KEY_HANDLE`], `wix-site-id` -> [`SITE_ID_HANDLE`]); the HOST resolves
//!   both from the community's Wix connection and substitutes them at send time. The guest never sees
//!   them, and a payload that names a site, header or URL is REFUSED (unknown fields are rejected);
//! * every request is pinned to `https://www.wixapis.com/events/v3/events...` (egress is a single host);
//! * every body is rebuilt from validated fields -- the payload is never forwarded verbatim.
//!
//! Wix Events V3 (verified 2026-11-07): Query `POST /events/v3/events/query`, Get `GET
//! /events/v3/events/{eventId}`, Create `POST /events/v3/events`, Update `PATCH
//! /events/v3/events/{event.id}` (carries the current `revision`), Cancel `POST
//! /events/v3/events/{eventId}/cancel`. Auth: Wix app / API-key identity with the "Manage Events"
//! permission. Docs: <https://dev.wix.com/docs/rest/business-solutions/events/event-management/events-v3/>
//! (`create-event`, `get-event`, `update-event`, `query-events`, `cancel-event`) and
//! <https://dev.wix.com/docs/api-reference/articles/authentication/about-authentication>.
//!
//! `booking.list` is deliberately NOT supported: Wix extended bookings embed participant contact
//! details (PII) and the connector cannot project a response, so it is refused loudly rather than
//! leaking raw PII past the API-server boundary.

use std::fmt;

use serde_json::{Map, Value, json};

/// Wix REST origin the host pins egress to.
pub const API_BASE: &str = "https://www.wixapis.com";
/// Opaque host handle for the Wix API credential (substituted into `Authorization`).
pub const API_KEY_HANDLE: &str = "wix.api_key";
/// Opaque host handle for the Wix site id (substituted into `wix-site-id`).
pub const SITE_ID_HANDLE: &str = "wix.site_id";
/// Wix event titles are bounded; Wix rejects longer values loudly with a 400.
pub const MAX_TITLE: usize = 200;
/// Wix `shortDescription` limit (Event object reference).
pub const MAX_DESCRIPTION: usize = 500;
/// Wix location `name` limit (Event object reference).
pub const MAX_LOCATION: usize = 50;
/// Wix `timeZoneId` limit (Event object reference).
pub const MAX_TIMEZONE: usize = 100;
/// Largest page the list tool may ask for.
pub const MAX_LIMIT: u64 = 100;
/// Largest offset the list tool may ask for.
pub const MAX_OFFSET: u64 = 10_000;

/// A failure the sender reports (maps 1:1 onto WIT `frame-error`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SendError {
    /// A valid shape this connector refuses (unknown action kind, `booking.list`).
    Unsupported(String),
    /// The payload violates the action's schema or a safety rule.
    Malformed(String),
}

impl fmt::Display for SendError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unsupported(m) => write!(f, "unsupported: {m}"),
            Self::Malformed(m) => write!(f, "malformed: {m}"),
        }
    }
}

impl std::error::Error for SendError {}

type Result<T> = std::result::Result<T, SendError>;

/// Mirror of WIT `sender.action`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Action {
    /// `event.list|get|create|update|cancel`.
    pub kind: String,
    /// Opaque binding id the host resolves to the community's Wix connection.
    pub target_source_id: String,
    /// Kind-specific JSON object.
    pub payload_json: String,
}

/// Mirror of WIT `sender.http-request-tpl`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpTpl {
    /// HTTP method.
    pub method: String,
    /// Absolute pinned URL.
    pub url: String,
    /// Non-secret headers.
    pub headers: Vec<(String, String)>,
    /// Request body, if any.
    pub body: Option<Vec<u8>>,
    /// `(header slot, opaque handle)`.
    pub secret_refs: Vec<(String, String)>,
}

fn bad<T>(msg: impl Into<String>) -> Result<T> {
    Err(SendError::Malformed(msg.into()))
}

/// Rejects control / invisible / lookalike-brace characters and `{{` `}}` slot delimiters (never
/// strips): the host substitutes `{{SLOT}}` credential slots into outbound bytes.
fn guard_text(field: &str, s: &str, allow_newlines: bool) -> Result<()> {
    let forbidden = |c: char| {
        matches!(
            c,
            '\u{00AD}'
                | '\u{180E}'
                | '\u{200B}'..='\u{200F}'
                | '\u{202A}'..='\u{202E}'
                | '\u{2060}'..='\u{2064}'
                | '\u{2066}'..='\u{2069}'
                | '\u{FEFF}'
                | '\u{FF5B}'
                | '\u{FF5D}'
                | '\u{2774}'
                | '\u{2775}'
                | '\u{2983}'
                | '\u{2984}'
                | '\u{FE5B}'
                | '\u{FE5C}'
        )
    };
    for c in s.chars() {
        let nl_ok = allow_newlines && (c == '\n' || c == '\t');
        if (c.is_control() && !nl_ok) || forbidden(c) {
            return bad(format!(
                "`{field}` contains a control, invisible or lookalike character"
            ));
        }
    }
    let collapsed: String = s.chars().filter(|c| !c.is_whitespace()).collect();
    for cand in [s, collapsed.as_str()] {
        if cand.contains("{{") || cand.contains("}}") {
            return bad(format!("`{field}` contains a credential-slot delimiter"));
        }
    }
    Ok(())
}

fn object(payload: &str, allowed: &[&str]) -> Result<Map<String, Value>> {
    let Ok(Value::Object(map)) = serde_json::from_str::<Value>(payload) else {
        return bad("payload is not a JSON object");
    };
    if let Some(k) = map.keys().find(|k| !allowed.contains(&k.as_str())) {
        // Includes any attempt to name a site, header, URL or credential.
        return bad(format!(
            "payload field `{k}` is not accepted for this action"
        ));
    }
    Ok(map)
}

/// A Wix id (UUID, canonical 8-4-4-4-12 hex) -- lower-cased so it is a safe path segment.
fn wix_uuid(map: &Map<String, Value>, key: &str) -> Result<String> {
    let s = map
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| SendError::Malformed(format!("missing string `{key}`")))?;
    let b = s.as_bytes();
    let ok = b.len() == 36
        && b.iter().enumerate().all(|(i, c)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                *c == b'-'
            } else {
                c.is_ascii_hexdigit()
            }
        });
    if ok {
        Ok(s.to_ascii_lowercase())
    } else {
        bad(format!("`{key}` is not a UUID"))
    }
}

/// A bounded, guarded text field; `required` fields must be non-blank.
fn text(
    map: &Map<String, Value>,
    key: &str,
    max: usize,
    required: bool,
    multiline: bool,
) -> Result<Option<String>> {
    let Some(v) = map.get(key) else {
        return if required {
            bad(format!("missing string `{key}`"))
        } else {
            Ok(None)
        };
    };
    let Some(s) = v.as_str() else {
        return bad(format!("`{key}` must be a string"));
    };
    guard_text(key, s, multiline)?;
    if s.chars().count() > max {
        return bad(format!("`{key}` exceeds {max} characters"));
    }
    if required && s.trim().is_empty() {
        return bad(format!("`{key}` is empty"));
    }
    Ok(Some(s.to_string()))
}

/// `true` for an RFC 3339 timestamp `YYYY-MM-DDTHH:MM:SS[.f+](Z|+HH:MM|-HH:MM)` with in-range fields.
/// Strict on purpose: it lands in a JSON body, and nothing but digits/`-`/`:`/`.`/`T`/`Z`/`+` may pass.
fn is_rfc3339(s: &str) -> bool {
    let b = s.as_bytes();
    let digits =
        |r: std::ops::Range<usize>| b.get(r).is_some_and(|x| x.iter().all(u8::is_ascii_digit));
    let num = |r: std::ops::Range<usize>| s.get(r).and_then(|x| x.parse::<u32>().ok());
    if b.len() < 20
        || !(digits(0..4) && b[4] == b'-' && digits(5..7) && b[7] == b'-' && digits(8..10))
        || !(b[10] == b'T'
            && digits(11..13)
            && b[13] == b':'
            && digits(14..16)
            && b[16] == b':'
            && digits(17..19))
    {
        return false;
    }
    let in_range = num(5..7).is_some_and(|m| (1..=12).contains(&m))
        && num(8..10).is_some_and(|d| (1..=31).contains(&d))
        && num(11..13).is_some_and(|h| h <= 23)
        && num(14..16).is_some_and(|m| m <= 59)
        && num(17..19).is_some_and(|x| x <= 60);
    let mut i = 19;
    if b[i] == b'.' {
        i += 1;
        let start = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == start || i - start > 9 {
            return false;
        }
    }
    let tz = &s[i..];
    let tz_ok = tz == "Z"
        || (tz.len() == 6
            && tz.is_ascii()
            && (tz.starts_with('+') || tz.starts_with('-'))
            && tz.as_bytes()[3] == b':'
            && tz[1..3].bytes().all(|c| c.is_ascii_digit())
            && tz[4..6].bytes().all(|c| c.is_ascii_digit())
            && tz[1..3].parse::<u32>().is_ok_and(|h| h <= 23)
            && tz[4..6].parse::<u32>().is_ok_and(|m| m <= 59));
    in_range && tz_ok
}

fn time_field(map: &Map<String, Value>, key: &str, required: bool) -> Result<Option<String>> {
    match map.get(key) {
        None if required => bad(format!("missing string `{key}`")),
        None => Ok(None),
        Some(Value::String(s)) if is_rfc3339(s) => Ok(Some(s.clone())),
        Some(_) => bad(format!("`{key}` is not an RFC 3339 timestamp")),
    }
}

/// IANA time-zone id shape: `Area/Location` segments of `[A-Za-z0-9_+-]`, plus `UTC`/`GMT`.
fn timezone(map: &Map<String, Value>, key: &str) -> Result<Option<String>> {
    let Some(s) = text(map, key, MAX_TIMEZONE, false, false)? else {
        return Ok(None);
    };
    let ok = !s.is_empty()
        && !s.starts_with('/')
        && !s.ends_with('/')
        && !s.contains("//")
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'/' | b'_' | b'+' | b'-'));
    if ok {
        Ok(Some(s))
    } else {
        bad(format!("`{key}` is not an IANA time-zone id"))
    }
}

fn location(map: &Map<String, Value>) -> Result<Option<Value>> {
    let name = text(map, "location_name", MAX_LOCATION, false, false)?;
    let kind = match map.get("location_type") {
        None => None,
        Some(Value::String(s)) if s == "VENUE" || s == "ONLINE" => Some(s.clone()),
        Some(_) => return bad("`location_type` must be VENUE or ONLINE"),
    };
    if name.is_none() && kind.is_none() {
        return Ok(None);
    }
    let mut loc = Map::new();
    if let Some(n) = name {
        loc.insert("name".into(), Value::String(n));
    }
    if let Some(k) = kind {
        loc.insert("type".into(), Value::String(k));
    }
    Ok(Some(Value::Object(loc)))
}

/// The common create/update event fields as a Wix `event` object (only what was supplied).
fn event_object(map: &Map<String, Value>, create: bool) -> Result<Map<String, Value>> {
    let mut ev = Map::new();
    if let Some(t) = text(map, "title", MAX_TITLE, create, false)? {
        ev.insert("title".into(), Value::String(t));
    }
    if let Some(d) = text(map, "description", MAX_DESCRIPTION, false, true)? {
        ev.insert("shortDescription".into(), Value::String(d));
    }
    let start = time_field(map, "starts_at", create)?;
    let end = time_field(map, "ends_at", create)?;
    let tz = timezone(map, "timezone")?;
    if create && tz.is_none() {
        return bad("missing string `timezone`");
    }
    if start.is_some() || end.is_some() || tz.is_some() {
        let mut dt = Map::new();
        if let Some(s) = start {
            dt.insert("startDate".into(), Value::String(s));
        }
        if let Some(e) = end {
            dt.insert("endDate".into(), Value::String(e));
        }
        if let Some(z) = tz {
            dt.insert("timeZoneId".into(), Value::String(z));
        }
        ev.insert("dateAndTimeSettings".into(), Value::Object(dt));
    }
    if let Some(l) = location(map)? {
        ev.insert("location".into(), l);
    }
    Ok(ev)
}

fn tpl(method: &str, path: &str, body: Option<Value>) -> Result<HttpTpl> {
    let body = body
        .map(|v| serde_json::to_vec(&v))
        .transpose()
        .map_err(|_| SendError::Malformed("body not serializable".into()))?;
    let headers = if body.is_some() {
        vec![("Content-Type".to_string(), "application/json".to_string())]
    } else {
        Vec::new()
    };
    Ok(HttpTpl {
        method: method.to_string(),
        url: format!("{API_BASE}{path}"),
        headers,
        body,
        secret_refs: vec![
            ("Authorization".to_string(), API_KEY_HANDLE.to_string()),
            ("wix-site-id".to_string(), SITE_ID_HANDLE.to_string()),
        ],
    })
}

fn bounded(map: &Map<String, Value>, key: &str, max: u64, default: u64) -> Result<u64> {
    match map.get(key) {
        None => Ok(default),
        Some(v) => match v.as_u64() {
            Some(n) if n <= max => Ok(n),
            _ => bad(format!("`{key}` must be an integer 0..={max}")),
        },
    }
}

fn build_list(p: &str) -> Result<HttpTpl> {
    let m = object(
        p,
        &[
            "limit",
            "offset",
            "starts_after",
            "starts_before",
            "exclude_canceled",
        ],
    )?;
    let limit = bounded(&m, "limit", MAX_LIMIT, 50)?;
    if limit == 0 {
        return bad("`limit` must be 1..=100");
    }
    let offset = bounded(&m, "offset", MAX_OFFSET, 0)?;
    let mut filter = Map::new();
    let after = time_field(&m, "starts_after", false)?;
    let before = time_field(&m, "starts_before", false)?;
    let mut range = Map::new();
    if let Some(a) = after {
        range.insert("$gte".into(), Value::String(a));
    }
    if let Some(b) = before {
        range.insert("$lte".into(), Value::String(b));
    }
    if !range.is_empty() {
        filter.insert("dateAndTimeSettings.startDate".into(), Value::Object(range));
    }
    match m.get("exclude_canceled") {
        None | Some(Value::Bool(false)) => {}
        Some(Value::Bool(true)) => {
            filter.insert("status".into(), json!({"$ne": "CANCELED"}));
        }
        Some(_) => return bad("`exclude_canceled` must be a boolean"),
    }
    let body = json!({
        "query": {
            "filter": Value::Object(filter),
            "sort": [{"fieldName": "dateAndTimeSettings.startDate", "order": "ASC"}],
            "paging": {"limit": limit, "offset": offset},
        },
        "fields": ["DETAILS", "TEXTS", "REGISTRATION", "URLS"],
    });
    tpl("POST", "/events/v3/events/query", Some(body))
}

fn build_get(p: &str) -> Result<HttpTpl> {
    let m = object(p, &["event_id"])?;
    let id = wix_uuid(&m, "event_id")?;
    tpl("GET", &format!("/events/v3/events/{id}"), None)
}

fn build_create(p: &str) -> Result<HttpTpl> {
    let m = object(
        p,
        &[
            "title",
            "description",
            "starts_at",
            "ends_at",
            "timezone",
            "location_name",
            "location_type",
        ],
    )?;
    let ev = event_object(&m, true)?;
    tpl(
        "POST",
        "/events/v3/events",
        Some(json!({"event": Value::Object(ev), "fields": ["DETAILS", "TEXTS"]})),
    )
}

fn build_update(p: &str) -> Result<HttpTpl> {
    let m = object(
        p,
        &[
            "event_id",
            "revision",
            "title",
            "description",
            "starts_at",
            "ends_at",
            "timezone",
            "location_name",
            "location_type",
        ],
    )?;
    let id = wix_uuid(&m, "event_id")?;
    let revision = m
        .get("revision")
        .and_then(Value::as_u64)
        .filter(|r| *r >= 1)
        .ok_or_else(|| SendError::Malformed("`revision` must be an integer >= 1".into()))?;
    let mut ev = event_object(&m, false)?;
    if ev.is_empty() {
        return bad("update carries no field to change");
    }
    ev.insert("id".into(), Value::String(id.clone()));
    // Wix carries the revision as a string-encoded int64 (optimistic-concurrency token).
    ev.insert("revision".into(), Value::String(revision.to_string()));
    tpl(
        "PATCH",
        &format!("/events/v3/events/{id}"),
        Some(json!({"event": Value::Object(ev), "fields": ["DETAILS", "TEXTS"]})),
    )
}

fn build_cancel(p: &str) -> Result<HttpTpl> {
    let m = object(p, &["event_id"])?;
    let id = wix_uuid(&m, "event_id")?;
    tpl(
        "POST",
        &format!("/events/v3/events/{id}/cancel"),
        Some(json!({})),
    )
}

/// Builds the request template for `action`.
///
/// # Errors
/// [`SendError::Unsupported`] for an unknown kind (including `booking.list`);
/// [`SendError::Malformed`] for an empty/invalid target, a non-object payload, a field outside the
/// kind's allow-list (notably any site/URL/credential), a bad id/time/zone, or unsafe text.
pub fn build_request(action: &Action) -> Result<HttpTpl> {
    if action.target_source_id.is_empty() || action.target_source_id.len() > 128 {
        return bad("target-source-id invalid");
    }
    guard_text("target-source-id", &action.target_source_id, false)?;
    let p = action.payload_json.as_str();
    match action.kind.as_str() {
        "event.list" => build_list(p),
        "event.get" => build_get(p),
        "event.create" => build_create(p),
        "event.update" => build_update(p),
        "event.cancel" => build_cancel(p),
        "booking.list" => Err(SendError::Unsupported(
            "booking.list is not offered: Wix bookings embed participant contact PII".into(),
        )),
        other => Err(SendError::Unsupported(format!("action kind `{other}`"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "75d64b92-4078-457a-9259-fef2948459f7";

    fn act(kind: &str, payload: &str) -> Action {
        Action {
            kind: kind.into(),
            target_source_id: "binding-1".into(),
            payload_json: payload.into(),
        }
    }

    fn body(t: &HttpTpl) -> Value {
        serde_json::from_slice(t.body.as_deref().unwrap()).unwrap()
    }

    #[test]
    fn every_request_is_pinned_tokenless_and_carries_host_secret_refs() {
        let t = build_request(&act("event.get", &format!(r#"{{"event_id":"{ID}"}}"#))).unwrap();
        assert_eq!(t.method, "GET");
        assert_eq!(
            t.url,
            format!("https://www.wixapis.com/events/v3/events/{ID}")
        );
        assert!(t.body.is_none() && t.headers.is_empty());
        assert_eq!(
            t.secret_refs,
            vec![
                ("Authorization".into(), "wix.api_key".into()),
                ("wix-site-id".into(), "wix.site_id".into())
            ]
        );
    }

    #[test]
    fn create_emits_the_exact_wix_event_shape() {
        let p = r#"{"title":"Movie night","description":"Bring snacks","starts_at":"2026-12-01T19:00:00Z","ends_at":"2026-12-01T21:00:00Z","timezone":"Europe/Vilnius","location_name":"Online","location_type":"ONLINE"}"#;
        let t = build_request(&act("event.create", p)).unwrap();
        assert_eq!(
            (t.method.as_str(), t.url.as_str()),
            ("POST", "https://www.wixapis.com/events/v3/events")
        );
        assert_eq!(
            body(&t),
            json!({"event":{"title":"Movie night","shortDescription":"Bring snacks",
              "dateAndTimeSettings":{"startDate":"2026-12-01T19:00:00Z","endDate":"2026-12-01T21:00:00Z","timeZoneId":"Europe/Vilnius"},
              "location":{"name":"Online","type":"ONLINE"}},"fields":["DETAILS","TEXTS"]})
        );
    }

    #[test]
    fn update_carries_id_revision_and_only_supplied_fields() {
        let p = format!(r#"{{"event_id":"{ID}","revision":3,"title":"New"}}"#);
        let t = build_request(&act("event.update", &p)).unwrap();
        assert_eq!(t.method, "PATCH");
        assert_eq!(
            t.url,
            format!("https://www.wixapis.com/events/v3/events/{ID}")
        );
        assert_eq!(
            body(&t)["event"],
            json!({"title":"New","id":ID,"revision":"3"})
        );
    }

    #[test]
    fn cancel_and_list_shapes() {
        let c = build_request(&act("event.cancel", &format!(r#"{{"event_id":"{ID}"}}"#))).unwrap();
        assert_eq!(c.method, "POST");
        assert_eq!(
            c.url,
            format!("https://www.wixapis.com/events/v3/events/{ID}/cancel")
        );
        let l = build_request(&act(
            "event.list",
            r#"{"limit":10,"offset":20,"starts_after":"2026-12-01T00:00:00Z","exclude_canceled":true}"#,
        ))
        .unwrap();
        assert_eq!(l.url, "https://www.wixapis.com/events/v3/events/query");
        let b = body(&l);
        assert_eq!(b["query"]["paging"], json!({"limit":10,"offset":20}));
        assert_eq!(b["query"]["filter"]["status"], json!({"$ne":"CANCELED"}));
        assert_eq!(
            b["query"]["filter"]["dateAndTimeSettings.startDate"],
            json!({"$gte":"2026-12-01T00:00:00Z"})
        );
        let d = build_request(&act("event.list", "{}")).unwrap();
        assert_eq!(body(&d)["query"]["paging"], json!({"limit":50,"offset":0}));
    }

    #[test]
    fn a_payload_can_never_name_a_site_url_header_or_credential() {
        for k in [
            "site_id",
            "url",
            "headers",
            "Authorization",
            "api_key",
            "instance_id",
        ] {
            let p = format!(r#"{{"event_id":"{ID}","{k}":"x"}}"#);
            for kind in ["event.get", "event.cancel"] {
                assert!(
                    matches!(build_request(&act(kind, &p)), Err(SendError::Malformed(_))),
                    "{kind} {k}"
                );
            }
        }
    }

    #[test]
    fn ids_times_zones_and_text_reject_injection_and_bad_shapes() {
        for id in [
            "../x",
            "123",
            &format!("{ID}/cancel"),
            "75d64b92-4078-457a-9259-fef2948459fz",
        ] {
            let p = format!(r#"{{"event_id":"{id}"}}"#);
            assert!(build_request(&act("event.get", &p)).is_err(), "{id}");
        }
        let ok = r#""starts_at":"2026-12-01T19:00:00Z","ends_at":"2026-12-01T20:00:00Z","timezone":"UTC""#;
        for title in ["", "  ", "{{WIX_KEY}}", "a\u{200B}b", "x\ny"] {
            let p = format!(
                r#"{{"title":{},{ok}}}"#,
                serde_json::to_string(title).unwrap()
            );
            assert!(
                build_request(&act("event.create", &p)).is_err(),
                "{title:?}"
            );
        }
        for tz in ["../etc", "A//B", "/UTC", "UTC;x", ""] {
            let p = format!(
                r#"{{"title":"t","starts_at":"2026-12-01T19:00:00Z","ends_at":"2026-12-01T20:00:00Z","timezone":"{tz}"}}"#
            );
            assert!(build_request(&act("event.create", &p)).is_err(), "{tz}");
        }
        for ts in [
            "2026-13-01T00:00:00Z",
            "tomorrow",
            "2026-12-01 19:00:00Z",
            "2026-12-01T19:00:00",
        ] {
            let p = format!(
                r#"{{"title":"t","starts_at":"{ts}","ends_at":"2026-12-01T20:00:00Z","timezone":"UTC"}}"#
            );
            assert!(build_request(&act("event.create", &p)).is_err(), "{ts}");
        }
        assert!(build_request(&act("event.create", r#"{"title":"t","starts_at":"2026-12-01T19:00:00Z","ends_at":"2026-12-01T20:00:00Z"}"#)).is_err(), "timezone required");
        assert!(
            build_request(&act(
                "event.create",
                &format!(r#"{{"title":"{}",{ok}}}"#, "a".repeat(201))
            ))
            .is_err()
        );
        assert!(
            build_request(&act(
                "event.create",
                &format!(r#"{{"title":"t","location_type":"MOON",{ok}}}"#)
            ))
            .is_err()
        );
    }

    #[test]
    fn update_requires_revision_and_a_change_and_list_bounds_hold() {
        assert!(
            build_request(&act(
                "event.update",
                &format!(r#"{{"event_id":"{ID}","title":"t"}}"#)
            ))
            .is_err()
        );
        assert!(
            build_request(&act(
                "event.update",
                &format!(r#"{{"event_id":"{ID}","revision":0,"title":"t"}}"#)
            ))
            .is_err()
        );
        assert!(
            build_request(&act(
                "event.update",
                &format!(r#"{{"event_id":"{ID}","revision":1}}"#)
            ))
            .is_err()
        );
        for p in [
            r#"{"limit":0}"#,
            r#"{"limit":101}"#,
            r#"{"offset":10001}"#,
            r#"{"limit":"5"}"#,
            r#"{"exclude_canceled":"yes"}"#,
            "[]",
            "nope",
        ] {
            assert!(build_request(&act("event.list", p)).is_err(), "{p}");
        }
    }

    #[test]
    fn booking_list_and_unknown_kinds_fail_loud_and_target_is_validated() {
        assert!(matches!(
            build_request(&act("booking.list", "{}")),
            Err(SendError::Unsupported(_))
        ));
        assert!(matches!(
            build_request(&act("event.delete", "{}")),
            Err(SendError::Unsupported(_))
        ));
        let mut a = act("event.list", "{}");
        a.target_source_id = String::new();
        assert!(build_request(&a).is_err());
        a.target_source_id = "a".repeat(129);
        assert!(build_request(&a).is_err());
        a.target_source_id = "{{X}}".into();
        assert!(build_request(&a).is_err());
    }
}
