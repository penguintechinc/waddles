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
//! | [`invoker`] | the seam to the executor (`ConnectorInvoker` + typed errors) |
//! | [`wire_invoker`] | the production invoker: connector exports over the executor host-API wire |
//! | [`driver`] | connect / handshake / pump / reconnect / failure classes |
//! | [`sink`] | publishes via the one `StageEnvelope` construction site |
//! | [`metrics`] | Prometheus series (PII-free, bounded labels) |
//!
//! **B0 ships the skeleton, not a platform.** No real Slack/Kick/Discord
//! transport lives here. Heartbeat scheduling (a dedicated per-connection
//! loop, spec S3.5) lives in [`driver`], dormant until a transport declares
//! `Transport::heartbeat_interval` AND the driver is opted in. The
//! production invoker is [`WireConnectorInvoker`], carrying connector exports
//! over the executor host-API wire (`penguin-bundle-host` 0.3.0
//! `ExportKind::Connector*`); its real-path proof against the real executor
//! lives in `core/connector_e2e`.

pub mod driver;
pub(crate) mod egress_guard;
pub mod gateway_rules;
pub mod handshake_gate;
pub mod host_api_listener;
pub mod host_calls;
pub mod identify_budget;
pub mod invoker;
pub mod lifecycle;
#[cfg(test)]
mod lifecycle_tests;
pub mod metrics;
pub mod poll;
pub(crate) mod ratelimit;
pub mod reconcile_hook;
pub mod rediscover;
pub mod registry;
pub mod secrets;
pub mod sender_exec;
pub mod sink;
pub mod spectrum;
pub mod spectrum_org;
pub mod transport;
pub(crate) mod transport_common;
pub mod types;
pub mod webhook;
pub mod webhook_verify;
pub mod wire_invoker;
pub mod wix_jwt;
pub mod ws;

pub use driver::{
    ConnectionEnd, ConnectionReport, ConnectorDriver, DriverError, RunEnd,
    DEFAULT_HEARTBEAT_SEND_TIMEOUT, MAX_HEARTBEAT_INTERVAL, MIN_HEARTBEAT_INTERVAL,
};
pub use gateway_rules::{HandshakeAfter, HeartbeatAck, ReconnectUrlCapture};
pub use handshake_gate::{GateCtx, GateError, GatePermit, HandshakeGate, NoopGate};
pub use host_calls::{
    ActiveConnections, CallScope, ConnectorHostCalls, HostCallError, HostCallHandler,
};
pub use identify_budget::{
    BotIdentity, BudgetError, DiscordIdentifyGate, GatewayBotSource, GatewayInfoSource,
    IdentifyBudget, IdentifyBudgetMetrics, RedisIdentifyBudget, SessionStartLimit,
};
pub use invoker::{ConnectorInvokeError, ConnectorInvoker};
pub use lifecycle::{
    CaptureRule, CloseCodeRule, FrameDirective, FrameMatch, HeartbeatCapture, HttpAction,
    HttpErrorRule, IntervalHint, LifecycleAction, LifecycleError, PollLifecycle, QuerySecret,
    Scalar, SessionEngine, SessionRules,
};
pub use metrics::{register_connector_metrics, ConnectorMetrics};
pub use poll::{PollAuth, PollConfig, PollRequest, PollTransport};
pub use rediscover::{Rediscover, RediscoverConfig, RediscoverError, RediscoverOutcome};
pub use registry::{
    ConnectorRegistry, EventLimits, PinnedConnector, RegistryError, MAX_EVENTS_PER_FRAME,
};
pub use secrets::{TransportSecretSource, TransportSecrets};
pub use sink::{
    check_event_limits, is_valid_event_type, to_platform_event, EventSink, SinkError,
    SpineEventSink,
};
pub use transport::{FrameDropReason, FrameOutcome, Transport, TransportError};
pub use transport_common::FrameRuleMetrics;
pub use types::{ConnectionCtx, ConnectorEvent, ConnectorFrameError, Handshake, RawFrame};
pub use wire_invoker::{AttachError, ExecutorLink, WireConnectorInvoker};
pub use ws::{AutoReplyRule, FrameRuleError, SelfEchoFilter, WsConfig, WsTransport};
