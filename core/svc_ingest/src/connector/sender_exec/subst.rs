//! Host-side slot substitution.
//!
//! The ONLY code that turns a [`VerifiedTemplate`] into bytes for the wire.
//! Values arrive from the host (stamp / connection resolver / duty), never
//! from the guest or the queue payload, and are encoded for the position they
//! land in: percent-encoded (RFC 3986 unreserved only) in URL path/query
//! positions, JSON-escaped in body positions (the body is rebuilt through
//! `serde_json`, so a quote/newline/backslash in a value cannot break out of
//! its string), header-safe ASCII in header values. Secret values live in
//! [`SlotValue`] (zeroizing, `Debug` redacted) and appear in no error.

use std::collections::BTreeMap;

use serde_json::Value;
use zeroize::Zeroizing;

use super::policy::{PlatformPolicy, SlotShape};
use super::verify::{Piece, VerifiedTemplate};

/// A resolved slot value. Zeroized on drop, never `Debug`-printed.
#[derive(Clone)]
pub struct SlotValue(Zeroizing<String>);

impl SlotValue {
    /// Wraps a resolved value.
    #[must_use]
    pub fn new(value: String) -> Self {
        Self(Zeroizing::new(value))
    }

    pub(crate) fn expose(&self) -> &str {
        self.0.as_str()
    }
}

impl std::fmt::Debug for SlotValue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("SlotValue([REDACTED])")
    }
}

/// Why substitution failed. Carries no value.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum SubstError {
    /// A used slot had no resolved value.
    #[error("a used slot has no resolved value")]
    MissingValue,
    /// A resolved value does not satisfy its slot's shape.
    #[error("a resolved slot value does not satisfy its shape")]
    ShapeInvalid,
    /// The rebuilt URL is not a well-formed URL on the verified origin, or
    /// it contains a dot segment.
    #[error("the rebuilt url is invalid")]
    UrlInvalid,
    /// The rebuilt body exceeds the kind's bound.
    #[error("the rebuilt body is over the size bound")]
    BodyTooLarge,
    /// A body pointer recorded by the verifier no longer resolves.
    #[error("the body slot position is invalid")]
    BodyPosition,
}

impl SubstError {
    /// Stable low-cardinality reason code.
    #[must_use]
    pub fn code(self) -> &'static str {
        match self {
            Self::MissingValue => "missing_value",
            Self::ShapeInvalid => "shape_invalid",
            Self::UrlInvalid => "url_invalid",
            Self::BodyTooLarge => "body_too_large",
            Self::BodyPosition => "body_position",
        }
    }
}

/// The finished request: ready for the guarded client, never loggable.
pub struct PreparedRequest {
    /// HTTP method.
    pub method: String,
    /// Absolute URL on the verified origin.
    pub url: String,
    /// Header name/value pairs (values may hold a credential).
    pub headers: Vec<(String, SlotValue)>,
    /// Serialized JSON body, if the kind has one.
    pub body: Option<Vec<u8>>,
}

impl std::fmt::Debug for PreparedRequest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PreparedRequest")
            .field("method", &self.method)
            .field("url", &"[REDACTED]")
            .field(
                "headers",
                &self.headers.iter().map(|(n, _)| n).collect::<Vec<_>>(),
            )
            .field("body", &self.body.as_ref().map(|_| "[REDACTED]"))
            .finish()
    }
}

/// Percent-encodes everything outside RFC 3986 unreserved
/// (`A-Za-z0-9-._~`), uppercase hex.
#[must_use]
pub fn pct_encode(s: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(char::from(b));
        } else {
            out.push('%');
            out.push(char::from(HEX[usize::from(b >> 4)]));
            out.push(char::from(HEX[usize::from(b & 0x0f)]));
        }
    }
    out
}

/// Checks `v` against `shape`.
pub(crate) fn shape_ok(shape: SlotShape, v: &str) -> bool {
    match shape {
        SlotShape::Id => {
            !v.is_empty()
                && v.len() <= 128
                && v.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        }
        SlotShape::Secret => {
            !v.is_empty() && v.len() <= 4096 && v.bytes().all(|b| (0x21..0x7f).contains(&b))
        }
        #[cfg(test)]
        SlotShape::Opaque => !v.is_empty() && v.chars().count() <= 256 && !v.contains('\0'),
    }
}

/// A value consisting only of dots would form a `.`/`..` path segment.
fn dots_only(v: &str) -> bool {
    v.bytes().all(|b| b == b'.')
}

fn piece_text<'a>(
    policy: &PlatformPolicy,
    pieces: &'a [Piece],
    values: &'a BTreeMap<usize, SlotValue>,
    encode: bool,
) -> Result<String, SubstError> {
    let mut out = String::new();
    for p in pieces {
        match p {
            Piece::Lit(l) => out.push_str(l),
            Piece::Slot(idx) => {
                let v = values.get(idx).ok_or(SubstError::MissingValue)?;
                if !shape_ok(policy.slots[*idx].shape, v.expose()) {
                    return Err(SubstError::ShapeInvalid);
                }
                if encode {
                    if dots_only(v.expose()) {
                        return Err(SubstError::UrlInvalid);
                    }
                    out.push_str(&pct_encode(v.expose()));
                } else {
                    out.push_str(v.expose());
                }
            }
        }
    }
    Ok(out)
}

/// Substitutes `values` (by slot index) and `creds` (by header-convention
/// handle) into `v`.
///
/// # Errors
/// [`SubstError`] -- nothing is returned partially filled.
pub fn render(
    policy: &PlatformPolicy,
    v: &VerifiedTemplate,
    values: &BTreeMap<usize, SlotValue>,
    creds: &BTreeMap<&'static str, SlotValue>,
) -> Result<PreparedRequest, SubstError> {
    let mut url = v.origin.clone();
    for seg in &v.path {
        url.push('/');
        url.push_str(&piece_text(policy, seg, values, true)?);
    }
    for (i, (k, pieces)) in v.query.iter().enumerate() {
        url.push(if i == 0 { '?' } else { '&' });
        url.push_str(k);
        url.push('=');
        url.push_str(&piece_text(policy, pieces, values, true)?);
    }
    let parsed = reqwest::Url::parse(&url).map_err(|_| SubstError::UrlInvalid)?;
    let origin_ok = format!(
        "{}://{}",
        parsed.scheme(),
        parsed
            .host_str()
            .map_or(String::new(), |h| match parsed.port() {
                Some(p) => format!("{h}:{p}"),
                None => h.to_string(),
            })
    ) == v.origin;
    let dot_seg = parsed
        .path_segments()
        .is_some_and(|mut s| s.any(|seg| seg == "." || seg == ".." || seg.is_empty()));
    if !origin_ok || dot_seg || !parsed.username().is_empty() || parsed.fragment().is_some() {
        return Err(SubstError::UrlInvalid);
    }

    let mut headers = Vec::with_capacity(v.headers.len() + v.header_creds.len());
    for (name, pieces) in &v.headers {
        headers.push((
            name.clone(),
            SlotValue::new(piece_text(policy, pieces, values, false)?),
        ));
    }
    for c in &v.header_creds {
        let secret = creds.get(c.handle).ok_or(SubstError::MissingValue)?;
        if !shape_ok(SlotShape::Secret, secret.expose()) {
            return Err(SubstError::ShapeInvalid);
        }
        headers.push((
            c.header.to_string(),
            SlotValue::new(format!("{}{}", c.prefix, secret.expose())),
        ));
    }

    let body = match &v.body {
        None => None,
        Some(tpl) => {
            let mut doc: Value = tpl.clone();
            for (ptr, idx) in &v.body_slots {
                let val = values.get(idx).ok_or(SubstError::MissingValue)?;
                if !shape_ok(policy.slots[*idx].shape, val.expose()) {
                    return Err(SubstError::ShapeInvalid);
                }
                let slot = doc.pointer_mut(ptr).ok_or(SubstError::BodyPosition)?;
                // `serde_json` escapes the string on serialization: quotes,
                // backslashes, newlines and unicode cannot leave the string.
                *slot = Value::String(val.expose().to_string());
            }
            let bytes = serde_json::to_vec(&doc).map_err(|_| SubstError::BodyPosition)?;
            if v.max_body.is_some_and(|m| bytes.len() > m) {
                return Err(SubstError::BodyTooLarge);
            }
            Some(bytes)
        }
    };
    Ok(PreparedRequest {
        method: v.method.clone(),
        url: parsed.to_string(),
        headers,
        body,
    })
}
