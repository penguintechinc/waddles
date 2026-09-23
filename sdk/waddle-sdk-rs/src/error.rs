//! The single error type a bundle author sees from every `waddle-sdk-rs`
//! capability wrapper. Each variant maps to one of the WIT world's `variant
//! error` shapes (`http::error`, `kv::error`, `db::error`, `relay::error`)
//! plus the SDK's own JSON-boundary failures -- never a bare `String` or an
//! opaque `anyhow::Error`, so a bundle can match on failure kind the same
//! way `waddle-sdk` (Python) callers match on typed exceptions.

use thiserror::Error;

/// Unified error surface for `waddle-sdk-rs`. `Display`/`Error` come from
/// `thiserror` so bundle authors can `?`-propagate or `.to_string()` for a
/// human-readable message without hand-writing `Display` impls.
#[derive(Debug, Error, PartialEq)]
pub enum SdkError {
    /// `payload-json`/`config-json`/`message-json`/`fields-json` failed to
    /// (de)serialize as JSON (spec Assumption A2).
    #[error("JSON boundary error: {0}")]
    PayloadJson(#[from] JsonError),

    /// A `*-json` field was valid JSON but not a JSON *object* -- the WIT
    /// world requires every such field to carry object text.
    #[error("expected a JSON object, got a scalar or array")]
    NonObjectJson,

    /// The guarded outbound `http` import (`interface http`) refused or
    /// failed the request.
    #[error("http error: {0}")]
    Http(#[from] HttpError),

    /// The bundle-scoped `kv` import (`interface kv`) failed.
    #[error("kv error: {0}")]
    Kv(#[from] KvError),

    /// The parameterized SQL `db` import (`interface db`) failed.
    #[error("db error: {0}")]
    Db(#[from] DbError),

    /// The action-stage-only `relay` import (`interface relay`) failed.
    #[error("relay error: {0}")]
    Relay(#[from] RelayError),
}

/// Wraps `serde_json::Error` so [`SdkError`] implements `PartialEq` (the
/// upstream type does not).
#[derive(Debug, Error)]
#[error("{0}")]
pub struct JsonError(pub(crate) String);

impl PartialEq for JsonError {
    fn eq(&self, other: &Self) -> bool {
        self.0 == other.0
    }
}

impl From<serde_json::Error> for JsonError {
    fn from(err: serde_json::Error) -> Self {
        JsonError(err.to_string())
    }
}

impl From<serde_json::Error> for SdkError {
    fn from(err: serde_json::Error) -> Self {
        SdkError::PayloadJson(JsonError::from(err))
    }
}

/// Mirrors WIT `interface http`'s `variant error`.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum HttpError {
    #[error("denied: {0}")]
    Denied(String),
    #[error("request timed out")]
    Timeout,
    #[error("response too large: {0} bytes")]
    TooLarge(u64),
    #[error("rate limited, retry after {0}ms")]
    RateLimited(u32),
    #[error("transport error: {0}")]
    Transport(String),
}

/// Mirrors WIT `interface kv`'s `variant error`.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum KvError {
    #[error("value too large: {0} bytes")]
    TooLarge(u64),
    #[error("backend error: {0}")]
    Backend(String),
}

/// Mirrors WIT `interface db`'s `variant error`.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum DbError {
    #[error("denied: {0}")]
    Denied(String),
    #[error("syntax error: {0}")]
    Syntax(String),
    #[error("conflict: {0}")]
    Conflict(String),
    #[error("statement timed out")]
    Timeout,
    #[error("backend error: {0}")]
    Backend(String),
}

/// Mirrors WIT `interface relay`'s `variant error`.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum RelayError {
    #[error("denied: {0}")]
    Denied(String),
    #[error("backend error: {0}")]
    Backend(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sdk_error_displays_wrapped_http_error() {
        let err = SdkError::Http(HttpError::Denied("egress not granted".to_string()));
        assert_eq!(err.to_string(), "http error: denied: egress not granted");
    }

    #[test]
    fn json_error_equality_is_by_message() {
        let a = JsonError("boom".to_string());
        let b = JsonError("boom".to_string());
        assert_eq!(a, b);
    }

    #[test]
    fn payload_json_from_serde_error_round_trips_message() {
        let serde_err = serde_json::from_str::<serde_json::Value>("{not json").unwrap_err();
        let sdk_err: SdkError = serde_err.into();
        assert!(matches!(sdk_err, SdkError::PayloadJson(_)));
    }

    #[test]
    fn db_error_variants_display_distinctly() {
        assert_eq!(DbError::Timeout.to_string(), "statement timed out");
        assert_eq!(
            DbError::Conflict("dup key".to_string()).to_string(),
            "conflict: dup key"
        );
    }

    #[test]
    fn relay_error_denied_carries_reason() {
        let err = RelayError::Denied("action-stage only".to_string());
        assert_eq!(err.to_string(), "denied: action-stage only");
    }

    #[test]
    fn kv_error_too_large_reports_byte_count() {
        assert_eq!(
            KvError::TooLarge(4096).to_string(),
            "value too large: 4096 bytes"
        );
    }
}
