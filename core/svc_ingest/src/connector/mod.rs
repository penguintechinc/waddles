//! The connector-bundle host capability, ingest side (issue #840 "B0"; spec
//! `docs/superpowers/specs/2026-09-28-connector-bundles.md` S2, S6 phases 0-1).
//!
//! svc_ingest is the **host** of a connector bundle: it owns the socket / poll
//! / webhook, the reconnect discipline and the publish; the WASM connector only
//! ever sees `receiver` calls and returns normalized events.
//!
//! ```text
//!   Transport (host: socket/poll/webhook)         ConnectorRegistry (always-pinned)
//!        |  raw frame bytes                             |  platform -> PinnedConnector
//!        v                                              v
//!   ConnectorDriver --size ceiling--> ConnectorInvoker::on_frame ==> executor (WASM)
//!        |                                  | normalized events
//!        |   cap + platform check <---------'
//!        v
//!   EventSink -> crate::publish::publish_event -> Valkey spine
//! ```
//!
//! | Module | Role |
//! |---|---|
//! | [`transport`] | the host-owned `Transport` trait (one impl per family) |
//! | [`registry`] | always-pinned connector registry + per-platform ceilings |
//! | [`invoker`] | the seam to the executor; [`UnwiredInvoker`] fails loud |
//! | [`driver`] | connect / handshake / pump / reconnect / failure classes |
//! | [`sink`] | publishes via the one `StageEnvelope` construction site |
//! | [`metrics`] | Prometheus series (PII-free, bounded labels) |
//!
//! **B0 ships the skeleton, not a platform.** No real Slack/Kick/Discord
//! transport lives here, and heartbeat scheduling (a dedicated per-connection
//! task, spec S3.5) arrives with the first heartbeat-bearing transport. The
//! production invoker is [`UnwiredInvoker`] until the executor host-API wire
//! gains connector export kinds (an external `penguin-libs` change); the
//! real-path proof against the real executor lives in `core/connector_e2e`.

pub mod driver;
pub mod invoker;
pub mod metrics;
pub mod registry;
pub mod sink;
pub mod transport;
pub mod types;

pub use driver::{ConnectionEnd, ConnectionReport, ConnectorDriver, DriverError};
pub use invoker::{ConnectorInvokeError, ConnectorInvoker, UnwiredInvoker};
pub use metrics::{register_connector_metrics, ConnectorMetrics};
pub use registry::{ConnectorRegistry, PinnedConnector, RegistryError, MAX_EVENTS_PER_FRAME};
pub use sink::{to_platform_event, EventSink, SinkError, SpineEventSink};
pub use transport::{Transport, TransportError};
pub use types::{ConnectionCtx, ConnectorEvent, ConnectorFrameError, Handshake, RawFrame};
