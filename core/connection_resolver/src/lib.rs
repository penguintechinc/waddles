//! Shared per-send connection resolver (edge-reshape Chunk 2).
//!
//! The public API of this crate is the contract svc-action and svc-ingest build on:
//! [`ConnectionResolver::resolve`] makes one authenticated hub-api call per invocation
//! (a community-bound machine JWT; no credential is ever cached) and returns a
//! [`ResolvedConnection`] whose secrets zeroize on drop and never appear in `Debug`.
//! [`ConnectionResolver::list_bindings`] enumerates what the data plane should run
//! (no secrets, unbound `connections:list` token). Failures are a closed taxonomy
//! ([`ResolveError`]) carrying fixed codes only -- never a hub response body.

mod breaker;
mod client;
mod error;
mod secret;
mod types;

pub use client::{
    ConnectionResolve, ConnectionResolver, ResolverConfig, ResolverMetrics, SCOPE_CONNECTIONS_LIST,
    SCOPE_CONNECTIONS_RESOLVE, SCOPE_CONNECTIONS_RESOLVE_STATIC,
};
pub use error::{NotUsableReason, ResolveError};
pub use secret::SecretString;
pub use types::{
    BindingPage, BindingSummary, ConnectionKind, ConnectionRef, ConnectionSecret, Endpoint,
    ResolvedConnection,
};
