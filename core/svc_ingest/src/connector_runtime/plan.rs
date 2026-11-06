//! The platform-static overlay profile seam (spec 1.1/1.2). A profile is
//! compiled into svc-ingest: URLs, slot prefixes and gateway rules are host
//! constants and never come from guest or binding data.

use std::net::IpAddr;

use connection_resolver::BindingSummary;

use super::feed::DesiredConnection;

/// Why a binding could not become a desired connection. Carries only a fixed
/// description, never a handle or any binding value (PII-free by construction).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum PlanError {
    /// The binding belongs to a different provider than the profile serves.
    #[error("binding provider does not match the profile")]
    WrongProvider,
    /// The binding is not the connection kind the profile needs.
    #[error("binding kind is not supported by the profile")]
    WrongKind,
    /// A required metadata key is absent.
    #[error("binding metadata is missing required key `{0}`")]
    MissingKey(&'static str),
    /// A metadata value failed validation (the value is never echoed).
    #[error("binding metadata key `{0}` is invalid")]
    InvalidValue(&'static str),
    /// The platform cannot run yet: its fleet-wide identify budget is not
    /// wired (Discord). Skipped loudly, never run unbudgeted.
    #[error("identify budget is not ready")]
    IdentifyBudgetNotReady,
    /// The derived configuration was rejected (fixed reason, no values).
    #[error("derived connector config rejected: {0}")]
    Config(&'static str),
}

/// Process-level static configuration handed to every profile.
#[derive(Debug, Clone)]
pub struct RuntimeEnv {
    /// Deployment environment label (`alpha`, `beta`, ...).
    pub environment: String,
    /// Frame-size ceiling applied to the derived transport configs.
    pub max_frame_bytes: usize,
    /// Exact IPs granted despite the SSRF denylist (empty in production).
    pub egress_allow: Vec<IpAddr>,
    /// Whether the driver heartbeat is enabled (`ingest.connector-heartbeat`).
    pub heartbeat_enabled: bool,
}

/// One platform's static derivation from a hub-api binding to desired
/// connections. Implemented once per overlay platform (twitch/discord/youtube).
///
/// `source_key` of any derived `WsConfig`/`PollConfig` MUST identify the
/// upstream resource (Twitch channel, Discord guild, YouTube chat): the feed
/// uses it to fail closed when two communities claim one resource.
pub trait OverlayProfile: Send + Sync {
    /// Platform slug: `twitch` | `discord` | `youtube`.
    fn platform(&self) -> &'static str;
    /// The connector bundle `app_id` (`waddles.core.service.<p>`).
    fn app_id(&self) -> &'static str;
    /// Provider name as hub-api's binding list knows it.
    fn provider(&self) -> &'static str;
    /// Capability the binding list is scoped to (`chat_ingest`).
    fn capability(&self) -> &'static str;
    /// Derives the desired connections for one binding.
    fn plan(
        &self,
        b: &BindingSummary,
        env: &RuntimeEnv,
    ) -> Result<Vec<DesiredConnection>, PlanError>;
}
