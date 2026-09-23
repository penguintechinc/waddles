//! The executor's internal error surface. Every fallible operation in this
//! crate returns `Result<T, ExecutorError>` (or a narrower error that
//! converts into it) -- `rules/general.md` forbids `.unwrap()`/`.expect()`
//! outside tests, so every failure path here is a typed variant rather
//! than a panic.

use penguin_bundle_host::wire::FrameError;

/// Every way this binary's own logic (as opposed to a bundle's guest code,
/// which fails through WIT `result<_, error>` types instead) can fail.
#[derive(Debug, thiserror::Error)]
pub enum ExecutorError {
    /// A `read_frame`/`write_frame` call failed -- fatal for the
    /// connection it occurred on (spec SS6.6).
    #[error("wire protocol error: {0}")]
    Wire(#[from] FrameError),

    /// The stage sent (or the executor sent and awaited a reply for) a
    /// correlation id that could not be matched.
    #[error("correlation error: {0}")]
    Correlation(#[from] penguin_bundle_host::wire::CorrelationError),

    /// The peer sent a frame kind this side did not expect in the current
    /// protocol state (e.g. a `load` before `hello-ok`).
    #[error("unexpected frame kind: {0}")]
    UnexpectedFrame(&'static str),

    /// A `wasmtime::Error` from engine construction, component
    /// compilation/instantiation, or a call into the guest.
    #[error("wasmtime error: {0}")]
    Wasmtime(String),

    /// The fetched component's bytes did not hash to the digest the stage
    /// sent in `load` (spec SS7.6): "Any mismatch -> error.code =
    /// DIGEST_MISMATCH, the previous version keeps serving".
    #[error("digest mismatch: expected {expected}, got {actual}")]
    DigestMismatch { expected: String, actual: String },

    /// A `load`'s digest string was not the `sha256:<64 hex>` shape.
    #[error("malformed digest {0:?}: expected `sha256:<64 hex chars>`")]
    MalformedDigest(String),

    /// The bundle export the stage invoked (`transform`/`dispatch`) is not
    /// present on the currently loaded component.
    #[error("export missing: {0}")]
    ExportMissing(&'static str),

    /// The per-call epoch deadline elapsed before the guest returned.
    #[error("call deadline exceeded after {0}ms")]
    DeadlineExceeded(u64),

    /// I/O failure dialing or reading/writing the stage connection.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    /// A host-call's JSON args/result did not match the shape the
    /// capability's Host trait implementation expected.
    #[error("malformed host-call payload for {capability}::{op}: {detail}")]
    MalformedHostCall {
        capability: &'static str,
        op: &'static str,
        detail: String,
    },

    /// The stage replied to a host-call with an error body.
    #[error("host-call {capability}::{op} denied by stage: {code}: {message}")]
    HostCallDenied {
        capability: &'static str,
        op: &'static str,
        code: String,
        message: String,
    },

    /// The bridge's connection to the stage is gone (closed/reconnecting);
    /// a host call issued in this window cannot be serviced.
    #[error("stage connection unavailable")]
    ConnectionUnavailable,

    /// Configuration failed to load or validate.
    #[error("configuration error: {0}")]
    Config(String),
}

/// `wasmtime::Error` does not implement `std::error::Error` in this
/// pinned version, so it cannot use `#[from]` directly -- see this
/// crate's `README`-equivalent note in `Cargo.toml`. This helper is the
/// single place that conversion happens.
impl From<wasmtime::Error> for ExecutorError {
    fn from(err: wasmtime::Error) -> Self {
        ExecutorError::Wasmtime(err.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wasmtime_error_converts_with_its_message_preserved() {
        let wasmtime_err = wasmtime::Error::msg("boom");
        let err: ExecutorError = wasmtime_err.into();
        assert!(matches!(err, ExecutorError::Wasmtime(ref m) if m == "boom"));
        assert_eq!(err.to_string(), "wasmtime error: boom");
    }

    #[test]
    fn digest_mismatch_display_names_both_values() {
        let err = ExecutorError::DigestMismatch {
            expected: "a".to_string(),
            actual: "b".to_string(),
        };
        assert_eq!(err.to_string(), "digest mismatch: expected a, got b");
    }
}
