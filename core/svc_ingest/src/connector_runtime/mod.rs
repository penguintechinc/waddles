//! Connector runtime (spec `docs/superpowers/specs/2026-11-05-connector-runtime-inbound-manager.md`
//! sections 0, 1, 3): the production inbound lifecycle manager that turns the
//! hub-api binding feed into supervised, per-community connector connections.
//!
//! | Module | Role |
//! |---|---|
//! | [`feed`] | desired-set types and the [`BindingFeed`] over hub-api bindings |
//! | [`plan`] | the platform-static [`OverlayProfile`] seam and [`RuntimeEnv`] |
//! | [`supervisor`] | the reconcile loop and per-connection supervision |
//! | [`lease`] | the per-connection Valkey lease (`connector:<platform>:<id>`) |
//! | [`observed`] | delegating wrappers recording readiness facts |
//! | [`ready`] | [`RuntimeReady`] / [`Readiness`] and the `/readyz` status |
//! | [`metrics`] | bounded-label Prometheus series (no community/connection ids) |

pub mod feed;
pub mod lease;
pub mod metrics;
pub mod observed;
pub mod plan;
pub mod profiles;
pub mod ready;
pub mod supervisor;
#[cfg(test)]
pub(crate) mod testkit;

pub use feed::{
    BindingFeed, ConnKey, DesiredConnection, DynFeed, FeedError, FeedSet, HubBindingFeed,
    TransportPlan, WebhookPlan,
};
pub use lease::ConnectorValkeyLease;
pub use plan::{OverlayProfile, PlanError, RuntimeEnv};
pub use ready::{Readiness, RuntimeReady};
pub use supervisor::{
    ConnectorRuntime, ResolverSecrets, RuntimeConfig, RuntimeDeps, RuntimeError, SecretsFactory,
    SinkBuildError, SinkFactory, WebhookAttach,
};
