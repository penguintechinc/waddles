//! Ingest listener contract shared by RTMP/SRT/WHIP.
//!
//! Each concrete listener (owned by a later chunk: `rtmp` S4, `srt` S5,
//! `whip` S6) binds its protocol-specific port and pushes
//! [`IngestSession`]s onto a shared channel for the pipeline supervisor to
//! match against a [`crate::pipeline::model::InputSpec`] and pick up.

pub mod rtmp;
pub mod srt;
pub mod whip;

use tokio::io::AsyncRead;
use tokio::sync::mpsc;

/// Which ingest protocol produced an [`IngestSession`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IngestKind {
    Rtmp,
    Srt,
    Whip,
}

/// A single accepted ingest connection, handed off to the pipeline
/// supervisor. `key` is the protocol-specific routing key (RTMP stream
/// key, SRT stream id, WHIP token) used to match against a
/// [`crate::pipeline::model::InputSpec`].
pub struct IngestSession {
    pub kind: IngestKind,
    pub key: String,
    pub stream: Box<dyn AsyncRead + Send + Unpin>,
    /// The listener's connection/request span, captured with
    /// `tracing::Span::current()` when the session was accepted. The
    /// orchestrator parents its per-session span to it, so one trace covers
    /// the whole ingest -> pipeline -> egress path even though the session
    /// crosses an `mpsc` channel and a `tokio::spawn` boundary (which carry
    /// no tracing context on their own).
    pub span: tracing::Span,
}

impl std::fmt::Debug for IngestSession {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("IngestSession")
            .field("kind", &self.kind)
            .field("key", &self.key)
            .finish_non_exhaustive()
    }
}

/// Runs a protocol-specific ingest listener. `run` takes `self` by value so
/// an implementation can hold owned listener state (bound socket, config).
/// Implementations are expected to loop forever, pushing each accepted
/// connection as an [`IngestSession`] onto `tx`.
pub trait IngestListener: Send {
    /// Runs the listener loop until `tx` is dropped or a fatal error
    /// occurs. A per-connection error must never terminate the loop --
    /// only a listener-level failure (bind error) should return `Err`.
    fn run(
        self,
        tx: mpsc::Sender<IngestSession>,
    ) -> impl std::future::Future<Output = anyhow::Result<()>> + Send;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ingest_session_debug_hides_stream_shows_kind_and_key() {
        let session = IngestSession {
            kind: IngestKind::Rtmp,
            key: "sk_abc123".to_string(),
            stream: Box::new(tokio::io::empty()),
            span: tracing::Span::none(),
        };
        let rendered = format!("{session:?}");
        assert!(rendered.contains("IngestSession"));
        assert!(rendered.contains("Rtmp"));
        assert!(rendered.contains("sk_abc123"));
    }

    #[test]
    fn ingest_kind_equality() {
        assert_eq!(IngestKind::Rtmp, IngestKind::Rtmp);
        assert_ne!(IngestKind::Rtmp, IngestKind::Srt);
        assert_ne!(IngestKind::Srt, IngestKind::Whip);
    }
}
