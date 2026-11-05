//! The overlay's single error type. It maps 1:1 onto the WIT `frame-error`
//! variants so the wasm glue is a trivial conversion and the host-testable core
//! never depends on generated bindings.

use std::fmt;

/// A failure the overlay reports to the host. Messages are PII-free by
/// construction: they name a field or a class, never frame content.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OverlayError {
    /// The input is valid JSON/shape but names something this overlay refuses
    /// (unknown action kind, foreign platform, unknown interaction type).
    Unsupported(String),
    /// The input is not what Discord's wire format allows.
    Malformed(String),
    /// A host dependency the overlay needs failed (reserved for glue use).
    Backend(String),
}

impl fmt::Display for OverlayError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unsupported(m) => write!(f, "unsupported: {m}"),
            Self::Malformed(m) => write!(f, "malformed: {m}"),
            Self::Backend(m) => write!(f, "backend: {m}"),
        }
    }
}

impl std::error::Error for OverlayError {}

/// Shorthand used throughout the crate.
pub type Result<T> = std::result::Result<T, OverlayError>;
