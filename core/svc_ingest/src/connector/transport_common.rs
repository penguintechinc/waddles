//! Shared helpers for the generic WebSocket and poll transports: endpoint
//! validation, per-call secret resolution and handshake-slot substitution.
//! Everything platform-specific (URL, auth slots, cursor shape) arrives via
//! the overlay's config; nothing here names a platform.

use std::collections::HashMap;

use connection_resolver::{ConnectionRef, SecretString};
use sha2::{Digest, Sha256};
use zeroize::Zeroizing;

use super::secrets::{Destination, ResolvedAuth, TransportSecretSource};
use super::transport::TransportError;
use super::webhook::SecretError;
use super::webhook_verify::hex_encode;

/// Test-only: serializes the process-wide proxy-env mutation in poll's pin test
/// against tests whose HTTP clients read that env (reader = client user).
#[cfg(test)]
pub(crate) static PROXY_ENV: tokio::sync::RwLock<()> = tokio::sync::RwLock::const_new(());

/// Validates a transport endpoint: `https`/`wss` only (plain `http`/`ws` is
/// accepted solely for a loopback host and only when `allow_loopback_insecure`
/// is set -- test harnesses), no embedded userinfo, a host is present.
pub(crate) fn validate_endpoint(
    raw: &str,
    secure_schemes: &[&str],
    insecure_scheme: &str,
    allow_loopback_insecure: bool,
) -> Result<reqwest::Url, String> {
    let url = reqwest::Url::parse(raw).map_err(|_| "endpoint is not a valid URL".to_string())?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err("endpoint must not embed credentials".into());
    }
    let host = url.host_str().ok_or("endpoint has no host")?;
    let scheme = url.scheme();
    if secure_schemes.contains(&scheme) {
        return Ok(url);
    }
    let loopback = matches!(host, "127.0.0.1" | "localhost" | "[::1]");
    if scheme == insecure_scheme && allow_loopback_insecure && loopback {
        return Ok(url);
    }
    Err(format!("endpoint scheme `{scheme}` is not permitted"))
}

/// Opaque `source_id` for a transport instance: a hash of the host-side
/// source key with a family prefix, never the URL, channel or any name.
pub(crate) fn opaque_id(prefix: &str, source_key: &str) -> String {
    let d = Sha256::digest(source_key.as_bytes());
    format!("{prefix}-{}", &hex_encode(&d)[..16])
}

/// Resolves one handle through the secret source, fresh on every call (the
/// source MUST NOT cache; this layer never stores the value).
pub(crate) async fn resolve_handle<S: TransportSecretSource>(
    secrets: &S,
    handles: &HashMap<String, ConnectionRef>,
    handle: &str,
    dest: &Destination,
) -> Result<ResolvedAuth, TransportError> {
    let r = handles
        .get(handle)
        .copied()
        .ok_or_else(|| TransportError::Handshake("unknown secret handle".into()))?;
    secrets.resolve(r, dest.clone()).await.map_err(|e| match e {
        SecretError::Missing => TransportError::Fatal("secret unavailable for handle".into()),
        SecretError::Unavailable => TransportError::Handshake("secret backend unavailable".into()),
    })
}

/// A secret is spliced into a text frame verbatim, so anything that could
/// break out of a quoted field or inject framing is refused (fail closed).
fn splice_safe(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| !c.is_control() && c != '"' && c != '\\')
}

/// Reason text of the typed refusal for a secret that could inject structure
/// (CR/LF/NUL/control, `"` or `\`); transports key their metric on it.
pub(crate) const SECRET_NOT_SPLICE_SAFE: &str = "secret not splice-safe";

/// Max `(slot, handle)` pairs per handshake.
pub(crate) const MAX_SECRET_REFS: usize = 8;

/// One uniform failure for every slot/handle problem (unknown handle, absent
/// slot, bad slot shape): the guest-controlled inputs get no oracle.
fn uniform() -> TransportError {
    TransportError::Handshake("handshake secret substitution failed".into())
}

/// Host-pinned credential-slot prefixes, keyed by secret handle: each handle
/// declares the exact byte prefix(es) its slot may directly follow (e.g.
/// `pass` -> `PASS oauth:`). A handle with no entry may not be substituted at
/// all, and one handle's prefix never authorizes another handle's slot.
pub type SlotPrefixes = HashMap<String, Vec<String>>;

/// Why a slot/handle pairing was refused (host-side diagnostics and metrics
/// only; the guest-visible error stays uniform).
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum SlotRefusal {
    /// The handle has no declared prefix at all (fail closed).
    #[error("handle has no declared slot prefix")]
    HandleNotDeclared,
    /// An occurrence of the slot is not directly after one of ITS handle's
    /// prefixes and before a terminator.
    #[error("slot placement does not match the handle's declared prefix")]
    PlacementRefused,
}

impl SlotRefusal {
    /// Stable, low-cardinality metric outcome label.
    #[must_use]
    pub fn outcome(self) -> &'static str {
        match self {
            Self::HandleNotDeclared => "refused_undeclared",
            Self::PlacementRefused => "refused_placement",
        }
    }
}

/// The guest-visible error for a refused slot pairing (identical to every
/// other substitution failure: no oracle).
pub(crate) fn slot_refused_error() -> TransportError {
    uniform()
}

/// True when `prefix` ends exactly at `slot_at`, STARTS at offset 0 or directly
/// after CR/LF, and the bytes in between equal it exactly. A mere suffix match
/// is not enough: `PRIVMSG #c :PASS oauth:{{S}}` ends with the prefix but would
/// publish the credential into chat.
fn anchored_prefix(bytes: &[u8], slot_at: usize, prefix: &str) -> bool {
    let p = prefix.as_bytes();
    if p.is_empty() {
        return false;
    }
    slot_at.checked_sub(p.len()).is_some_and(|start| {
        let at_boundary = start == 0 || matches!(bytes[start - 1], b'\n' | b'\r');
        at_boundary && &bytes[start..slot_at] == p
    })
}

/// True when EVERY occurrence of `slot` in `bytes` sits directly after one of
/// the handle's own host-pinned `prefixes` (anchored at frame/line start, see
/// [`anchored_prefix`]) and directly before a terminator
/// (end, `"`, CR or LF). The connector authors the template, so without this a
/// hostile one could place the credential in a message body or target (e.g.
/// `PRIVMSG #x :{{AUTH}}`) and publish it. No prefixes = nothing substituted.
fn placement_ok(bytes: &[u8], slot: &[u8], prefixes: &[String]) -> bool {
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i..].starts_with(slot) {
            let after = bytes.get(i + slot.len()).copied();
            let term_ok = matches!(after, None | Some(b'"' | b'\r' | b'\n'));
            let pre_ok = prefixes.iter().any(|p| anchored_prefix(bytes, i, p));
            if !(term_ok && pre_ok) {
                return false;
            }
            i += slot.len();
        } else {
            i += 1;
        }
    }
    true
}

/// Checks one `slot`/`handle` pairing against the handle's own declared
/// `prefixes`; `Some` is the refusal reason.
fn pairing_refusal(
    bytes: &[u8],
    slot: &str,
    handle: &str,
    prefixes: &SlotPrefixes,
) -> Option<SlotRefusal> {
    let declared = prefixes
        .get(handle)
        .filter(|v| v.iter().any(|p| !p.is_empty()));
    let Some(declared) = declared else {
        return Some(SlotRefusal::HandleNotDeclared);
    };
    if placement_ok(bytes, slot.as_bytes(), declared) {
        None
    } else {
        Some(SlotRefusal::PlacementRefused)
    }
}

/// Checks every `(slot, handle)` pairing; the first violation is returned.
/// [`substitute_slots`] enforces the same check; transports call this first
/// to record the typed reason (log + metric) before failing uniformly.
pub(crate) fn slot_refusal(
    bytes: &[u8],
    refs: &[(String, String)],
    prefixes: &SlotPrefixes,
) -> Option<SlotRefusal> {
    refs.iter()
        .find_map(|(slot, handle)| pairing_refusal(bytes, slot, handle, prefixes))
}

/// Substitutes every `(slot, handle)` pair into `bytes` in ONE pass over the
/// IMMUTABLE original (already-substituted text is never re-scanned). `slot`
/// is the literal placeholder the connector put in the handshake; each must
/// occur at least once, none may be empty, duplicated, or a substring of
/// another slot. Output is a pre-sized zeroizing buffer (no realloc copies);
/// resolved secrets zeroize on drop inside this call. Every slot occurrence must
/// satisfy its OWN handle's host-owned `prefixes` (fail closed).
pub(crate) async fn substitute_slots<S: TransportSecretSource>(
    secrets: &S,
    handles: &HashMap<String, ConnectionRef>,
    bytes: &[u8],
    refs: &[(String, String)],
    prefixes: &SlotPrefixes,
    dest: &Destination,
) -> Result<Zeroizing<Vec<u8>>, TransportError> {
    if refs.len() > MAX_SECRET_REFS {
        return Err(uniform());
    }
    for (i, (slot, handle)) in refs.iter().enumerate() {
        let bad_shape = slot.is_empty()
            || refs.iter().enumerate().any(|(j, (o, _))| {
                j != i && (o == slot || o.contains(slot.as_str()) || slot.contains(o.as_str()))
            });
        if bad_shape
            || !handles.contains_key(handle)
            || count_matches(bytes, slot.as_bytes()) == 0
            || pairing_refusal(bytes, slot, handle, prefixes).is_some()
        {
            return Err(uniform());
        }
    }
    let mut resolved: Vec<(&[u8], SecretString)> = Vec::with_capacity(refs.len());
    let mut extra = 0usize;
    for (slot, handle) in refs {
        let secret = resolve_handle(secrets, handles, handle, dest).await?.secret;
        if !splice_safe(secret.expose()) {
            return Err(TransportError::Fatal(SECRET_NOT_SPLICE_SAFE.into()));
        }
        extra = extra.saturating_add(
            count_matches(bytes, slot.as_bytes()).saturating_mul(secret.expose().len()),
        );
        resolved.push((slot.as_bytes(), secret));
    }
    let mut out = Zeroizing::new(Vec::with_capacity(bytes.len().saturating_add(extra)));
    let mut i = 0;
    'scan: while i < bytes.len() {
        for (slot, secret) in &resolved {
            if bytes[i..].starts_with(slot) {
                out.extend_from_slice(secret.expose().as_bytes());
                i += slot.len();
                continue 'scan;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    Ok(out)
}

/// Non-overlapping occurrences of `needle` in `hay`.
fn count_matches(hay: &[u8], needle: &[u8]) -> usize {
    let (mut i, mut n) = (0, 0);
    while i < hay.len() {
        if hay[i..].starts_with(needle) {
            n += 1;
            i += needle.len();
        } else {
            i += 1;
        }
    }
    n
}

/// Most auto-reply rules one transport may declare.
pub const MAX_AUTO_REPLY_RULES: usize = 8;
/// Most host-built replies one inbound frame may trigger (flood bound).
pub const MAX_REPLIES_PER_FRAME: usize = 8;
/// Hard ceiling on an echoed token's length, whatever the rule asks for.
pub const MAX_REPLY_TOKEN_LEN: usize = 256;

/// One declarative, host-owned auto-reply rule: an inbound line that starts
/// with `prefix` is answered with `reply_prefix + <echoed token> +
/// reply_suffix`, built by the HOST. Nothing platform-named lives in the
/// transport: an IRC overlay configures `PING :` -> `PONG :`, another
/// protocol configures its own pair. The only inbound bytes ever reflected
/// are the token after `prefix`, validated against a strict charset
/// (`[A-Za-z0-9.:_/+=@-]`) and `max_token_len`; no guest byte is ever
/// reflected (CR/LF injection is impossible by construction).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutoReplyRule {
    /// Printable-ASCII line prefix that selects the rule (e.g. `PING :`).
    pub prefix: String,
    /// Printable-ASCII reply prefix (e.g. `PONG :`).
    pub reply_prefix: String,
    /// Reply terminator: exactly `""`, `"\r\n"` or `"\n"`.
    pub reply_suffix: String,
    /// Longest accepted token, `1..=`[`MAX_REPLY_TOKEN_LEN`].
    pub max_token_len: usize,
    /// When true the matched line is ALSO delivered to the connector; the
    /// default (false) consumes it so the overlay never sees keepalives.
    pub forward_to_guest: bool,
}

/// Host-side drop of the connection's own echoed lines (self-echo), so a
/// connector without a self identity (the frozen `connector` WIT gives it
/// none) never normalizes the bot's own messages. The line grammar is
/// declarative: after optional `@tags `, the line is
/// `<sender_open><id><sender_close><user@host> <command> ...`; a line whose
/// `<id>` equals `self_id` (ASCII case-insensitive) and whose command is
/// `command` is dropped. `self_id` is the bot's public login, host-config
/// supplied -- never a secret, never guest-controlled.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SelfEchoFilter {
    /// The bot's own sender id/login.
    pub self_id: String,
    /// Token that opens the sender id (e.g. `:`).
    pub sender_open: String,
    /// Token that closes the sender id (e.g. `!`).
    pub sender_close: String,
    /// Command whose own-sender lines are dropped (e.g. `PRIVMSG`).
    pub command: String,
}

/// Why a frame-rule set or an inbound frame was refused. Reason strings
/// carry no frame content.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum FrameRuleError {
    /// The rule configuration is invalid (fail loud at construction).
    #[error("invalid frame rule config: {0}")]
    Config(&'static str),
    /// An inbound line matched a rule but its token is not echoable.
    #[error("auto-reply token rejected (empty, too long or illegal character)")]
    BadToken,
    /// One frame would trigger more than [`MAX_REPLIES_PER_FRAME`] replies.
    #[error("auto-reply flood: too many reply lines in one frame")]
    TooManyReplies,
}

/// The outcome of running one inbound frame through [`FrameRules::apply`].
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Applied {
    /// Bytes to hand the connector; `None` when nothing is left.
    pub forward: Option<Vec<u8>>,
    /// Host-built reply frames to send back upstream, in order.
    pub replies: Vec<Vec<u8>>,
    /// Self-echo lines dropped.
    pub echo_dropped: usize,
}

/// A validated set of auto-reply rules and an optional self-echo filter.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FrameRules {
    auto_replies: Vec<AutoReplyRule>,
    self_echo: Option<SelfEchoFilter>,
}

fn printable(s: &str, max: usize) -> bool {
    !s.is_empty() && s.len() <= max && s.bytes().all(|b| (0x20..=0x7e).contains(&b))
}

fn graphic(s: &str, max: usize) -> bool {
    !s.is_empty() && s.len() <= max && s.bytes().all(|b| (0x21..=0x7e).contains(&b))
}

fn token_ok(t: &str, max: usize) -> bool {
    !t.is_empty()
        && t.len() <= max
        && t.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".:_/+=@-".contains(&b))
}

impl FrameRules {
    /// Validates and builds the rule set; any malformed rule is refused
    /// (fail loud), and overlapping prefixes are refused as ambiguous.
    pub fn new(
        auto_replies: Vec<AutoReplyRule>,
        self_echo: Option<SelfEchoFilter>,
    ) -> Result<Self, FrameRuleError> {
        if auto_replies.len() > MAX_AUTO_REPLY_RULES {
            return Err(FrameRuleError::Config("too many auto-reply rules"));
        }
        for r in &auto_replies {
            if !printable(&r.prefix, 32) || !printable(&r.reply_prefix, 32) {
                return Err(FrameRuleError::Config(
                    "auto-reply prefix must be 1..=32 printable ASCII",
                ));
            }
            if !matches!(r.reply_suffix.as_str(), "" | "\r\n" | "\n") {
                return Err(FrameRuleError::Config(
                    "auto-reply suffix must be empty, CRLF or LF",
                ));
            }
            if r.max_token_len == 0 || r.max_token_len > MAX_REPLY_TOKEN_LEN {
                return Err(FrameRuleError::Config(
                    "auto-reply max_token_len out of range",
                ));
            }
        }
        for (i, a) in auto_replies.iter().enumerate() {
            for b in &auto_replies[i + 1..] {
                if a.prefix.starts_with(&b.prefix) || b.prefix.starts_with(&a.prefix) {
                    return Err(FrameRuleError::Config("auto-reply prefixes overlap"));
                }
            }
        }
        if let Some(f) = &self_echo {
            if !graphic(&f.self_id, 128)
                || !graphic(&f.sender_open, 4)
                || !graphic(&f.sender_close, 4)
                || f.command.is_empty()
                || f.command.len() > 32
                || !f.command.bytes().all(|b| b.is_ascii_alphanumeric())
            {
                return Err(FrameRuleError::Config("invalid self-echo filter"));
            }
        }
        Ok(Self {
            auto_replies,
            self_echo,
        })
    }

    /// True when no rule is configured (the frame path is then untouched).
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.auto_replies.is_empty() && self.self_echo.is_none()
    }

    fn echo_matches(&self, line: &str) -> bool {
        let Some(f) = &self.self_echo else {
            return false;
        };
        let rest = match line.strip_prefix('@') {
            Some(tagged) => match tagged.split_once(' ') {
                Some((_, r)) => r,
                None => return false,
            },
            None => line,
        };
        let Some(rest) = rest.strip_prefix(f.sender_open.as_str()) else {
            return false;
        };
        let Some((id, after)) = rest.split_once(f.sender_close.as_str()) else {
            return false;
        };
        if !id.eq_ignore_ascii_case(&f.self_id) {
            return false;
        }
        let Some((_, tail)) = after.split_once(' ') else {
            return false;
        };
        tail.strip_prefix(f.command.as_str())
            .is_some_and(|r| r.starts_with(' '))
    }

    /// Runs one inbound frame through the rules, line by line. Non-UTF-8
    /// frames and an empty rule set pass through untouched (rules are
    /// line-protocol rules). A matched-but-illegal token is an error.
    pub fn apply(&self, frame: &[u8]) -> Result<Applied, FrameRuleError> {
        let passthrough = || Applied {
            forward: Some(frame.to_vec()),
            ..Applied::default()
        };
        if self.is_empty() {
            return Ok(passthrough());
        }
        let Ok(text) = std::str::from_utf8(frame) else {
            return Ok(passthrough());
        };
        let mut out = Applied::default();
        let mut kept = String::with_capacity(text.len());
        let mut changed = false;
        for line in text.split_inclusive('\n') {
            let body = line.trim_end_matches(['\r', '\n']);
            if let Some(rule) = self
                .auto_replies
                .iter()
                .find(|r| body.starts_with(r.prefix.as_str()))
            {
                let token = &body[rule.prefix.len()..];
                if !token_ok(token, rule.max_token_len) {
                    return Err(FrameRuleError::BadToken);
                }
                if out.replies.len() >= MAX_REPLIES_PER_FRAME {
                    return Err(FrameRuleError::TooManyReplies);
                }
                out.replies.push(
                    format!("{}{token}{}", rule.reply_prefix, rule.reply_suffix).into_bytes(),
                );
                if rule.forward_to_guest {
                    kept.push_str(line);
                } else {
                    changed = true;
                }
            } else if self.echo_matches(body) {
                out.echo_dropped += 1;
                changed = true;
            } else {
                kept.push_str(line);
            }
        }
        out.forward = if !changed {
            Some(frame.to_vec())
        } else if kept.is_empty() {
            None
        } else {
            Some(kept.into_bytes())
        };
        Ok(out)
    }
}

/// Counters for the frame rules: `svc_ingest_transport_frame_rules_total
/// {platform,rule,outcome}` (rule `auto_reply`/`self_echo`; outcome
/// `replied`/`reply_failed`/`rejected`/`dropped`). Labels are bounded enums
/// and the registered platform slug -- never frame-derived.
#[derive(Clone)]
pub struct FrameRuleMetrics {
    total: prometheus::IntCounterVec,
    ack_timeout: prometheus::IntCounterVec,
}

impl FrameRuleMetrics {
    /// Registers the counter on `registry` (typed error on a duplicate).
    pub fn register(registry: &prometheus::Registry) -> Result<Self, prometheus::Error> {
        let total = prometheus::IntCounterVec::new(
            prometheus::Opts::new(
                "svc_ingest_transport_frame_rules_total",
                "Host-side transport frame rule outcomes, by platform, rule and outcome",
            ),
            &["platform", "rule", "outcome"],
        )?;
        let ack_timeout = prometheus::IntCounterVec::new(
            prometheus::Opts::new(
                "svc_ingest_transport_heartbeat_ack_timeout_total",
                "Connections ended because a heartbeat went unacknowledged, by platform",
            ),
            &["platform"],
        )?;
        registry.register(Box::new(total.clone()))?;
        registry.register(Box::new(ack_timeout.clone()))?;
        Ok(Self { total, ack_timeout })
    }

    /// Counts one heartbeat-ACK timeout (zombie connection ended).
    pub fn ack_timeout(&self, platform: &str) {
        self.ack_timeout.with_label_values(&[platform]).inc();
    }

    /// Counts `n` occurrences.
    pub fn add(&self, platform: &str, rule: &str, outcome: &str, n: u64) {
        self.total
            .with_label_values(&[platform, rule, outcome])
            .inc_by(n);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoint_rules() {
        assert!(validate_endpoint("wss://a.example/x", &["wss"], "ws", false).is_ok());
        assert!(validate_endpoint("ws://a.example/x", &["wss"], "ws", true).is_err());
        assert!(validate_endpoint("ws://127.0.0.1:1/x", &["wss"], "ws", false).is_err());
        assert!(validate_endpoint("ws://127.0.0.1:1/x", &["wss"], "ws", true).is_ok());
        assert!(validate_endpoint("wss://u:p@a.example/", &["wss"], "ws", false).is_err());
        assert!(validate_endpoint("nonsense", &["wss"], "ws", false).is_err());
    }

    #[test]
    fn opaque_id_hides_key() {
        let id = opaque_id("ws", "secret-channel-name");
        assert!(id.starts_with("ws-") && !id.contains("channel"));
        assert_eq!(id, opaque_id("ws", "secret-channel-name"));
    }

    #[test]
    fn count_matches_behaviour() {
        assert_eq!(count_matches(b"a{T}b{T}", b"{T}"), 2);
        assert_eq!(count_matches(b"abc", b"{T}"), 0);
    }

    #[test]
    fn splice_safety() {
        assert!(splice_safe("abc.DEF-1_2"));
        for bad in ["", "a\"b", "a\\b", "a\nb"] {
            assert!(!splice_safe(bad));
        }
    }

    use crate::connector::webhook::{BoxFut, SecretError};

    /// Per-ref secret map (handle id -> value), counts resolves.
    struct MapSecrets(HashMap<i64, &'static str>);
    impl TransportSecretSource for MapSecrets {
        fn resolve(
            &self,
            r: ConnectionRef,
            _d: Destination,
        ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
            let v = self.0.get(&r.connection_id).copied();
            Box::pin(async move {
                v.map(|s| ResolvedAuth {
                    secret: SecretString::new(s.into()),
                    header: None,
                })
                .ok_or(SecretError::Missing)
            })
        }
    }

    fn dest() -> Destination {
        Destination::from_url(&reqwest::Url::parse("https://api.twitch.tv/x").unwrap()).unwrap()
    }

    fn handles() -> HashMap<String, ConnectionRef> {
        ["a", "b"]
            .iter()
            .zip([1i64, 2])
            .map(|(h, id)| {
                (
                    h.to_string(),
                    ConnectionRef {
                        community_id: 1,
                        connection_id: id,
                    },
                )
            })
            .collect()
    }

    /// Per-handle prefix map from `(handle, prefix)` pairs.
    fn pins(v: &[(&str, &str)]) -> SlotPrefixes {
        let mut m = SlotPrefixes::new();
        for (h, p) in v {
            m.entry(h.to_string()).or_default().push(p.to_string());
        }
        m
    }

    fn refs(v: &[(&str, &str)]) -> Vec<(String, String)> {
        v.iter()
            .map(|(a, b)| (a.to_string(), b.to_string()))
            .collect()
    }

    #[tokio::test]
    async fn single_pass_never_rescans_substituted_text() {
        // Secret A contains slot B's text: must stay literal, not be re-expanded.
        let s = MapSecrets(HashMap::from([(1, "x{{B}}y"), (2, "SECOND")]));
        let out = substitute_slots(
            &s,
            &handles(),
            b"k={{A}}\nk={{B}}\nk={{A}}",
            &refs(&[("{{A}}", "a"), ("{{B}}", "b")]),
            &pins(&[("a", "k="), ("b", "k=")]),
            &dest(),
        )
        .await
        .unwrap();
        assert_eq!(&out[..], b"k=x{{B}}y\nk=SECOND\nk=x{{B}}y");
    }

    #[tokio::test]
    async fn bad_slot_shapes_and_uniform_errors() {
        let s = MapSecrets(HashMap::from([(1, "s1"), (2, "s2")]));
        let h = handles();
        let cases: Vec<Vec<(&str, &str)>> = vec![
            vec![("", "a")],
            vec![("{{A}}", "a"), ("{{A}}", "b")],
            vec![("{{A}}", "a"), ("{{A}}x", "b")],
            vec![("{{A}}", "nope")],
            vec![("{{ABSENT}}", "a")],
        ];
        let mut msgs = Vec::new();
        for c in cases {
            let e = substitute_slots(
                &s,
                &h,
                b"{{A}} {{A}}x",
                &refs(&c),
                &pins(&[("a", " "), ("b", " ")]),
                &dest(),
            )
            .await
            .unwrap_err();
            msgs.push(e);
        }
        assert!(msgs.windows(2).all(|w| w[0] == w[1]), "uniform error");
        let many: Vec<(String, String)> = (0..=MAX_SECRET_REFS)
            .map(|i| (format!("{{{{S{i}}}}}"), "a".into()))
            .collect();
        assert!(
            substitute_slots(&s, &h, b"x", &many, &SlotPrefixes::new(), &dest())
                .await
                .is_err()
        );
    }

    /// regression: p982-A -- a connector-authored template must not be able to
    /// place the credential in a message body/target (publishing it to a
    /// channel); only host-pinned prefixes may precede a slot.
    #[tokio::test]
    async fn slot_only_substituted_after_host_pinned_prefix() {
        let s = MapSecrets(HashMap::from([(1, "tok"), (2, "x")]));
        let h = handles();
        let r = refs(&[("{{AUTH}}", "a")]);
        let pins = pins(&[("a", "PASS oauth:")]);
        let ok = substitute_slots(
            &s,
            &h,
            b"PASS oauth:{{AUTH}}\r\nNICK bot",
            &r,
            &pins,
            &dest(),
        )
        .await
        .unwrap();
        assert_eq!(&ok[..], b"PASS oauth:tok\r\nNICK bot");
        for evil in [
            &b"PRIVMSG #attacker :{{AUTH}}"[..],
            b"PASS oauth:{{AUTH}}\r\nPRIVMSG #attacker :{{AUTH}}",
            b"PASS oauth:{{AUTH}}extra",
            b"{{AUTH}}",
        ] {
            assert!(
                substitute_slots(&s, &h, evil, &r, &pins, &dest())
                    .await
                    .is_err(),
                "{}",
                String::from_utf8_lossy(evil)
            );
        }
        // No pins configured = deny everything (fail closed).
        assert!(substitute_slots(
            &s,
            &h,
            b"PASS oauth:{{AUTH}}",
            &r,
            &SlotPrefixes::new(),
            &dest()
        )
        .await
        .is_err());
    }

    /// regression: p982-C -- CRLF in the guest template cannot move the
    /// credential outside the pinned position (injected commands never get it).
    #[tokio::test]
    async fn crlf_injected_template_cannot_relocate_secret() {
        let s = MapSecrets(HashMap::from([(1, "tok")]));
        let r = refs(&[("{{AUTH}}", "a")]);
        let pins = pins(&[("a", "PASS oauth:")]);
        let out = substitute_slots(
            &s,
            &handles(),
            b"JOIN #a\r\nPASS oauth:{{AUTH}}\r\nPRIVMSG #a :hi",
            &r,
            &pins,
            &dest(),
        )
        .await
        .unwrap();
        assert_eq!(
            out.windows(3).filter(|w| *w == b"tok").count(),
            1,
            "secret appears exactly once"
        );
        // CRLF-injected second line that tries to echo the secret into a body.
        let hostile = b"PASS oauth:{{AUTH}}\r\nPRIVMSG #a :{{AUTH}}";
        assert!(
            substitute_slots(&s, &handles(), hostile, &r, &pins, &dest())
                .await
                .is_err(),
            "CRLF-injected body slot must be refused"
        );
    }

    fn pingpong() -> AutoReplyRule {
        AutoReplyRule {
            prefix: "PING :".into(),
            reply_prefix: "PONG :".into(),
            reply_suffix: "\r\n".into(),
            max_token_len: 64,
            forward_to_guest: false,
        }
    }

    fn echo() -> SelfEchoFilter {
        SelfEchoFilter {
            self_id: "WaddleBot".into(),
            sender_open: ":".into(),
            sender_close: "!".into(),
            command: "PRIVMSG".into(),
        }
    }

    fn rules() -> FrameRules {
        FrameRules::new(vec![pingpong()], Some(echo())).unwrap()
    }

    #[test]
    fn ping_is_answered_and_consumed() {
        let a = rules().apply(b"PING :tmi.twitch.tv\r\n").unwrap();
        assert_eq!(a.replies, vec![b"PONG :tmi.twitch.tv\r\n".to_vec()]);
        assert_eq!(a.forward, None);
    }

    #[test]
    fn forward_to_guest_keeps_the_line() {
        let mut r = pingpong();
        r.forward_to_guest = true;
        let rs = FrameRules::new(vec![r], None).unwrap();
        let a = rs.apply(b"PING :x\r\n").unwrap();
        assert_eq!(a.replies.len(), 1);
        assert_eq!(a.forward.as_deref(), Some(&b"PING :x\r\n"[..]));
    }

    #[test]
    fn batched_frame_answers_ping_and_forwards_the_rest() {
        let a = rules()
            .apply(b"PING :abc\r\n:other!o@o PRIVMSG #c :hi\r\n")
            .unwrap();
        assert_eq!(a.replies, vec![b"PONG :abc\r\n".to_vec()]);
        assert_eq!(
            a.forward.as_deref(),
            Some(&b":other!o@o PRIVMSG #c :hi\r\n"[..])
        );
    }

    /// regression: p6b-crlf -- a token carrying CR/LF/space or other bytes can
    /// never be reflected; the frame is an error, not a reply.
    #[test]
    fn hostile_tokens_are_rejected_not_reflected() {
        let long = [&b"PING :"[..], "x".repeat(65).as_bytes(), b"\r\n"].concat();
        for bad in [
            &b"PING :a\rJOIN #x\r\n"[..],
            b"PING :a b\r\n",
            b"PING :\r\n",
            b"PING :\xc3\xa9\r\n",
            b"PING :a;b\r\n",
            &long,
        ] {
            assert_eq!(rules().apply(bad), Err(FrameRuleError::BadToken), "{bad:?}");
        }
    }

    #[test]
    fn reply_flood_is_bounded() {
        let frame = "PING :a\r\n".repeat(MAX_REPLIES_PER_FRAME + 1);
        assert_eq!(
            rules().apply(frame.as_bytes()),
            Err(FrameRuleError::TooManyReplies)
        );
        let ok = "PING :a\r\n".repeat(MAX_REPLIES_PER_FRAME);
        assert_eq!(rules().apply(ok.as_bytes()).unwrap().replies.len(), 8);
    }

    #[test]
    fn self_echo_dropped_others_pass() {
        let own = b":waddlebot!waddlebot@waddlebot.tmi.twitch.tv PRIVMSG #c :hi\r\n";
        let a = rules().apply(own).unwrap();
        assert_eq!((a.forward, a.echo_dropped), (None, 1));
        let tagged = b"@badge-info=;id=1 :WADDLEBOT!w@w.tmi PRIVMSG #c :hi\r\n";
        assert_eq!(rules().apply(tagged).unwrap().echo_dropped, 1);
        let other = b":someone!s@s.tmi PRIVMSG #c :hi\r\n";
        let a = rules().apply(other).unwrap();
        assert_eq!(a.forward.as_deref(), Some(&other[..]));
        assert_eq!(a.echo_dropped, 0);
        // non-PRIVMSG from self (e.g. JOIN ack) is not an echo
        let join = b":waddlebot!w@w JOIN #c\r\n";
        assert_eq!(rules().apply(join).unwrap().echo_dropped, 0);
        // a longer command that merely starts with the filtered one is kept
        let longer = b":waddlebot!w@w PRIVMSGX #c :hi\r\n";
        assert_eq!(rules().apply(longer).unwrap().echo_dropped, 0);
        // a prefix-sharing nick is not the bot
        let near = b":waddlebot2!w@w PRIVMSG #c :hi\r\n";
        assert_eq!(rules().apply(near).unwrap().echo_dropped, 0);
        // text that merely mentions the bot is not an echo
        let mention = b":x!x@x PRIVMSG #c :waddlebot!w PRIVMSG\r\n";
        assert_eq!(rules().apply(mention).unwrap().echo_dropped, 0);
        // tag-only malformed line is kept, not a panic
        assert_eq!(rules().apply(b"@only-tags").unwrap().echo_dropped, 0);
        // a mixed frame drops only the echo line
        let mixed = [&other[..], &own[..]].concat();
        let a = rules().apply(&mixed).unwrap();
        assert_eq!(a.forward.as_deref(), Some(&other[..]));
        assert_eq!(a.echo_dropped, 1);
    }

    #[test]
    fn untouched_paths_pass_through() {
        let none = FrameRules::default();
        assert!(none.is_empty());
        assert_eq!(
            none.apply(b"PING :x").unwrap().forward.as_deref(),
            Some(&b"PING :x"[..])
        );
        let bin = [0xff, 0xfe, b'\n'];
        assert_eq!(
            rules().apply(&bin).unwrap().forward.as_deref(),
            Some(&bin[..])
        );
        let plain = br#"{"op":10}"#;
        let a = rules().apply(plain).unwrap();
        assert_eq!(a.forward.as_deref(), Some(&plain[..]));
        assert!(a.replies.is_empty());
    }

    #[test]
    fn misconfig_is_refused() {
        let mutations: [fn(&mut AutoReplyRule); 6] = [
            |r| r.prefix = String::new(),
            |r| r.prefix = "PI\nNG".into(),
            |r| r.reply_prefix = "PO\rNG".into(),
            |r| r.reply_suffix = "\r\n\r\n".into(),
            |r| r.max_token_len = 0,
            |r| r.max_token_len = MAX_REPLY_TOKEN_LEN + 1,
        ];
        for f in mutations {
            let mut r = pingpong();
            f(&mut r);
            assert!(matches!(
                FrameRules::new(vec![r], None),
                Err(FrameRuleError::Config(_))
            ));
        }
        let mut overlap = pingpong();
        overlap.prefix = "PING".into();
        assert!(FrameRules::new(vec![pingpong(), overlap], None).is_err());
        assert!(FrameRules::new(vec![pingpong(), pingpong()], None).is_err());
        let many: Vec<_> = (0..=MAX_AUTO_REPLY_RULES)
            .map(|i| AutoReplyRule {
                prefix: format!("P{i}:"),
                ..pingpong()
            })
            .collect();
        assert!(FrameRules::new(many, None).is_err());
        let echo_mut: [fn(&mut SelfEchoFilter); 6] = [
            |f| f.self_id = String::new(),
            |f| f.self_id = "a b".into(),
            |f| f.sender_open = String::new(),
            |f| f.sender_close = "!!!!!".into(),
            |f| f.command = "PRIV MSG".into(),
            |f| f.command = String::new(),
        ];
        for m in echo_mut {
            let mut f = echo();
            m(&mut f);
            assert!(FrameRules::new(vec![], Some(f)).is_err());
        }
    }

    #[test]
    fn metrics_register_once() {
        let reg = prometheus::Registry::new();
        let m = FrameRuleMetrics::register(&reg).unwrap();
        m.add("p", "auto_reply", "replied", 2);
        assert!(FrameRuleMetrics::register(&reg).is_err());
        assert_eq!(
            reg.gather()[0].get_metric()[0].get_counter().value() as u64,
            2
        );
    }

    /// regression: per-handle prefixes -- a credential slot after a
    /// NON-credential prefix (`NICK `) is refused even though that prefix is
    /// declared for another handle.
    #[tokio::test]
    async fn slot_prefix_is_bound_to_its_own_handle() {
        let s = MapSecrets(HashMap::from([(1, "tok"), (2, "nickname")]));
        let h = handles();
        // a = secret (PASS oauth:), b = nick (NICK ).
        let p = pins(&[("a", "PASS oauth:"), ("b", "NICK ")]);
        let r = refs(&[("{{PASS}}", "a"), ("{{NICK}}", "b")]);
        let ok = substitute_slots(
            &s,
            &h,
            b"PASS oauth:{{PASS}}\r\nNICK {{NICK}}\r\n",
            &r,
            &p,
            &dest(),
        )
        .await
        .unwrap();
        assert_eq!(&ok[..], b"PASS oauth:tok\r\nNICK nickname\r\n");
        // Secret slot placed after the nick handle's prefix: refused.
        for evil in [
            &b"NICK {{PASS}}\r\nPASS oauth:{{NICK}}"[..],
            b"NICK {{PASS}}",
        ] {
            assert!(
                substitute_slots(&s, &h, evil, &r, &p, &dest())
                    .await
                    .is_err(),
                "{}",
                String::from_utf8_lossy(evil)
            );
        }
        // Same slot legitimately placed once and illegitimately once: refused.
        assert!(substitute_slots(
            &s,
            &h,
            b"PASS oauth:{{PASS}}\r\nNICK {{PASS}}",
            &refs(&[("{{PASS}}", "a")]),
            &p,
            &dest()
        )
        .await
        .is_err());
    }

    #[test]
    fn refusal_reasons_are_typed() {
        let r = refs(&[("{{A}}", "a")]);
        let p = pins(&[("a", "k=")]);
        assert_eq!(slot_refusal(b"k={{A}}", &r, &p), None);
        assert_eq!(
            slot_refusal(b"x={{A}}", &r, &p),
            Some(SlotRefusal::PlacementRefused)
        );
        assert_eq!(
            slot_refusal(b"k={{A}}", &r, &pins(&[("b", "k=")])),
            Some(SlotRefusal::HandleNotDeclared)
        );
        assert_eq!(
            slot_refusal(b"k={{A}}", &r, &pins(&[("a", "")])),
            Some(SlotRefusal::HandleNotDeclared)
        );
        assert_eq!(
            SlotRefusal::HandleNotDeclared.outcome(),
            "refused_undeclared"
        );
        assert_eq!(SlotRefusal::PlacementRefused.outcome(), "refused_placement");
        assert!(!SlotRefusal::PlacementRefused.to_string().is_empty());
        assert!(!SlotRefusal::HandleNotDeclared.to_string().is_empty());
        assert_eq!(slot_refused_error(), uniform());
    }

    /// regression: unanchored suffix match -- a prefix that merely ENDS the
    /// preceding bytes (mid-line IRC body, nested JSON key) is refused; a
    /// prefix at frame start or right after CR/LF is accepted.
    #[tokio::test]
    async fn prefix_must_be_anchored_at_frame_or_line_start() {
        let s = MapSecrets(HashMap::from([(1, "tok"), (2, "x")]));
        let h = handles();
        let r = refs(&[("{{S}}", "a")]);
        let irc = pins(&[("a", "PASS oauth:")]);
        for ok in [
            &b"PASS oauth:{{S}}"[..],
            b"CAP REQ :x\r\nPASS oauth:{{S}}\r\n",
            b"CAP REQ :x\nPASS oauth:{{S}}",
        ] {
            assert!(substitute_slots(&s, &h, ok, &r, &irc, &dest())
                .await
                .is_ok());
        }
        for evil in [
            &b"PRIVMSG #chan :PASS oauth:{{S}}"[..],
            b"PASS oauth:{{S}}\r\nPRIVMSG #c :PASS oauth:{{S}}",
            b"xPASS oauth:{{S}}",
        ] {
            assert!(
                substitute_slots(&s, &h, evil, &r, &irc, &dest())
                    .await
                    .is_err(),
                "{}",
                String::from_utf8_lossy(evil)
            );
        }
        let json = pins(&[("a", r#"{"op":2,"d":{"token":""#)]);
        let ok = substitute_slots(
            &s,
            &h,
            br#"{"op":2,"d":{"token":"{{S}}"}}"#,
            &r,
            &json,
            &dest(),
        )
        .await
        .unwrap();
        assert_eq!(&ok[..], br#"{"op":2,"d":{"token":"tok"}}"#);
        for evil in [
            &br#"{"op":2,"d":{"x":"a","y":{"token":"{{S}}"}}}"#[..],
            br#"{"op":2,"d":{"x":"\"token\":\"{{S}}"}}"#,
            br#"{"a":"x"token":"{{S}}"}"#,
        ] {
            assert!(
                substitute_slots(&s, &h, evil, &r, &json, &dest())
                    .await
                    .is_err(),
                "{}",
                String::from_utf8_lossy(evil)
            );
        }
    }

    #[test]
    fn anchored_prefix_edges() {
        assert!(!anchored_prefix(b"ab", 0, "ab"));
        assert!(!anchored_prefix(b"ab", 2, ""));
        assert!(!anchored_prefix(b"ab", 1, "abc"));
        assert!(anchored_prefix(b"\rab", 3, "ab"));
        assert!(!anchored_prefix(b" ab", 3, "ab"));
    }

    /// A secret carrying CR/LF/NUL/quote/backslash can never be spliced
    /// (structure injection): fail closed with the typed splice reason.
    #[tokio::test]
    async fn secret_with_structure_chars_is_refused() {
        let h = handles();
        let r = refs(&[("{{S}}", "a")]);
        let p = pins(&[("a", "PASS oauth:")]);
        for bad in ["a\r\nPRIVMSG #c :x", "a\nb", "a\0b", "a\"b", "a\\b"] {
            let s = MapSecrets(HashMap::from([(1, bad)]));
            let e = substitute_slots(&s, &h, b"PASS oauth:{{S}}", &r, &p, &dest())
                .await
                .unwrap_err();
            assert_eq!(e, TransportError::Fatal(SECRET_NOT_SPLICE_SAFE.into()));
        }
    }
}
