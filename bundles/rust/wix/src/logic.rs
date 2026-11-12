//! Pure Wix event mapping (no WASM types): a host-VERIFIED webhook JWT in,
//! normalized event records out. The host (svc-ingest `Scheme::WixJwt`) has
//! already checked the RS256 signature, `alg`, freshness, instance binding and
//! replay; this module therefore decodes the payload WITHOUT re-verifying and
//! must never be fed an unverified body. It never emits an email, name or any
//! free-form member data: member events carry only ids that the component then
//! tokenizes through `identity.lookup`.

use serde_json::{Map, Value, json};

/// Why a frame could not be mapped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LogicError {
    /// The frame is not a well-formed Wix webhook envelope.
    Malformed(String),
}

type Result<T> = std::result::Result<T, LogicError>;

fn bad<T>(msg: &str) -> Result<T> {
    Err(LogicError::Malformed(msg.to_owned()))
}

/// One mapped event; `member` is the Wix member id the component must tokenize.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mapped {
    pub platform: String,
    pub event_type: String,
    pub member: Option<String>,
    pub payload_json: String,
    pub occurred_at: String,
}

/// The mapping result: events plus the count of well-formed but ignored events.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Parsed {
    pub events: Vec<Mapped>,
    pub skipped: usize,
}

/// Max chars kept from free-text display fields (event title / location).
const MAX_TEXT: usize = 256;

fn b64url_decode(s: &str) -> Result<Vec<u8>> {
    let mut out = Vec::with_capacity(s.len() * 3 / 4);
    let (mut acc, mut bits) = (0u32, 0u32);
    for c in s.bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            _ => return bad("jwt segment is not base64url"),
        };
        acc = (acc << 6) | u32::from(v);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((acc >> bits) & 0xff) as u8);
        }
    }
    Ok(out)
}

fn json_str(v: &Value, key: &str) -> Result<Value> {
    let s = v
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| LogicError::Malformed(format!("missing string `{key}`")))?;
    serde_json::from_str(s).map_err(|_| LogicError::Malformed(format!("`{key}` is not JSON")))
}

fn req_str<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| LogicError::Malformed(format!("missing `{key}`")))
}

/// `^[A-Za-z0-9_-]{1,128}$` (Wix member/event ids are GUID-like).
fn is_ref(s: &str) -> bool {
    (1..=128).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn checked_ref(v: &str, what: &str) -> Result<String> {
    if is_ref(v) {
        Ok(v.to_owned())
    } else {
        Err(LogicError::Malformed(format!("`{what}` is not a valid id")))
    }
}

/// Display text: control characters removed, length-capped (chars, not bytes).
fn text(v: Option<&Value>) -> Option<String> {
    v.and_then(Value::as_str).map(|s| {
        s.chars()
            .filter(|c| !c.is_control())
            .take(MAX_TEXT)
            .collect()
    })
}

/// The JSON-string entity carried by the domain event (create/update/delete/action).
fn entity(event: &Value) -> Result<Value> {
    for (outer, inner) in [
        ("createdEvent", "entity"),
        ("updatedEvent", "currentEntity"),
        ("actionEvent", "body"),
    ] {
        if let Some(o) = event.get(outer) {
            return json_str(o, inner);
        }
    }
    if let Some(d) = event.get("deletedEvent") {
        return match d.get("deletedEntity").and_then(Value::as_str) {
            Some(s) => serde_json::from_str(s)
                .map_err(|_| LogicError::Malformed("`deletedEntity` is not JSON".into())),
            None => Ok(Value::Null),
        };
    }
    bad("domain event carries no entity")
}

struct Ctx<'a> {
    event_id: &'a str,
    instance_id: &'a str,
    occurred_at: String,
    /// `true` when `occurred_at` is Wix's own `eventTime` (orderable), `false` for the receive-time fallback.
    wix_time: bool,
}

impl Ctx<'_> {
    fn base(&self) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert("event_id".into(), json!(self.event_id));
        m.insert("instance_id".into(), json!(self.instance_id));
        m.insert(
            "time_source".into(),
            json!(if self.wix_time { "wix" } else { "received" }),
        );
        m
    }

    fn mapped(&self, kind: &str, member: Option<String>, extra: Map<String, Value>) -> Mapped {
        let mut payload = self.base();
        payload.extend(extra);
        Mapped {
            platform: "wix".into(),
            event_type: kind.into(),
            member,
            payload_json: Value::Object(payload).to_string(),
            occurred_at: self.occurred_at.clone(),
        }
    }
}

fn member_event(ctx: &Ctx<'_>, kind: &str, event: &Value) -> Result<Mapped> {
    let id = checked_ref(req_str(event, "entityId")?, "entityId")?;
    Ok(ctx.mapped(kind, Some(id), Map::new()))
}

fn calendar_event(ctx: &Ctx<'_>, kind: &str, deleted: bool, ent: &Value) -> Result<Mapped> {
    let ev = ent.get("event").unwrap_or(ent);
    let id = ev
        .get("_id")
        .or_else(|| ev.get("id"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| LogicError::Malformed("event entity has no id".into()))?;
    let when = ev.get("dateAndTimeSettings");
    let mut extra = Map::new();
    extra.insert("wix_event_id".into(), json!(checked_ref(id, "event id")?));
    extra.insert("title".into(), json!(text(ev.get("title"))));
    extra.insert(
        "starts_at".into(),
        json!(text(when.and_then(|w| w.get("startDate")))),
    );
    extra.insert(
        "ends_at".into(),
        json!(text(when.and_then(|w| w.get("endDate")))),
    );
    extra.insert(
        "timezone".into(),
        json!(text(when.and_then(|w| w.get("timeZoneId")))),
    );
    extra.insert(
        "location".into(),
        json!(text(ev.get("location").and_then(|l| l.get("name")))),
    );
    let status = if deleted {
        Some("DELETED".to_owned())
    } else {
        text(ev.get("status"))
    };
    extra.insert("status".into(), json!(status));
    Ok(ctx.mapped(kind, None, extra))
}

/// Maps one VERIFIED webhook body (the raw JWT) into normalized events.
///
/// # Errors
/// [`LogicError::Malformed`] when the envelope is not the documented shape.
pub fn parse_frame(bytes: &[u8], received_at: &str) -> Result<Parsed> {
    let text = std::str::from_utf8(bytes)
        .map_err(|_| LogicError::Malformed("frame is not UTF-8".into()))?
        .trim_end_matches(['\r', '\n']);
    let mut seg = text.split('.');
    let (Some(_), Some(payload), Some(_), None) = (seg.next(), seg.next(), seg.next(), seg.next())
    else {
        return bad("frame is not a compact JWT");
    };
    let payload: Value = serde_json::from_slice(&b64url_decode(payload)?)
        .map_err(|_| LogicError::Malformed("jwt payload is not JSON".into()))?;
    let envelope = json_str(&payload, "data")?;
    let instance_id = req_str(&envelope, "instanceId")?;
    let event_type = req_str(&envelope, "eventType")?;
    let event = json_str(&envelope, "data")?;
    let event_id = req_str(&event, "id")?;
    let (occurred_at, wix_time) = match event.get("eventTime").and_then(Value::as_str) {
        Some(t) if !t.is_empty() => (t.to_owned(), true),
        _ => (received_at.to_owned(), false),
    };
    let ctx = Ctx {
        event_id,
        instance_id: &checked_ref(instance_id, "instanceId")?,
        occurred_at,
        wix_time,
    };
    let mapped = map_event(&ctx, event_type, &event)?;
    Ok(match mapped {
        Some(m) => Parsed {
            events: vec![m],
            skipped: 0,
        },
        None => Parsed {
            events: Vec::new(),
            skipped: 1,
        },
    })
}

fn map_event(ctx: &Ctx<'_>, event_type: &str, event: &Value) -> Result<Option<Mapped>> {
    if let Some(s) = event_type.strip_prefix("wix.members.v1.") {
        return match s {
            "member_created" => member_event(ctx, "member.linked", event).map(Some),
            "member_updated" => member_event(ctx, "member.updated", event).map(Some),
            "member_deleted" => member_event(ctx, "member.deleted", event).map(Some),
            _ => Ok(None),
        };
    }
    // Pricing-plan / order / subscription webhooks are deliberately NOT mapped:
    // Wix syncs EVENTS only; product subscriptions go through the PenguinTech
    // license server (decision 2026-11-12). They fall through to `skipped`.
    if let Some(s) = event_type.strip_prefix("wix.events.v3.") {
        let (kind, deleted) = match s {
            "event_created" => ("event.created", false),
            "event_updated" => ("event.updated", false),
            "event_canceled" => ("event.canceled", false),
            "event_deleted" => ("event.canceled", true),
            _ => return Ok(None),
        };
        return calendar_event(ctx, kind, deleted, &entity(event)?).map(Some);
    }
    Ok(None)
}
