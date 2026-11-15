//! Command-latency telemetry shared by `svc_ingest`, `svc_process` and `svc_action`.
//!
//! Owner SLA (2026-11-14): a non-external bot command must have p99 < 1 s from the moment the request
//! hits our FIRST DOOR until the outbound platform call completes. This crate is the common
//! measuring tape:
//!
//! | Module | Role |
//! |---|---|
//! | [`stamp`] | the non-PII stamps in the W3C `tracestate` `waddles` member (t0 + per-hop instants) |
//! | [`first_door`] | task-local "the clock started here" for the single publish funnel; proxy request-start header parsing |
//! | [`metrics`] | per-hop + end-to-end histograms, the over-budget counter, the hop breakdown for the WARN |
//! | [`registry`] | bounded `command` / `bundle_lang` label vocabularies (never user text) |
//! | [`switch`] | the opt-out kill switch `waddles.disable-command-latency-telemetry` |
//! | [`span`] | W3C trace-context parenting + error recording for the per-hop spans |
//! | [`canary`] | synthetic-canary configuration and the exact-match sink rule |
//!
//! Everything here is metadata: nothing is ever used for authorization, and a dead exporter or a
//! malformed stamp can only skip a sample, never fail or slow a request (the record helpers are
//! synchronous, allocation-light and never await).

pub mod canary;
pub mod first_door;
pub mod metrics;
pub mod registry;
pub mod span;
pub mod stage;
pub mod stamp;
pub mod switch;

pub use metrics::{Hop, Outcome, BUDGET};
pub use stamp::{Door, Stamps};
