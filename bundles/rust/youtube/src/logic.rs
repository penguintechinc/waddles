//! Pure, host-testable YouTube overlay logic (no WASM bindings here).
//!
//! Everything the Python path did in `receivers/youtube_live_poll.py` and
//! `builtin_handlers/youtube_live_ingest.py` that is *platform knowledge*
//! lives here: live-chat message parsing, normalization, page-token /
//! polling-interval extraction, Data API error classification and backoff,
//! broadcast-discovery request templates, and the chat-send request template.
//! Transport (the poll loop, SSRF pinning, credentials) stays in the host.

use serde::Serialize;
use serde_json::{Map, Value, json};

/// Data API v3 base URL (declared as the only egress host family).
pub const API_BASE: &str = "https://www.googleapis.com/youtube/v3";
/// `pollingIntervalMillis` fallback when the response omits it.
pub const DEFAULT_POLL_INTERVAL_MS: u64 = 5000;
/// Floor under the API-reported interval so a bad response never tight-loops.
pub const MIN_POLL_INTERVAL_MS: u64 = 2000;
/// Default backoff when no broadcast is live / quota is hit.
pub const NO_BROADCAST_BACKOFF_S: f64 = 30.0;
/// Consecutive quota errors before the poller stops.
pub const MAX_CONSECUTIVE_QUOTA_ERRORS: u32 = 5;
/// `search.list` candidate cap.
pub const LIVE_SEARCH_MAX_RESULTS: u32 = 5;
/// Longest chat message YouTube accepts (characters).
pub const MAX_CHAT_CHARS: usize = 200;
/// Placeholder the host substitutes with the bearer token (never a literal).
pub const TOKEN_SLOT: &str = "{{YT_OAUTH_TOKEN}}";
/// Opaque handle the host resolves through the credential adapter.
pub const TOKEN_HANDLE: &str = "youtube.oauth";

const QUOTA_REASONS: [&str; 4] = [
    "quotaExceeded",
    "dailyLimitExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
];

/// Typed failure; mapped by the component onto the WIT `frame-error`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LogicError {
    /// Input is not what the platform contract allows.
    Malformed(String),
    /// Verb/step the overlay does not implement.
    Unsupported(String),
}

/// One normalized event, field-for-field the WIT `normalized-event`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    /// Platform slug.
    pub platform: String,
    /// Event type (`message`).
    pub event_type: String,
    /// Raw actor id (tokenized downstream by the PII stage).
    pub actor: Option<String>,
    /// JSON payload text.
    pub payload_json: String,
    /// RFC 3339 timestamp.
    pub occurred_at: String,
}

/// Result of parsing one frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Parsed {
    /// Normalized events, in API order.
    pub events: Vec<Event>,
    /// Items dropped (no displayable text / no liveChatId); count only, never content.
    pub skipped: usize,
}

/// Mirrors Python `str.strip()` (Unicode whitespace plus 0x1c-0x1f).
fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Non-empty string or `null` (Python `_as_str_or_none`).
fn str_or_none(v: Option<&Value>) -> Value {
    match v {
        Some(Value::String(s)) if !s.is_empty() => Value::String(s.clone()),
        _ => Value::Null,
    }
}

/// Real boolean `true` only; everything else is `false` (fail closed, Python `strict_flag`).
fn strict_flag(v: Option<&Value>) -> bool {
    matches!(v, Some(Value::Bool(true)))
}

/// Normalizes one receiver-raw event dict (the Python `normalize()` contract).
///
/// Requires non-empty `text` and `live_chat_id`; `actor` is `author_id` or
/// `"unknown"`; `occurred_at` falls back `occurred_at` -> `published_at` ->
/// `received_at`. Returns `Malformed` on a missing required field.
pub fn normalize_raw(raw: &Map<String, Value>, received_at: &str) -> Result<Event, LogicError> {
    let text = match raw.get("text") {
        Some(Value::String(t)) if !t.is_empty() => t,
        _ => {
            return Err(LogicError::Malformed(
                "raw YouTube Live event missing required 'text' string field".into(),
            ));
        }
    };
    let live_chat_id = match raw.get("live_chat_id") {
        Some(Value::String(c)) if !c.is_empty() => c,
        _ => {
            return Err(LogicError::Malformed(
                "raw YouTube Live event missing required 'live_chat_id' string field".into(),
            ));
        }
    };
    let actor = match raw.get("author_id") {
        Some(Value::String(a)) if !a.is_empty() => a.clone(),
        _ => "unknown".to_string(),
    };
    let platform = match raw.get("platform") {
        Some(Value::String(p)) if !p.is_empty() => p.clone(),
        _ => "youtube".to_string(),
    };
    let occurred_at = ["occurred_at", "published_at"]
        .iter()
        .find_map(|k| match raw.get(*k) {
            Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
            _ => None,
        })
        .unwrap_or_else(|| received_at.to_string());
    let payload = json!({
        "text": py_strip(text),
        "video_id": str_or_none(raw.get("video_id")),
        "live_chat_id": live_chat_id,
        "author_id": str_or_none(raw.get("author_id")),
        "display_name": str_or_none(raw.get("display_name")),
        "is_mod": strict_flag(raw.get("is_mod")),
        "is_owner": strict_flag(raw.get("is_owner")),
        "is_sponsor": strict_flag(raw.get("is_sponsor")),
        "message_id": str_or_none(raw.get("message_id")),
        "published_at": str_or_none(raw.get("published_at")),
    });
    Ok(Event {
        platform,
        event_type: "message".into(),
        actor: Some(actor),
        payload_json: payload.to_string(),
        occurred_at,
    })
}

/// Maps one `liveChatMessages` item to the raw-event dict, or `None` if it has
/// no displayable text or no `liveChatId` (skipped, never raised: one bad item
/// must not poison the window).
fn item_to_raw(item: &Value) -> Option<Map<String, Value>> {
    let snippet = item.get("snippet").and_then(Value::as_object);
    let author = item.get("authorDetails").and_then(Value::as_object);
    let get = |m: Option<&Map<String, Value>>, k: &str| m.and_then(|m| m.get(k)).cloned();
    let text = match get(snippet, "displayMessage") {
        Some(Value::String(t)) if !t.is_empty() => t,
        _ => return None,
    };
    let chat = match get(snippet, "liveChatId") {
        Some(Value::String(c)) if !c.is_empty() => c,
        _ => return None,
    };
    let mut m = Map::new();
    m.insert("platform".into(), json!("youtube"));
    m.insert("text".into(), json!(text));
    m.insert("live_chat_id".into(), json!(chat));
    m.insert(
        "author_id".into(),
        str_or_none(get(author, "channelId").as_ref()),
    );
    m.insert(
        "display_name".into(),
        str_or_none(get(author, "displayName").as_ref()),
    );
    for (out, key) in [
        ("is_mod", "isChatModerator"),
        ("is_owner", "isChatOwner"),
        ("is_sponsor", "isChatSponsor"),
    ] {
        m.insert(out.into(), get(author, key).unwrap_or(Value::Null));
    }
    m.insert("message_id".into(), str_or_none(item.get("id")));
    m.insert(
        "published_at".into(),
        str_or_none(get(snippet, "publishedAt").as_ref()),
    );
    Some(m)
}

/// Parses one poll frame. Accepts either a real `liveChat/messages` list
/// response (`items` array -- what the host poll transport yields) or a
/// receiver-raw event dict (`text` present -- the Python/corpus contract).
/// `video_id` is not derivable from a list response (it is host config), so it
/// is `null` on the response path.
pub fn parse_frame(bytes: &[u8], received_at: &str) -> Result<Parsed, LogicError> {
    let v: Value = serde_json::from_slice(bytes)
        .map_err(|_| LogicError::Malformed("frame is not JSON".into()))?;
    let obj = v
        .as_object()
        .ok_or_else(|| LogicError::Malformed("frame is not a JSON object".into()))?;
    if let Some(items) = obj.get("items") {
        let items = items
            .as_array()
            .ok_or_else(|| LogicError::Malformed("'items' is not an array".into()))?;
        let mut events = Vec::new();
        let mut skipped = 0;
        for item in items {
            match item_to_raw(item).map(|r| normalize_raw(&r, received_at)) {
                Some(Ok(e)) => events.push(e),
                _ => skipped += 1,
            }
        }
        return Ok(Parsed { events, skipped });
    }
    if obj.contains_key("text") || obj.contains_key("live_chat_id") {
        return Ok(Parsed {
            events: vec![normalize_raw(obj, received_at)?],
            skipped: 0,
        });
    }
    Err(LogicError::Malformed(
        "frame is neither a live chat list response nor a raw event".into(),
    ))
}

/// Next-poll plan extracted from a list response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PollPlan {
    /// `nextPageToken` (the cursor), if any.
    pub next_page_token: Option<String>,
    /// Floored polling interval in ms.
    pub interval_ms: u64,
}

/// Honors the API's own `nextPageToken`/`pollingIntervalMillis` (floored).
pub fn poll_plan(body: &Value) -> PollPlan {
    let token = match body.get("nextPageToken") {
        Some(Value::String(t)) if !t.is_empty() => Some(t.clone()),
        _ => None,
    };
    let ms = match body.get("pollingIntervalMillis") {
        Some(Value::Number(n)) => n
            .as_u64()
            .or_else(|| n.as_f64().map(|f| if f < 0.0 { 0 } else { f as u64 }))
            .unwrap_or(DEFAULT_POLL_INTERVAL_MS),
        _ => DEFAULT_POLL_INTERVAL_MS,
    };
    PollPlan {
        next_page_token: token,
        interval_ms: ms.max(MIN_POLL_INTERVAL_MS),
    }
}

/// Google's error `reason` from a 403 body (Python `_describe_403_reason`).
pub fn describe_403_reason(body: &Value) -> String {
    let Some(err) = body.get("error").and_then(Value::as_object) else {
        return "forbidden".into();
    };
    if let Some(Value::String(r)) = err
        .get("errors")
        .and_then(Value::as_array)
        .and_then(|a| a.first())
        .and_then(|e| e.get("reason"))
        && !r.is_empty()
    {
        return r.clone();
    }
    for k in ["status", "message"] {
        if let Some(Value::String(s)) = err.get(k)
            && !s.is_empty()
        {
            return s.clone();
        }
    }
    "forbidden".into()
}

/// What the host should do after a Data API error.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ErrorVerdict {
    /// `quota` | `chat_unavailable` | `unauthorized` | `retryable` | `fatal`.
    pub class: &'static str,
    /// `backoff` | `stop` | `rediscover` | `refresh_credentials` | `retry` | `fail`.
    pub action: &'static str,
    /// Seconds to wait before the next attempt (0 when not applicable).
    pub backoff_s: f64,
    /// Updated consecutive-quota counter.
    pub consecutive_quota_errors: u32,
    /// Google reason for 403s.
    pub reason: Option<String>,
}

/// Classifies an HTTP error exactly as `_data_api_get` + `_poll_loop` did.
pub fn classify_error(
    status: u16,
    body: &Value,
    consecutive_quota_errors: u32,
    backoff_s: f64,
    max_quota_errors: u32,
) -> ErrorVerdict {
    let v = |class, action, backoff, n, reason| ErrorVerdict {
        class,
        action,
        backoff_s: backoff,
        consecutive_quota_errors: n,
        reason,
    };
    match status {
        401 => v(
            "unauthorized",
            "refresh_credentials",
            0.0,
            consecutive_quota_errors,
            None,
        ),
        403 => {
            let reason = describe_403_reason(body);
            if QUOTA_REASONS.contains(&reason.as_str()) {
                let n = consecutive_quota_errors.saturating_add(1);
                if n >= max_quota_errors {
                    v("quota", "stop", 0.0, n, Some(reason))
                } else {
                    v("quota", "backoff", backoff_s, n, Some(reason))
                }
            } else {
                // Chat ended/disabled/forbidden: state reset, quota counter untouched.
                v(
                    "chat_unavailable",
                    "rediscover",
                    backoff_s,
                    consecutive_quota_errors,
                    Some(reason),
                )
            }
        }
        429 | 500.. => v(
            "retryable",
            "retry",
            backoff_s,
            consecutive_quota_errors,
            None,
        ),
        _ => v("fatal", "fail", 0.0, consecutive_quota_errors, None),
    }
}

/// A request the host transmits (WIT `http-request-tpl` shape).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestTpl {
    /// HTTP method.
    pub method: String,
    /// Fully-qualified URL.
    pub url: String,
    /// Header pairs.
    pub headers: Vec<(String, String)>,
    /// Optional body bytes.
    pub body: Option<Vec<u8>>,
    /// `(slot, handle)` pairs; the host substitutes the credential.
    pub secret_refs: Vec<(String, String)>,
}

/// RFC 3986 percent-encoding of a query value.
pub fn pct(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(char::from(b));
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn require<'a>(v: &'a Value, key: &str) -> Result<&'a str, LogicError> {
    match v.get(key) {
        Some(Value::String(s)) if !s.is_empty() => Ok(s),
        _ => Err(LogicError::Malformed(format!("missing string field {key}"))),
    }
}

fn bearer_get(url: String) -> RequestTpl {
    RequestTpl {
        method: "GET".into(),
        url,
        headers: vec![("Authorization".into(), format!("Bearer {TOKEN_SLOT}"))],
        body: None,
        secret_refs: vec![(TOKEN_SLOT.into(), TOKEN_HANDLE.into())],
    }
}

/// `search.list?eventType=live` template (100 quota units; only while no chat is known).
pub fn discovery_search_request(channel_id: &str) -> RequestTpl {
    bearer_get(format!(
        "{API_BASE}/search?part=id%2Csnippet&channelId={}&eventType=live&type=video&maxResults={LIVE_SEARCH_MAX_RESULTS}",
        pct(channel_id)
    ))
}

/// `videos.list?part=liveStreamingDetails` template (1 quota unit).
pub fn discovery_videos_request(video_id: &str) -> RequestTpl {
    bearer_get(format!(
        "{API_BASE}/videos?part=liveStreamingDetails&id={}",
        pct(video_id)
    ))
}

/// `liveChat/messages` poll template for the host's poll-config builder.
pub fn poll_request(live_chat_id: &str, max_results: u32) -> RequestTpl {
    bearer_get(format!(
        "{API_BASE}/liveChat/messages?part=id%2Csnippet%2CauthorDetails&liveChatId={}&maxResults={max_results}",
        pct(live_chat_id)
    ))
}

/// First candidate `videoId` from a `search.list` response.
pub fn parse_search(body: &Value) -> Option<String> {
    body.get("items")?.as_array()?.iter().find_map(|i| {
        match i.get("id").and_then(|id| id.get("videoId")) {
            Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
            _ => None,
        }
    })
}

/// `activeLiveChatId` from a `videos.list` response.
pub fn parse_videos(body: &Value) -> Option<String> {
    let live = body
        .get("items")?
        .as_array()?
        .first()?
        .get("liveStreamingDetails")?;
    match live.get("activeLiveChatId") {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

/// Builds the `liveChatMessages.insert` template for verb `chat.send`.
///
/// `target` is the HOST-resolved opaque source id for the binding (for YouTube
/// it is the `liveChatId`); the payload supplies only the text, so an app
/// cannot redirect a message to another chat. The credential is a slot the
/// host substitutes -- the overlay never holds a token.
pub fn build_chat_send(
    kind: &str,
    target: &str,
    payload_json: &str,
) -> Result<RequestTpl, LogicError> {
    if kind != "chat.send" {
        return Err(LogicError::Unsupported(format!(
            "unsupported action kind: {kind}"
        )));
    }
    if target.is_empty() {
        return Err(LogicError::Malformed("target-source-id is empty".into()));
    }
    let payload: Value = serde_json::from_str(payload_json)
        .map_err(|_| LogicError::Malformed("payload is not JSON".into()))?;
    let text = py_strip(require(&payload, "text")?);
    if text.is_empty() {
        return Err(LogicError::Malformed("text is blank".into()));
    }
    if text.chars().count() > MAX_CHAT_CHARS {
        return Err(LogicError::Malformed(format!(
            "text exceeds {MAX_CHAT_CHARS} characters"
        )));
    }
    let body = json!({"snippet": {
        "liveChatId": target,
        "type": "textMessageEvent",
        "textMessageDetails": {"messageText": text},
    }});
    Ok(RequestTpl {
        method: "POST".into(),
        url: format!("{API_BASE}/liveChat/messages?part=snippet"),
        headers: vec![
            ("Authorization".into(), format!("Bearer {TOKEN_SLOT}")),
            ("Content-Type".into(), "application/json".into()),
        ],
        body: Some(body.to_string().into_bytes()),
        secret_refs: vec![(TOKEN_SLOT.into(), TOKEN_HANDLE.into())],
    })
}

/// Serializes a template for the tool results.
pub fn tpl_json(t: &RequestTpl) -> Value {
    json!({
        "method": t.method,
        "url": t.url,
        "headers": t.headers.iter().map(|(n, v)| json!({"name": n, "value": v})).collect::<Vec<_>>(),
        "body": t.body.as_ref().map(|b| String::from_utf8_lossy(b).into_owned()),
        "secret_refs": t.secret_refs,
    })
}

fn ser<T: Serialize>(v: T) -> Result<Value, LogicError> {
    serde_json::to_value(v).map_err(|e| LogicError::Malformed(e.to_string()))
}

/// Tool dispatch (`tool-provider.call`): pure JSON in/out; no tool performs I/O.
pub fn run_tool(name: &str, args: &Value) -> Result<Value, LogicError> {
    let body = || {
        args.get("body")
            .ok_or_else(|| LogicError::Malformed("missing body".into()))
    };
    match name {
        "discovery_request" => match require(args, "step")? {
            "search" => Ok(tpl_json(&discovery_search_request(require(
                args,
                "channel_id",
            )?))),
            "videos" => Ok(tpl_json(&discovery_videos_request(require(
                args, "video_id",
            )?))),
            s => Err(LogicError::Malformed(format!(
                "unknown discovery step: {s}"
            ))),
        },
        "discovery_parse" => match require(args, "step")? {
            "search" => Ok(json!({"video_id": parse_search(body()?)})),
            "videos" => Ok(json!({"live_chat_id": parse_videos(body()?)})),
            s => Err(LogicError::Malformed(format!(
                "unknown discovery step: {s}"
            ))),
        },
        "poll_request" => {
            let max = args
                .get("max_results")
                .and_then(Value::as_u64)
                .unwrap_or(200);
            if !(1..=2000).contains(&max) {
                return Err(LogicError::Malformed(
                    "max_results out of range 1..=2000".into(),
                ));
            }
            let max = u32::try_from(max).unwrap_or(200);
            Ok(tpl_json(&poll_request(require(args, "live_chat_id")?, max)))
        }
        "poll_plan" => ser(poll_plan(body()?)),
        "classify_error" => {
            let status = args
                .get("status")
                .and_then(Value::as_u64)
                .and_then(|s| u16::try_from(s).ok())
                .ok_or_else(|| LogicError::Malformed("missing numeric status".into()))?;
            let num = |k: &str, d: u32| {
                args.get(k)
                    .and_then(Value::as_u64)
                    .map_or(d, |n| u32::try_from(n).unwrap_or(u32::MAX))
            };
            let backoff = args
                .get("no_broadcast_backoff_s")
                .and_then(Value::as_f64)
                .unwrap_or(NO_BROADCAST_BACKOFF_S);
            let null = Value::Null;
            ser(classify_error(
                status,
                args.get("body").unwrap_or(&null),
                num("consecutive_quota_errors", 0),
                backoff,
                num("max_consecutive_quota_errors", MAX_CONSECUTIVE_QUOTA_ERRORS),
            ))
        }
        "build_send_chat_request" => {
            let payload = args
                .get("payload")
                .map(Value::to_string)
                .unwrap_or_default();
            Ok(tpl_json(&build_chat_send(
                "chat.send",
                require(args, "target_source_id")?,
                &payload,
            )?))
        }
        other => Err(LogicError::Malformed(format!("unknown tool: {other}"))),
    }
}
