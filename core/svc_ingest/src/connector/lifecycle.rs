//! Host-owned, declarative session-lifecycle rules for the generic WebSocket
//! and poll transports (P6b session lifecycle). Same pattern as
//! `AutoReplyRule`: the rules are HOST CONFIG, never supplied by a guest, and
//! no platform is named here -- a Discord connector and an IRC connector differ
//! only in the rule values their host config carries. The frozen `connector`
//! WIT world is untouched: the guest still only sees `receiver` calls, and the
//! host feeds session state back to it through the existing opaque
//! `session-token` and the handshake bytes it already returns.
//!
//! # WebSocket ([`SessionRules`] / [`SessionEngine`])
//!
//! * **captures** -- pull a named value out of a matched frame (JSON pointer),
//!   e.g. READY `/d/session_id` or each dispatch's `/s` sequence. Values live
//!   in per-transport host state ([`SessionEngine`]), bounded
//!   ([`MAX_STATE_VALUES`] names of at most [`MAX_CAPTURE_LEN`] bytes each),
//!   validated against a strict charset (REJECTED, never sanitized -- an
//!   illegal value fails the frame), never logged and never a metric label.
//! * **heartbeat** -- a capture whose value is a number of milliseconds that
//!   becomes the transport's heartbeat interval, bounded by the driver's
//!   `MIN/MAX_HEARTBEAT_INTERVAL`.
//! * **directives** -- a matched frame maps to `reconnect`, `reconnect_fresh`
//!   (drop session state) or `park`.
//! * **close codes** -- WS close-code ranges map to the same actions;
//!   unclassified codes keep the old behaviour (reconnect under backoff).
//! * **templates** -- `{{NAME}}` / `{{NAME|default}}` host slots substituted
//!   from captured state into the next handshake, into the opaque
//!   `session-token`, and into a host-owned default heartbeat body.
//!
//! # Poll ([`PollLifecycle`])
//!
//! * an **interval hint** pointer (e.g. `/pollingIntervalMillis`) clamped to
//!   a configured `[min, max]`;
//! * **HTTP error rules** classifying a status by a JSON-pointer `reason` into
//!   a bounded back-off (exponential, `Retry-After` aware) or a park;
//! * a **query secret** slot: a host-substituted credential in the URL query,
//!   percent-encoded by the url crate and never present in any log, metric or
//!   error string.
//!
//! Everything is validated at construction ([`LifecycleError::Config`]):
//! misconfiguration is loud at startup, never silently ignored.

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use super::driver::{MAX_HEARTBEAT_INTERVAL, MIN_HEARTBEAT_INTERVAL};
use super::gateway_rules::{
    validate_reconnect_url, HandshakeAfter, HeartbeatAck, ReconnectUrlCapture,
};

/// Most capture rules one transport may declare.
pub const MAX_CAPTURES: usize = 8;
/// Most frame directives one transport may declare.
pub const MAX_DIRECTIVES: usize = 8;
/// Most close-code rules one transport may declare.
pub const MAX_CLOSE_RULES: usize = 16;
/// Most distinct state names (and so values held per connection).
pub const MAX_STATE_VALUES: usize = 8;
/// Longest captured value (bytes).
pub const MAX_CAPTURE_LEN: usize = 128;
/// Most conditions in one JSON match.
pub const MAX_MATCH_CONDITIONS: usize = 4;
/// Longest JSON pointer.
pub const MAX_POINTER_LEN: usize = 128;
/// Longest template (session token / default heartbeat).
pub const MAX_TEMPLATE_LEN: usize = 512;
/// Most HTTP error rules for one poll transport.
pub const MAX_HTTP_RULES: usize = 8;
/// Most reasons one HTTP error rule may list.
pub const MAX_RULE_REASONS: usize = 8;
/// Upper bound of any back-off / interval (24 h).
pub const MAX_BACKOFF: Duration = Duration::from_secs(86_400);

/// Why a rule set or an inbound frame was refused. Reason strings carry no
/// frame content, no captured value and no URL.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum LifecycleError {
    /// The rule configuration is invalid (fail loud at construction).
    #[error("invalid lifecycle rule config: {0}")]
    Config(&'static str),
    /// A capture matched but its value is not an acceptable token.
    #[error("captured value rejected (illegal type, empty, too long or illegal character)")]
    BadCapture,
    /// A heartbeat capture matched but its value is not an in-range interval.
    #[error("captured heartbeat interval rejected (not a number or out of range)")]
    BadHeartbeat,
    /// A reconnect-URL capture matched but the URL failed strict validation
    /// (scheme, userinfo, port, path, charset or host allow-list).
    #[error("captured reconnect url rejected")]
    BadReconnectUrl,
}

/// A scalar a JSON field is compared against.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Scalar {
    /// An integer (e.g. `"op": 7`).
    Int(i64),
    /// A boolean (e.g. `"d": false`).
    Bool(bool),
    /// A string (e.g. `"t": "READY"`).
    Str(String),
}

/// What selects a frame for a rule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FrameMatch {
    /// A JSON frame in which EVERY `(pointer, scalar)` pair holds.
    Json(Vec<(String, Scalar)>),
    /// A line-protocol frame holding a line whose COMMAND token is exactly
    /// this (after optional `@tags ` and `:prefix `), so a chat message that
    /// merely contains the word (`PRIVMSG #c :RECONNECT`) never matches.
    IrcCommand(String),
}

/// What the transport does for a matched frame / classified close code.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LifecycleAction {
    /// End the connection; the driver reconnects under backoff and the
    /// session state is kept (RESUME is possible).
    Reconnect,
    /// As [`Self::Reconnect`] but the session state is dropped first, so the
    /// next handshake starts a fresh session.
    ReconnectFresh,
    /// Terminal failure (`TransportError::Fatal`): the source is parked.
    Park,
}

impl LifecycleAction {
    /// Stable, low-cardinality metric label.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Reconnect => "reconnect",
            Self::ReconnectFresh => "reconnect_fresh",
            Self::Park => "park",
        }
    }
}

/// Captures a named value from a matched frame into per-connection state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureRule {
    /// Which frames the rule reads.
    pub when: FrameMatch,
    /// JSON pointer to the value (string or non-negative integer); an absent
    /// or `null` value is skipped (Discord heartbeat `s: null`).
    pub pointer: String,
    /// State name, `[A-Z][A-Z0-9_]{0,31}`; referenced as `{{NAME}}`.
    pub name: String,
}

/// Captures a heartbeat interval (milliseconds) from a matched frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HeartbeatCapture {
    /// Which frames the rule reads.
    pub when: FrameMatch,
    /// JSON pointer to a non-negative integer number of milliseconds.
    pub pointer: String,
}

/// A matched frame mapped to an action; the frame is consumed (not forwarded).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameDirective {
    /// Which frames the directive fires on.
    pub when: FrameMatch,
    /// What to do.
    pub action: LifecycleAction,
}

/// An inclusive WS close-code range mapped to an action.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CloseCodeRule {
    /// First code (1000..=4999).
    pub from: u16,
    /// Last code (>= `from`).
    pub to: u16,
    /// What to do.
    pub action: LifecycleAction,
}

/// Declarative WS session-lifecycle rules (host config). Empty = none (the
/// default stance, byte-for-byte the pre-lifecycle behaviour).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SessionRules {
    /// Value captures feeding the host session state.
    pub captures: Vec<CaptureRule>,
    /// Dynamic heartbeat-interval capture.
    pub heartbeat: Option<HeartbeatCapture>,
    /// Frame -> action rules (first match wins).
    pub directives: Vec<FrameDirective>,
    /// Close-code -> action rules (ranges may not overlap).
    pub close_codes: Vec<CloseCodeRule>,
    /// Rendered into `ConnectionCtx.session_token` on connect when every
    /// slot has a value (or a default); else the token is `None` (fresh).
    /// Mutually exclusive with a static `WsConfig.resume_token`.
    pub session_token_template: Option<String>,
    /// Host-built heartbeat body, e.g. `{"op":1,"d":{{SEQ|null}}}`; used when
    /// the connector answered `none`, or always with `host_owned_heartbeat`.
    pub default_heartbeat: Option<String>,
    /// Ignore the connector's heartbeat body and always send
    /// `default_heartbeat` (the guest cannot see the live sequence).
    pub host_owned_heartbeat: bool,
    /// Validated capture of the URL the next reconnect must dial (Discord
    /// `resume_gateway_url`); `None` = always dial the configured URL.
    pub reconnect_url: Option<ReconnectUrlCapture>,
    /// Heartbeat-ACK timeout (zombie-connection rule); `None` = off.
    pub heartbeat_ack: Option<HeartbeatAck>,
    /// When the on-connect handshake is sent (default: right after connect).
    pub handshake_after: HandshakeAfter,
    /// Delay the FIRST heartbeat of a connection by a random fraction of the
    /// interval (Discord's HELLO jitter) instead of a full period.
    pub jitter_first_heartbeat: bool,
}

/// What one frame did to the session.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Observation {
    /// A newly captured heartbeat interval.
    pub heartbeat: Option<Duration>,
    /// A directive that fired.
    pub action: Option<LifecycleAction>,
    /// Values stored by captures.
    pub captured: usize,
    /// A new reconnect URL passed validation and was stored.
    pub reconnect_url: bool,
    /// The frame was a heartbeat acknowledgement.
    pub acked: bool,
}

pub(super) fn pointer_ok(p: &str) -> bool {
    p.starts_with('/')
        && p.len() <= MAX_POINTER_LEN
        && p.bytes().all(|b| (0x20..=0x7e).contains(&b))
}

fn name_ok(n: &str) -> bool {
    let mut b = n.bytes();
    n.len() <= 32
        && b.next().is_some_and(|c| c.is_ascii_uppercase())
        && b.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == b'_')
}

/// The strict charset for a captured value and for a template default.
fn value_ok(v: &str) -> bool {
    !v.is_empty()
        && v.len() <= MAX_CAPTURE_LEN
        && v.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:/+=-".contains(&b))
}

pub(super) fn match_ok(m: &FrameMatch) -> bool {
    match m {
        FrameMatch::Json(conds) => {
            !conds.is_empty()
                && conds.len() <= MAX_MATCH_CONDITIONS
                && conds.iter().all(|(p, s)| {
                    pointer_ok(p)
                        && match s {
                            Scalar::Str(v) => {
                                !v.is_empty()
                                    && v.len() <= 64
                                    && v.bytes().all(|b| (0x20..=0x7e).contains(&b))
                            }
                            _ => true,
                        }
                })
        }
        FrameMatch::IrcCommand(c) => {
            !c.is_empty() && c.len() <= 32 && c.bytes().all(|b| b.is_ascii_alphanumeric())
        }
    }
}

/// One parsed template segment.
enum Seg<'a> {
    Lit(&'a [u8]),
    Slot {
        name: &'a str,
        default: Option<&'a str>,
    },
}

/// Splits `tpl` into literals and `{{NAME}}` / `{{NAME|default}}` slots. Only
/// well-formed names are slots; anything else is literal text (so a secret
/// slot such as `{{DISCORD_BOT_TOKEN}}` is a slot syntactically and is left to
/// the caller to match or ignore by name).
fn segments(tpl: &[u8]) -> Vec<Seg<'_>> {
    let mut out = Vec::new();
    let (mut lit_start, mut i) = (0usize, 0usize);
    while i < tpl.len() {
        if tpl[i..].starts_with(b"{{") {
            if let Some(end) = tpl[i + 2..].windows(2).position(|w| w == b"}}") {
                let inner = &tpl[i + 2..i + 2 + end];
                if let Ok(s) = std::str::from_utf8(inner) {
                    let (name, default) = match s.split_once('|') {
                        Some((n, d)) => (n, Some(d)),
                        None => (s, None),
                    };
                    if name_ok(name) {
                        if lit_start < i {
                            out.push(Seg::Lit(&tpl[lit_start..i]));
                        }
                        out.push(Seg::Slot { name, default });
                        i += 2 + end + 2;
                        lit_start = i;
                        continue;
                    }
                }
            }
        }
        i += 1;
    }
    if lit_start < tpl.len() {
        out.push(Seg::Lit(&tpl[lit_start..]));
    }
    out
}

fn template_ok(tpl: &str, declared: &HashSet<&str>) -> bool {
    if tpl.is_empty()
        || tpl.len() > MAX_TEMPLATE_LEN
        || !tpl.bytes().all(|b| (0x20..=0x7e).contains(&b))
    {
        return false;
    }
    segments(tpl.as_bytes()).iter().all(|s| match s {
        Seg::Lit(_) => true,
        Seg::Slot { name, default } => declared.contains(name) && default.is_none_or(value_ok),
    })
}

fn pointer_value<'v>(v: &'v serde_json::Value, p: &str) -> Option<&'v serde_json::Value> {
    v.pointer(p)
}

fn json_matches(v: &serde_json::Value, conds: &[(String, Scalar)]) -> bool {
    conds.iter().all(|(p, s)| match (pointer_value(v, p), s) {
        (Some(x), Scalar::Int(i)) => x.as_i64() == Some(*i),
        (Some(x), Scalar::Bool(b)) => x.as_bool() == Some(*b),
        (Some(x), Scalar::Str(t)) => x.as_str() == Some(t.as_str()),
        (None, _) => false,
    })
}

fn irc_matches(text: &str, cmd: &str) -> bool {
    text.split_inclusive('\n').any(|line| {
        let mut rest = line.trim_end_matches(['\r', '\n']);
        if rest.starts_with('@') {
            match rest.split_once(' ') {
                Some((_, r)) => rest = r,
                None => return false,
            }
        }
        if rest.starts_with(':') {
            match rest.split_once(' ') {
                Some((_, r)) => rest = r,
                None => return false,
            }
        }
        rest.split(' ').next() == Some(cmd)
    })
}

/// The per-transport session engine: validated rules plus the bounded
/// per-connection state they feed. Never logs or exposes a value.
#[derive(Clone, Default)]
pub struct SessionEngine {
    rules: SessionRules,
    declared: Vec<String>,
    state: HashMap<String, String>,
    needs_json: bool,
    /// Validated reconnect URL (never a captured-state value: it has its own
    /// stricter validation and is dropped with the session).
    reconnect_url: Option<String>,
    /// Heartbeats sent on this connection and not yet acknowledged.
    unacked: u8,
    allow_loopback_insecure: bool,
}

impl std::fmt::Debug for SessionEngine {
    /// Never prints captured values (session ids are connection secrets).
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SessionEngine")
            .field("rules", &self.rules)
            .field("held", &self.state.len())
            .field("has_reconnect_url", &self.reconnect_url.is_some())
            .finish()
    }
}

impl SessionEngine {
    /// Validates the rules; any malformed rule is refused (fail loud).
    pub fn new(rules: SessionRules) -> Result<Self, LifecycleError> {
        Self::new_with(rules, false)
    }

    /// As [`Self::new`]; `allow_loopback_insecure` (tests only, mirrors
    /// `WsConfig.allow_loopback_insecure`) lets a captured reconnect URL be a
    /// loopback `ws://` address.
    pub fn new_with(
        rules: SessionRules,
        allow_loopback_insecure: bool,
    ) -> Result<Self, LifecycleError> {
        if let Some(r) = &rules.reconnect_url {
            r.validate()?;
        }
        if let Some(a) = &rules.heartbeat_ack {
            a.validate()?;
        }
        rules.handshake_after.validate()?;
        if rules.captures.len() > MAX_CAPTURES
            || rules.directives.len() > MAX_DIRECTIVES
            || rules.close_codes.len() > MAX_CLOSE_RULES
        {
            return Err(LifecycleError::Config("too many session rules"));
        }
        let mut names: Vec<String> = Vec::new();
        for c in &rules.captures {
            if !match_ok(&c.when) || !pointer_ok(&c.pointer) || !name_ok(&c.name) {
                return Err(LifecycleError::Config("invalid capture rule"));
            }
            if !names.contains(&c.name) {
                names.push(c.name.clone());
            }
        }
        if names.len() > MAX_STATE_VALUES {
            return Err(LifecycleError::Config("too many distinct state names"));
        }
        if let Some(h) = &rules.heartbeat {
            if !match_ok(&h.when) || !pointer_ok(&h.pointer) {
                return Err(LifecycleError::Config("invalid heartbeat capture"));
            }
        }
        if rules.directives.iter().any(|d| !match_ok(&d.when)) {
            return Err(LifecycleError::Config("invalid frame directive"));
        }
        for (i, a) in rules.close_codes.iter().enumerate() {
            if !(1000..=4999).contains(&a.from) || a.to < a.from || a.to > 4999 {
                return Err(LifecycleError::Config("close-code range invalid"));
            }
            if rules.close_codes[i + 1..]
                .iter()
                .any(|b| a.from <= b.to && b.from <= a.to)
            {
                return Err(LifecycleError::Config("close-code ranges overlap"));
            }
        }
        let declared: HashSet<&str> = names.iter().map(String::as_str).collect();
        for t in [&rules.session_token_template, &rules.default_heartbeat]
            .into_iter()
            .flatten()
        {
            if !template_ok(t, &declared) {
                return Err(LifecycleError::Config(
                    "template invalid (undeclared slot, bad default, charset or length)",
                ));
            }
        }
        if rules.host_owned_heartbeat && rules.default_heartbeat.is_none() {
            return Err(LifecycleError::Config(
                "host_owned_heartbeat requires default_heartbeat",
            ));
        }
        let needs_json = rules
            .captures
            .iter()
            .map(|c| &c.when)
            .chain(rules.heartbeat.iter().map(|h| &h.when))
            .chain(rules.directives.iter().map(|d| &d.when))
            .chain(rules.reconnect_url.iter().map(|r| &r.when))
            .chain(rules.heartbeat_ack.iter().map(|a| &a.when))
            .any(|m| matches!(m, FrameMatch::Json(_)));
        Ok(Self {
            rules,
            declared: names,
            state: HashMap::new(),
            needs_json,
            reconnect_url: None,
            unacked: 0,
            allow_loopback_insecure,
        })
    }

    /// True when the session-token template is configured.
    #[must_use]
    pub fn has_token_template(&self) -> bool {
        self.rules.session_token_template.is_some()
    }

    /// True when no rule at all is configured (frame path untouched).
    #[must_use]
    pub fn is_inert(&self) -> bool {
        self.rules.captures.is_empty()
            && self.rules.heartbeat.is_none()
            && self.rules.directives.is_empty()
            && self.rules.close_codes.is_empty()
            && self.rules.default_heartbeat.is_none()
            && self.rules.session_token_template.is_none()
            && self.rules.reconnect_url.is_none()
            && self.rules.heartbeat_ack.is_none()
    }

    /// Drops every captured value (identify-fresh).
    pub fn clear(&mut self) {
        self.state.clear();
        self.reconnect_url = None;
    }

    /// Per-connection reset: nothing is unacknowledged on a new socket.
    pub fn begin_connection(&mut self) {
        self.unacked = 0;
    }

    /// The validated URL the next connect must dial, when a session exists
    /// to resume (with a token template, only if it renders). `None` = dial
    /// the configured URL (a fresh identify).
    #[must_use]
    pub fn reconnect_target(&self) -> Option<&str> {
        let url = self.reconnect_url.as_deref()?;
        if self.has_token_template() && self.render_session_token().is_none() {
            return None;
        }
        Some(url)
    }

    /// Notes that a heartbeat is due. `false` = the connection is dead: the
    /// previous heartbeat(s) were never acknowledged (nothing is sent).
    /// Always `true` when no `heartbeat_ack` rule is configured.
    pub fn heartbeat_due(&mut self) -> bool {
        let Some(rule) = &self.rules.heartbeat_ack else {
            return true;
        };
        if self.unacked >= rule.max_unacked {
            return false;
        }
        self.unacked += 1;
        true
    }

    /// Number of values currently held (never the values).
    #[must_use]
    pub fn held(&self) -> usize {
        self.state.len()
    }

    /// Whether the connector's heartbeat body is replaced by the host's.
    #[must_use]
    pub fn host_owned_heartbeat(&self) -> bool {
        self.rules.host_owned_heartbeat
    }

    /// Classifies a WS close code.
    #[must_use]
    pub fn classify_close(&self, code: u16) -> Option<LifecycleAction> {
        self.rules
            .close_codes
            .iter()
            .find(|r| (r.from..=r.to).contains(&code))
            .map(|r| r.action)
    }

    /// Runs one inbound frame through captures, the heartbeat capture and
    /// the directives. A matched-but-illegal capture is an error (the frame is
    /// refused, nothing stored from it).
    pub fn observe(&mut self, frame: &[u8]) -> Result<Observation, LifecycleError> {
        let mut obs = Observation::default();
        if self.is_inert() {
            return Ok(obs);
        }
        let Ok(text) = std::str::from_utf8(frame) else {
            return Ok(obs);
        };
        let json: Option<serde_json::Value> =
            if self.needs_json && text.trim_start().starts_with('{') {
                serde_json::from_str(text).ok()
            } else {
                None
            };
        let matches = |m: &FrameMatch| match m {
            FrameMatch::Json(c) => json.as_ref().is_some_and(|v| json_matches(v, c)),
            FrameMatch::IrcCommand(c) => irc_matches(text, c),
        };
        // Stage then commit: an illegal value fails the whole frame with
        // nothing stored from it.
        let mut staged: Vec<(String, String)> = Vec::new();
        for c in &self.rules.captures {
            if !matches(&c.when) {
                continue;
            }
            let Some(v) = json.as_ref().and_then(|j| j.pointer(&c.pointer)) else {
                continue;
            };
            let val = match v {
                serde_json::Value::Null => continue,
                serde_json::Value::String(s) => s.clone(),
                serde_json::Value::Number(n) => n
                    .as_u64()
                    .map(|u| u.to_string())
                    .ok_or(LifecycleError::BadCapture)?,
                _ => return Err(LifecycleError::BadCapture),
            };
            if !value_ok(&val) {
                return Err(LifecycleError::BadCapture);
            }
            staged.push((c.name.clone(), val));
        }
        if let Some(h) = &self.rules.heartbeat {
            if matches(&h.when) {
                if let Some(v) = json.as_ref().and_then(|j| j.pointer(&h.pointer)) {
                    let ms = v.as_u64().ok_or(LifecycleError::BadHeartbeat)?;
                    let d = Duration::from_millis(ms);
                    if !(MIN_HEARTBEAT_INTERVAL..=MAX_HEARTBEAT_INTERVAL).contains(&d) {
                        return Err(LifecycleError::BadHeartbeat);
                    }
                    obs.heartbeat = Some(d);
                }
            }
        }
        let mut staged_url: Option<String> = None;
        if let Some(r) = &self.rules.reconnect_url {
            if matches(&r.when) {
                match json.as_ref().and_then(|j| j.pointer(&r.pointer)) {
                    None | Some(serde_json::Value::Null) => {}
                    Some(serde_json::Value::String(raw)) => {
                        staged_url = Some(validate_reconnect_url(
                            raw,
                            r,
                            self.allow_loopback_insecure,
                        )?);
                    }
                    Some(_) => return Err(LifecycleError::BadReconnectUrl),
                }
            }
        }
        if let Some(a) = &self.rules.heartbeat_ack {
            if matches(&a.when) {
                obs.acked = true;
            }
        }
        obs.captured = staged.len();
        for (k, v) in staged {
            self.state.insert(k, v);
        }
        if let Some(u) = staged_url {
            self.reconnect_url = Some(u);
            obs.reconnect_url = true;
        }
        if obs.acked {
            self.unacked = 0;
        }
        obs.action = self
            .rules
            .directives
            .iter()
            .find(|d| matches(&d.when))
            .map(|d| d.action);
        Ok(obs)
    }

    fn render_bytes(&self, tpl: &[u8]) -> Option<Vec<u8>> {
        let mut out = Vec::with_capacity(tpl.len() + 32);
        for seg in segments(tpl) {
            match seg {
                Seg::Lit(l) => out.extend_from_slice(l),
                Seg::Slot { name, default } => {
                    if !self.declared.iter().any(|d| d == name) {
                        // Not ours (e.g. a secret slot): leave verbatim.
                        out.extend_from_slice(b"{{");
                        out.extend_from_slice(name.as_bytes());
                        if let Some(d) = default {
                            out.push(b'|');
                            out.extend_from_slice(d.as_bytes());
                        }
                        out.extend_from_slice(b"}}");
                        continue;
                    }
                    match (self.state.get(name), default) {
                        (Some(v), _) => out.extend_from_slice(v.as_bytes()),
                        (None, Some(d)) if value_ok(d) => out.extend_from_slice(d.as_bytes()),
                        _ => return None,
                    }
                }
            }
        }
        Some(out)
    }

    /// Substitutes declared state slots in a connector-authored handshake.
    /// `None` = a declared slot has neither a value nor a default (the
    /// caller fails the handshake loudly). Slots that are not declared state
    /// names (the secret slots) are left untouched for the secret path.
    #[must_use]
    pub fn render_handshake(&self, bytes: &[u8]) -> Option<Vec<u8>> {
        self.render_bytes(bytes)
    }

    /// Renders the configured session token; `None` when not configured or a
    /// slot is unavailable (the connector then starts a fresh session).
    #[must_use]
    pub fn render_session_token(&self) -> Option<String> {
        let t = self.rules.session_token_template.as_deref()?;
        String::from_utf8(self.render_bytes(t.as_bytes())?).ok()
    }

    /// Renders the host default heartbeat body.
    #[must_use]
    pub fn render_default_heartbeat(&self) -> Option<Vec<u8>> {
        self.render_bytes(self.rules.default_heartbeat.as_deref()?.as_bytes())
    }
}

/// What a classified HTTP error response does.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HttpAction {
    /// Pause polling: `base`, doubled per consecutive hit, capped at `max`;
    /// a `Retry-After` header (seconds) replaces the computed wait when
    /// `honor_retry_after`, still capped at `max`.
    Backoff {
        /// First wait.
        base: Duration,
        /// Hard ceiling (<= 24 h).
        max: Duration,
        /// Honour a numeric `Retry-After`.
        honor_retry_after: bool,
    },
    /// Terminal: the source parks (`TransportError::Fatal`).
    Park,
}

/// Classifies an HTTP error status by a `reason` in the JSON body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpErrorRule {
    /// Status code the rule applies to (400..=599).
    pub status: u16,
    /// JSON pointer to the reason string in the body.
    pub reason_pointer: String,
    /// Reason values selecting the rule (charset `[A-Za-z0-9_.-]`, <= 64).
    pub reasons: Vec<String>,
    /// What to do.
    pub action: HttpAction,
}

/// Interval taken from a response, clamped to `[min, max]`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IntervalHint {
    /// JSON pointer to a non-negative integer number of milliseconds.
    pub pointer: String,
    /// Floor.
    pub min: Duration,
    /// Ceiling (<= 24 h).
    pub max: Duration,
}

/// A host-substituted secret carried in the URL query.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuerySecret {
    /// Query parameter name (`[A-Za-z0-9_.-]`, <= 32).
    pub param: String,
    /// Handle into the transport's `handles` (the manifest's secret_ref).
    pub handle: String,
}

/// Declarative poll lifecycle (host config). Default = none.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PollLifecycle {
    /// Next-interval hint from the response.
    pub interval_hint: Option<IntervalHint>,
    /// HTTP error classification (first match wins).
    pub error_rules: Vec<HttpErrorRule>,
    /// API-key-in-query slot.
    pub query_secret: Option<QuerySecret>,
}

fn reason_ok(r: &str) -> bool {
    !r.is_empty()
        && r.len() <= 64
        && r.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
}

impl PollLifecycle {
    /// Validates the config (fail loud). `handles`/`cursor_param` are the
    /// transport's, for the cross-checks the query secret needs.
    pub fn validate(
        &self,
        handles: &HashMap<String, connection_resolver::ConnectionRef>,
        cursor_param: Option<&str>,
        url: &reqwest::Url,
    ) -> Result<(), LifecycleError> {
        if let Some(h) = &self.interval_hint {
            if !pointer_ok(&h.pointer)
                || h.min < Duration::from_millis(1)
                || h.max < h.min
                || h.max > MAX_BACKOFF
            {
                return Err(LifecycleError::Config("invalid interval hint"));
            }
        }
        if self.error_rules.len() > MAX_HTTP_RULES {
            return Err(LifecycleError::Config("too many http error rules"));
        }
        for r in &self.error_rules {
            let bad_action = match &r.action {
                HttpAction::Backoff { base, max, .. } => {
                    *base < Duration::from_millis(1) || max < base || *max > MAX_BACKOFF
                }
                HttpAction::Park => false,
            };
            if !(400..=599).contains(&r.status)
                || !pointer_ok(&r.reason_pointer)
                || r.reasons.is_empty()
                || r.reasons.len() > MAX_RULE_REASONS
                || !r.reasons.iter().all(|x| reason_ok(x))
                || bad_action
            {
                return Err(LifecycleError::Config("invalid http error rule"));
            }
        }
        for (i, a) in self.error_rules.iter().enumerate() {
            for b in &self.error_rules[i + 1..] {
                if a.status == b.status
                    && a.reason_pointer == b.reason_pointer
                    && a.reasons.iter().any(|x| b.reasons.contains(x))
                {
                    return Err(LifecycleError::Config("http error rules overlap"));
                }
            }
        }
        if let Some(q) = &self.query_secret {
            let param_ok = !q.param.is_empty()
                && q.param.len() <= 32
                && q.param
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b));
            if !param_ok
                || !handles.contains_key(&q.handle)
                || cursor_param == Some(q.param.as_str())
                || url.query_pairs().any(|(k, _)| k == q.param.as_str())
            {
                return Err(LifecycleError::Config("invalid query secret slot"));
            }
        }
        Ok(())
    }

    /// Classifies an error response: `(rule index)` of the first rule whose
    /// status and body reason match.
    #[must_use]
    pub fn classify(&self, status: u16, body: &[u8]) -> Option<&HttpErrorRule> {
        let rules: Vec<&HttpErrorRule> = self
            .error_rules
            .iter()
            .filter(|r| r.status == status)
            .collect();
        if rules.is_empty() {
            return None;
        }
        let v: serde_json::Value = serde_json::from_slice(body).ok()?;
        rules.into_iter().find(|r| {
            v.pointer(&r.reason_pointer)
                .and_then(serde_json::Value::as_str)
                .is_some_and(|s| r.reasons.iter().any(|x| x == s))
        })
    }

    /// The next poll interval from a response body, clamped; the second item
    /// says whether clamping changed it. `None` = no usable hint.
    #[must_use]
    pub fn next_interval(&self, body: &[u8]) -> Option<(Duration, bool)> {
        let h = self.interval_hint.as_ref()?;
        let v: serde_json::Value = serde_json::from_slice(body).ok()?;
        let ms = match v.pointer(&h.pointer)? {
            serde_json::Value::Number(n) => n.as_u64()?,
            serde_json::Value::String(s) => s.parse::<u64>().ok()?,
            _ => return None,
        };
        let want = Duration::from_millis(ms);
        let got = want.clamp(h.min, h.max);
        Some((got, got != want))
    }
}

/// Back-off wait for the `streak`-th consecutive hit of a backoff rule.
#[must_use]
pub fn backoff_wait(
    base: Duration,
    max: Duration,
    streak: u32,
    retry_after: Option<Duration>,
) -> Duration {
    let computed = base.saturating_mul(1u32 << streak.min(20));
    retry_after.unwrap_or(computed).min(max)
}
