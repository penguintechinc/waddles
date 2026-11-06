//! Template verification: everything a connector-built `http-request-tpl`
//! must satisfy BEFORE the host substitutes a single slot or opens a socket.
//!
//! The connector is a guest: its output is untrusted input, exactly like a
//! queue entry. [`verify`] therefore checks the template against the
//! per-platform/per-kind [`PlatformPolicy`] and returns a
//! [`VerifiedTemplate`] -- the ONLY type the substituter accepts, so an
//! unverified template cannot reach the wire. Every refusal is a typed
//! [`TemplateError`] whose text carries no template content (no URL, header
//! value, slot text or secret).

use std::collections::BTreeSet;

use serde_json::Value;

use super::policy::{
    HeaderCred, KindPolicy, PathSeg, PlatformPolicy, Position, SlotOrigin, SlotShape, SlotSyntax,
};
use super::RequestTpl;

/// Longest accepted template URL.
pub const MAX_URL_BYTES: usize = 2048;
/// Most headers a template may carry.
pub const MAX_HEADERS: usize = 16;
/// Longest accepted header value (template side).
pub const MAX_HEADER_VALUE_BYTES: usize = 512;
/// Deepest JSON nesting in a body.
const MAX_BODY_DEPTH: usize = 8;
/// Most JSON nodes in a body.
const MAX_BODY_NODES: usize = 512;

/// Why a template was refused. Never carries template content.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum TemplateError {
    /// Method differs from the kind's required method.
    #[error("template method is not the kind's method")]
    BadMethod,
    /// URL too long or not parseable into scheme/authority/path.
    #[error("template url is malformed")]
    BadUrl,
    /// Scheme is not `https`.
    #[error("template url scheme is not https")]
    BadScheme,
    /// The URL carries userinfo.
    #[error("template url carries userinfo")]
    UserInfo,
    /// The URL carries a fragment.
    #[error("template url carries a fragment")]
    Fragment,
    /// The URL authority is not a declared egress host.
    #[error("template url host is not a declared egress host")]
    HostNotAllowed,
    /// Path differs from the kind's expected path.
    #[error("template url path does not match the kind")]
    PathMismatch,
    /// Query keys differ from the kind's exact set.
    #[error("template url query keys do not match the kind")]
    QueryMismatch,
    /// A query value is neither a whole declared slot nor a safe literal.
    #[error("template url query value is invalid")]
    QueryValueInvalid,
    /// A `{{`/`}}`/brace outside a declared slot.
    #[error("template carries a stray or unclosed slot marker")]
    StrayMarker,
    /// A slot token that is not declared for this platform.
    #[error("template names an undeclared slot")]
    UndeclaredSlot,
    /// A declared slot that this kind may not use.
    #[error("template uses a slot not allowed for this kind")]
    SlotNotAllowedForKind,
    /// A slot in a position its declaration does not allow.
    #[error("template places a slot in a position it may not occupy")]
    SlotMisplaced,
    /// Control, zero-width, bidi or other invisible character.
    #[error("template carries a control or invisible character")]
    InvisibleChar,
    /// A header that is forbidden outright or not on the allowlist.
    #[error("template header is not allowed")]
    HeaderNotAllowed,
    /// A header value that is not a safe literal / whole slot.
    #[error("template header value is invalid")]
    HeaderValueInvalid,
    /// The same header twice.
    #[error("template repeats a header")]
    DuplicateHeader,
    /// More headers than [`MAX_HEADERS`].
    #[error("template carries too many headers")]
    TooManyHeaders,
    /// `Authorization` set by the template other than via a credential slot.
    #[error("template sets Authorization other than through a credential slot")]
    AuthorizationFromTemplate,
    /// A `secret-refs` entry that matches no declared slot / header credential.
    #[error("template secret-ref is unknown")]
    SecretRefUnknown,
    /// A `secret-refs` entry paired with the wrong handle.
    #[error("template secret-ref handle does not match the declaration")]
    SecretRefHandleMismatch,
    /// The same `secret-refs` entry twice.
    #[error("template repeats a secret-ref")]
    SecretRefDuplicate,
    /// A slot is used but not declared in `secret-refs`, or a required
    /// header credential is missing.
    #[error("template uses a slot or credential it did not declare")]
    SecretRefMissing,
    /// A body on a kind that takes none, or none where one is required.
    #[error("template body presence does not match the kind")]
    BodyPresence,
    /// Body over the kind's bound.
    #[error("template body is over the size bound")]
    BodyTooLarge,
    /// Body is not a JSON object / too deep / too large a tree.
    #[error("template body is not a bounded json object")]
    BodyNotJson,
    /// Body without `Content-Type: application/json`.
    #[error("template body lacks the json content type")]
    BodyContentType,
}

impl TemplateError {
    /// Stable low-cardinality reason code (metric label / ack detail).
    #[must_use]
    pub fn code(self) -> &'static str {
        match self {
            Self::BadMethod => "bad_method",
            Self::BadUrl => "bad_url",
            Self::BadScheme => "bad_scheme",
            Self::UserInfo => "userinfo",
            Self::Fragment => "fragment",
            Self::HostNotAllowed => "host_not_allowed",
            Self::PathMismatch => "path_mismatch",
            Self::QueryMismatch => "query_mismatch",
            Self::QueryValueInvalid => "query_value_invalid",
            Self::StrayMarker => "stray_marker",
            Self::UndeclaredSlot => "undeclared_slot",
            Self::SlotNotAllowedForKind => "slot_not_allowed_for_kind",
            Self::SlotMisplaced => "slot_misplaced",
            Self::InvisibleChar => "invisible_char",
            Self::HeaderNotAllowed => "header_not_allowed",
            Self::HeaderValueInvalid => "header_value_invalid",
            Self::DuplicateHeader => "duplicate_header",
            Self::TooManyHeaders => "too_many_headers",
            Self::AuthorizationFromTemplate => "authorization_from_template",
            Self::SecretRefUnknown => "secret_ref_unknown",
            Self::SecretRefHandleMismatch => "secret_ref_handle_mismatch",
            Self::SecretRefDuplicate => "secret_ref_duplicate",
            Self::SecretRefMissing => "secret_ref_missing",
            Self::BodyPresence => "body_presence",
            Self::BodyTooLarge => "body_too_large",
            Self::BodyNotJson => "body_not_json",
            Self::BodyContentType => "body_content_type",
        }
    }
}

/// A literal run or a declared slot (index into `PlatformPolicy::slots`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Piece {
    Lit(String),
    Slot(usize),
}

/// A template that passed [`verify`]; the substituter's only input.
#[derive(Debug)]
pub struct VerifiedTemplate {
    /// `scheme://authority` (already checked against the host allowlist).
    pub(crate) origin: String,
    pub(crate) method: String,
    pub(crate) path: Vec<Vec<Piece>>,
    pub(crate) query: Vec<(String, Vec<Piece>)>,
    pub(crate) headers: Vec<(String, Vec<Piece>)>,
    pub(crate) header_creds: Vec<HeaderCred>,
    pub(crate) body: Option<Value>,
    pub(crate) body_slots: Vec<(String, usize)>,
    pub(crate) max_body: Option<usize>,
    /// Every slot index the template actually uses (URL, headers, body).
    pub(crate) used: BTreeSet<usize>,
}

/// `true` for control characters and the invisible format characters that can
/// hide or reshape a slot (zero-width, bidi, word joiner, BOM, soft hyphen,
/// line/paragraph separators, tag characters).
#[must_use]
pub fn is_invisible(c: char) -> bool {
    c.is_control()
        || matches!(c,
            '\u{00AD}' | '\u{200B}'..='\u{200F}' | '\u{2028}'..='\u{202E}'
            | '\u{2060}'..='\u{206F}' | '\u{FEFF}' | '\u{E0000}'..='\u{E007F}')
}

fn id_literal(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn query_literal(s: &str) -> bool {
    s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b','))
}

/// How strictly literal runs are checked for braces.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Braces {
    /// No `{`/`}` at all outside a slot (URL, headers).
    Strict,
    /// Only the pairs `{{`/`}}` are forbidden (free text in a JSON body).
    Pairs,
}

fn tokenize(policy: &PlatformPolicy, s: &str, braces: Braces) -> Result<Vec<Piece>, TemplateError> {
    let (open, close) = match policy.syntax {
        SlotSyntax::Double => ("{{", "}}"),
        SlotSyntax::Single => ("{", "}"),
    };
    let check_lit = |lit: &str| -> Result<(), TemplateError> {
        let bad = match braces {
            Braces::Strict => lit.contains('{') || lit.contains('}'),
            Braces::Pairs => lit.contains("{{") || lit.contains("}}"),
        };
        if bad {
            Err(TemplateError::StrayMarker)
        } else {
            Ok(())
        }
    };
    let mut out = Vec::new();
    let mut rest = s;
    loop {
        let Some(i) = rest.find(open) else {
            check_lit(rest)?;
            if !rest.is_empty() {
                out.push(Piece::Lit(rest.to_string()));
            }
            return Ok(out);
        };
        let lit = &rest[..i];
        check_lit(lit)?;
        let after = &rest[i + open.len()..];
        let j = after.find(close).ok_or(TemplateError::StrayMarker)?;
        let name = &after[..j];
        if name.contains('{') || name.contains('}') {
            return Err(TemplateError::StrayMarker);
        }
        let idx = policy
            .slot_index(name)
            .ok_or(TemplateError::UndeclaredSlot)?;
        if !lit.is_empty() {
            out.push(Piece::Lit(lit.to_string()));
        }
        out.push(Piece::Slot(idx));
        rest = &after[j + close.len()..];
    }
}

/// Headers a template may never set, regardless of any allowlist.
fn header_forbidden(name: &str) -> bool {
    matches!(
        name,
        "host"
            | "cookie"
            | "set-cookie"
            | "content-length"
            | "transfer-encoding"
            | "connection"
            | "te"
            | "upgrade"
            | "expect"
            | "forwarded"
            | "x-real-ip"
            | "x-http-method-override"
            | "proxy-authorization"
            | "proxy-authenticate"
    ) || name.starts_with("proxy-")
        || name.starts_with("x-forwarded-")
        || name.starts_with("x-waddle-host-")
}

struct Ctx<'a> {
    policy: &'a PlatformPolicy,
    kind: &'a KindPolicy,
    used: BTreeSet<usize>,
}

impl Ctx<'_> {
    /// A slot declared and allowed for this kind (not yet position-checked).
    fn allowed(&self, idx: usize) -> Result<(), TemplateError> {
        let name = self.policy.slots[idx].name;
        if self.kind.slots.contains(&name) {
            Ok(())
        } else {
            Err(TemplateError::SlotNotAllowedForKind)
        }
    }

    fn require_position(&mut self, idx: usize, pos: Position) -> Result<(), TemplateError> {
        self.allowed(idx)?;
        if self.policy.slots[idx].positions.contains(&pos) {
            self.used.insert(idx);
            Ok(())
        } else {
            Err(TemplateError::SlotMisplaced)
        }
    }
}

/// Verifies `tpl` for `kind` under `policy`.
///
/// # Errors
/// A typed [`TemplateError`] for the FIRST rule violated; nothing about the
/// template's content is carried in it.
pub fn verify(
    policy: &PlatformPolicy,
    kind: &KindPolicy,
    tpl: &RequestTpl,
) -> Result<VerifiedTemplate, TemplateError> {
    if tpl.method != kind.method {
        return Err(TemplateError::BadMethod);
    }
    let mut cx = Ctx {
        policy,
        kind,
        used: BTreeSet::new(),
    };
    let (origin, path, query) = verify_url(&mut cx, &tpl.url)?;
    let headers = verify_headers(&mut cx, &tpl.headers)?;
    let (body, body_slots) = verify_body(&mut cx, tpl, &headers)?;
    let header_creds = verify_secret_refs(&cx, &tpl.secret_refs)?;
    Ok(VerifiedTemplate {
        origin,
        method: tpl.method.clone(),
        path,
        query,
        headers,
        header_creds,
        body,
        body_slots,
        max_body: kind.max_body,
        used: cx.used,
    })
}

type UrlParts = (String, Vec<Vec<Piece>>, Vec<(String, Vec<Piece>)>);

fn verify_url(cx: &mut Ctx<'_>, url: &str) -> Result<UrlParts, TemplateError> {
    if url.len() > MAX_URL_BYTES {
        return Err(TemplateError::BadUrl);
    }
    if url.chars().any(is_invisible) {
        return Err(TemplateError::InvisibleChar);
    }
    let policy = cx.policy;
    let rest = if let Some(r) = url.strip_prefix("https://") {
        r
    } else if policy.allow_http && url.starts_with("http://") {
        &url["http://".len()..]
    } else if url.contains("://") {
        return Err(TemplateError::BadScheme);
    } else {
        return Err(TemplateError::BadUrl);
    };
    let scheme = &url[..url.len() - rest.len()];
    if rest.contains('#') {
        return Err(TemplateError::Fragment);
    }
    let auth_end = rest.find(['/', '?']).unwrap_or(rest.len());
    let authority = &rest[..auth_end];
    if authority.contains('@') {
        return Err(TemplateError::UserInfo);
    }
    if !policy
        .hosts
        .iter()
        .any(|h| h.as_str() == authority.to_ascii_lowercase())
    {
        return Err(TemplateError::HostNotAllowed);
    }
    let tail = &rest[auth_end..];
    let (path_str, query_str) = match tail.split_once('?') {
        Some((p, q)) => (p, Some(q)),
        None => (tail, None),
    };
    let path = verify_path(cx, path_str)?;
    let query = verify_query(cx, query_str)?;
    Ok((format!("{scheme}{authority}"), path, query))
}

fn verify_path(cx: &mut Ctx<'_>, path: &str) -> Result<Vec<Vec<Piece>>, TemplateError> {
    let segs: Vec<&str> = path
        .strip_prefix('/')
        .ok_or(TemplateError::PathMismatch)?
        .split('/')
        .collect();
    if segs.len() != cx.kind.path.len() {
        return Err(TemplateError::PathMismatch);
    }
    let mut out = Vec::with_capacity(segs.len());
    for (seg, want) in segs.iter().zip(cx.kind.path.clone()) {
        match want {
            PathSeg::Lit(l) => {
                if *seg != l {
                    return Err(TemplateError::PathMismatch);
                }
                out.push(vec![Piece::Lit(l.to_string())]);
            }
            PathSeg::Id => {
                if !id_literal(seg) {
                    return Err(TemplateError::PathMismatch);
                }
                out.push(vec![Piece::Lit((*seg).to_string())]);
            }
            PathSeg::Slot(name) => {
                let pieces = tokenize(cx.policy, seg, Braces::Strict)?;
                match pieces.as_slice() {
                    [Piece::Slot(idx)] if cx.policy.slots[*idx].name == name => {
                        let idx = *idx;
                        cx.require_position(idx, Position::Path)?;
                    }
                    [Piece::Slot(_)] => return Err(TemplateError::PathMismatch),
                    _ => return Err(TemplateError::PathMismatch),
                }
                out.push(pieces);
            }
        }
    }
    Ok(out)
}

fn verify_query(
    cx: &mut Ctx<'_>,
    query: Option<&str>,
) -> Result<Vec<(String, Vec<Piece>)>, TemplateError> {
    let want = cx.kind.query.clone();
    let Some(q) = query else {
        return if want.is_empty() {
            Ok(Vec::new())
        } else {
            Err(TemplateError::QueryMismatch)
        };
    };
    if want.is_empty() {
        return Err(TemplateError::QueryMismatch);
    }
    let mut out: Vec<(String, Vec<Piece>)> = Vec::new();
    for pair in q.split('&') {
        let (k, v) = pair.split_once('=').ok_or(TemplateError::QueryMismatch)?;
        let key: &'static str = want
            .iter()
            .copied()
            .find(|w| *w == k)
            .ok_or(TemplateError::QueryMismatch)?;
        if out.iter().any(|(ek, _)| ek == k) {
            return Err(TemplateError::QueryMismatch);
        }
        let pieces = tokenize(cx.policy, v, Braces::Strict)?;
        match pieces.as_slice() {
            [Piece::Slot(idx)] => {
                let idx = *idx;
                cx.require_position(idx, Position::Query(key))?;
            }
            [Piece::Lit(l)] if query_literal(l) => {}
            [] => {}
            _ => return Err(TemplateError::QueryValueInvalid),
        }
        out.push((k.to_string(), pieces));
    }
    if out.len() != want.len() {
        return Err(TemplateError::QueryMismatch);
    }
    Ok(out)
}

fn verify_headers(
    cx: &mut Ctx<'_>,
    headers: &[(String, String)],
) -> Result<Vec<(String, Vec<Piece>)>, TemplateError> {
    if headers.len() > MAX_HEADERS {
        return Err(TemplateError::TooManyHeaders);
    }
    let policy = cx.policy;
    let mut seen = BTreeSet::new();
    let mut out = Vec::new();
    for (raw_name, value) in headers {
        if raw_name.chars().any(is_invisible) || value.chars().any(is_invisible) {
            return Err(TemplateError::InvisibleChar);
        }
        let name = raw_name.to_ascii_lowercase();
        if name.is_empty()
            || !name
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        {
            return Err(TemplateError::HeaderNotAllowed);
        }
        if value.len() > MAX_HEADER_VALUE_BYTES {
            return Err(TemplateError::HeaderValueInvalid);
        }
        if !seen.insert(name.clone()) {
            return Err(TemplateError::DuplicateHeader);
        }
        let pieces = tokenize(policy, value, Braces::Strict)?;
        if name == "authorization" {
            // Only `<allowed prefix><credential slot>`; a literal credential
            // (or anything else) from the template is refused.
            let ok_prefix = |p: &str| policy.auth_prefixes.contains(&p);
            let idx = match pieces.as_slice() {
                [Piece::Lit(p), Piece::Slot(idx)] if ok_prefix(p.as_str()) => *idx,
                _ => return Err(TemplateError::AuthorizationFromTemplate),
            };
            let decl = &policy.slots[idx];
            if decl.origin != SlotOrigin::Resolver || decl.shape != SlotShape::Secret {
                return Err(TemplateError::AuthorizationFromTemplate);
            }
            cx.require_position(idx, Position::Header("authorization"))?;
            out.push((name, pieces));
            continue;
        }
        if header_forbidden(&name) || !policy.allowed_headers.contains(&name.as_str()) {
            return Err(TemplateError::HeaderNotAllowed);
        }
        let has_slot = pieces.iter().any(|p| matches!(p, Piece::Slot(_)));
        if has_slot {
            let [Piece::Slot(idx)] = pieces.as_slice() else {
                return Err(TemplateError::HeaderValueInvalid);
            };
            let key: &'static str = policy
                .allowed_headers
                .iter()
                .copied()
                .find(|h| *h == name)
                .ok_or(TemplateError::HeaderNotAllowed)?;
            let idx = *idx;
            cx.require_position(idx, Position::Header(key))?;
        } else if name == "content-type" {
            if value != "application/json" {
                return Err(TemplateError::HeaderValueInvalid);
            }
        } else if !value.bytes().all(|b| (0x20..0x7f).contains(&b)) {
            return Err(TemplateError::HeaderValueInvalid);
        }
        out.push((name, pieces));
    }
    Ok(out)
}

type BodyParts = (Option<Value>, Vec<(String, usize)>);

fn verify_body(
    cx: &mut Ctx<'_>,
    tpl: &RequestTpl,
    headers: &[(String, Vec<Piece>)],
) -> Result<BodyParts, TemplateError> {
    let Some(max) = cx.kind.max_body else {
        return if tpl.body.is_some() {
            Err(TemplateError::BodyPresence)
        } else {
            Ok((None, Vec::new()))
        };
    };
    let bytes = tpl.body.as_ref().ok_or(TemplateError::BodyPresence)?;
    if bytes.len() > max {
        return Err(TemplateError::BodyTooLarge);
    }
    let json_ct = headers
        .iter()
        .any(|(n, p)| n == "content-type" && p == &[Piece::Lit("application/json".to_string())]);
    if !json_ct {
        return Err(TemplateError::BodyContentType);
    }
    let value: Value = serde_json::from_slice(bytes).map_err(|_| TemplateError::BodyNotJson)?;
    if !value.is_object() {
        return Err(TemplateError::BodyNotJson);
    }
    let mut slots = Vec::new();
    let mut nodes = 0usize;
    walk_body(cx, &value, "", 0, &mut nodes, &mut slots)?;
    Ok((Some(value), slots))
}

/// JSON-pointer escaping of one object key.
fn escape_pointer(key: &str) -> String {
    key.replace('~', "~0").replace('/', "~1")
}

fn walk_body(
    cx: &mut Ctx<'_>,
    v: &Value,
    ptr: &str,
    depth: usize,
    nodes: &mut usize,
    slots: &mut Vec<(String, usize)>,
) -> Result<(), TemplateError> {
    *nodes += 1;
    if depth > MAX_BODY_DEPTH || *nodes > MAX_BODY_NODES {
        return Err(TemplateError::BodyNotJson);
    }
    match v {
        Value::Object(m) => {
            for (k, child) in m {
                if k.chars().any(is_invisible) {
                    return Err(TemplateError::InvisibleChar);
                }
                // A slot (or any marker pair) in a KEY is never legitimate.
                if k.contains("{{") || k.contains("}}") {
                    return Err(TemplateError::StrayMarker);
                }
                let child_ptr = format!("{ptr}/{}", escape_pointer(k));
                walk_body(cx, child, &child_ptr, depth + 1, nodes, slots)?;
            }
            Ok(())
        }
        Value::Array(a) => {
            for (i, child) in a.iter().enumerate() {
                walk_body(cx, child, &format!("{ptr}/{i}"), depth + 1, nodes, slots)?;
            }
            Ok(())
        }
        Value::String(s) => walk_string(cx, s, ptr, slots),
        _ => Ok(()),
    }
}

fn walk_string(
    cx: &mut Ctx<'_>,
    s: &str,
    ptr: &str,
    slots: &mut Vec<(String, usize)>,
) -> Result<(), TemplateError> {
    // Free text may carry a newline/tab (chat content); everything else
    // invisible is refused outright.
    if s.chars().any(|c| is_invisible(c) && c != '\n' && c != '\t') {
        return Err(TemplateError::InvisibleChar);
    }
    if cx.policy.syntax == SlotSyntax::Single {
        // Single-brace platforms carry no body slots: user text may hold a
        // lone `{`/`}`, but never a marker PAIR.
        return if s.contains("{{") || s.contains("}}") {
            Err(TemplateError::StrayMarker)
        } else {
            Ok(())
        };
    }
    let pieces = tokenize(cx.policy, s, Braces::Pairs)?;
    match pieces.as_slice() {
        [] => Ok(()),
        [Piece::Slot(idx)] => {
            let idx = *idx;
            let pos = cx.policy.slots[idx]
                .positions
                .iter()
                .copied()
                .find(|p| matches!(p, Position::Body(b) if *b == ptr))
                .ok_or(TemplateError::SlotMisplaced)?;
            cx.require_position(idx, pos)?;
            slots.push((ptr.to_string(), idx));
            Ok(())
        }
        // A slot embedded in (or alongside) free text is refused: a slot is
        // only ever a whole structural value.
        p if p.iter().any(|x| matches!(x, Piece::Slot(_))) => Err(TemplateError::SlotMisplaced),
        _ => Ok(()),
    }
}

fn verify_secret_refs(
    cx: &Ctx<'_>,
    refs: &[(String, String)],
) -> Result<Vec<HeaderCred>, TemplateError> {
    let policy = cx.policy;
    let mut declared: BTreeSet<usize> = BTreeSet::new();
    let mut creds: Vec<HeaderCred> = Vec::new();
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    for (slot, handle) in refs {
        if !seen.insert(slot.as_str()) {
            return Err(TemplateError::SecretRefDuplicate);
        }
        // Header-name convention: ("Authorization", "<handle>").
        let lower = slot.to_ascii_lowercase();
        if let Some(c) = cx.kind.header_creds.iter().find(|c| c.header == lower) {
            if c.handle != handle {
                return Err(TemplateError::SecretRefHandleMismatch);
            }
            creds.push(*c);
            continue;
        }
        let name = match policy.syntax {
            SlotSyntax::Double => slot.strip_prefix("{{").and_then(|r| r.strip_suffix("}}")),
            SlotSyntax::Single => slot.strip_prefix('{').and_then(|r| r.strip_suffix('}')),
        }
        .ok_or(TemplateError::SecretRefUnknown)?;
        let idx = policy
            .slot_index(name)
            .ok_or(TemplateError::SecretRefUnknown)?;
        if !cx.kind.slots.contains(&policy.slots[idx].name) {
            return Err(TemplateError::SecretRefUnknown);
        }
        if policy.slots[idx].handle != handle {
            return Err(TemplateError::SecretRefHandleMismatch);
        }
        declared.insert(idx);
    }
    // Every used non-stamp slot must have been declared by the connector.
    for idx in &cx.used {
        let d = &policy.slots[*idx];
        if d.origin == SlotOrigin::Resolver && !declared.contains(idx) {
            return Err(TemplateError::SecretRefMissing);
        }
    }
    // Every header-convention credential the kind needs must be present.
    for need in &cx.kind.header_creds {
        if !creds.iter().any(|c| c.header == need.header) {
            return Err(TemplateError::SecretRefMissing);
        }
    }
    Ok(creds)
}

#[cfg(test)]
pub(crate) mod tests;
