//! Host-side mirrors of `waddle:connector@1.0.0`'s `types` interface
//! (`wit/waddle-connector/connector.wit`). Deliberately plain Rust structs, not
//! the executor's wasmtime-generated types: `svc_ingest` never links wasmtime
//! (the executor is a separate credential-less sandbox process), so the
//! invocation seam ([`super::ConnectorInvoker`]) speaks these.

/// Immutable, host-constructed connection context. Never guest-influenced
/// (same confused-deputy rule as the executor's `InvokeScope`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnectionCtx {
    /// Platform slug; must equal the transport's registered platform.
    pub platform: String,
    /// Opaque source id -- NEVER a channel/guild name (dataplane-scale
    /// "opaque source ids" rule extends to every value a connector sees).
    pub source_id: String,
    /// Host-assigned, stable per logical session.
    pub connection_id: String,
    /// Opaque RESUME handle for transports that support it; the connector
    /// never parses it, the host is the only reader.
    pub session_token: Option<String>,
}

/// One raw inbound frame, exactly as read off the socket / poll / webhook
/// (a webhook frame has already passed signature + replay + dedup, host-side,
/// BEFORE a connector is ever resolved -- spec S2).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawFrame {
    /// The connection this frame arrived on.
    pub ctx: ConnectionCtx,
    /// The frame bytes, never logged.
    pub bytes: Vec<u8>,
    /// RFC 3339 UTC, millisecond precision, host-stamped arrival time.
    pub received_at: String,
}

/// One normalized event a connector returned (`normalized-event`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnectorEvent {
    /// Platform slug the connector claims; the driver rejects a value that
    /// differs from the registered platform (a connector must never write
    /// into another platform's stream).
    pub platform: String,
    /// Dotted lowercase event namespace.
    pub event_type: String,
    /// Tokenized/opaque actor, or `None` for a system event. Never logged.
    pub actor: Option<String>,
    /// A JSON **object**, serialized. Never logged.
    pub payload_json: String,
    /// RFC 3339 UTC, millisecond precision, `Z` suffix.
    pub occurred_at: String,
}

/// The connector-built handshake (`handshake-payload`). `secret_refs` are
/// `(slot-name, opaque-handle)` pairs the **transport** substitutes at send
/// time, as the literal last step before bytes leave the process -- the
/// resolved credential never enters guest memory (spec S2.1).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Handshake {
    /// Bytes with the handles still in place.
    pub bytes: Vec<u8>,
    /// `(slot-name, handle)` pairs; empty for a handshake needing no secret.
    pub secret_refs: Vec<(String, String)>,
}

/// A connector-reported frame failure (`frame-error`): the connector said
/// this *frame* is bad. Distinct from a sandbox trip, which is the host
/// containing a connector *fault*.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConnectorFrameError {
    /// The connector does not handle this frame kind.
    Unsupported(String),
    /// The frame is structurally invalid.
    Malformed(String),
    /// The connector hit a backend problem.
    Backend(String),
}

impl ConnectorFrameError {
    /// Stable, low-cardinality label for metrics.
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Unsupported(_) => "unsupported",
            Self::Malformed(_) => "malformed",
            Self::Backend(_) => "backend",
        }
    }
}
