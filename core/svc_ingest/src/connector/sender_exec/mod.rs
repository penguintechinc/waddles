//! Generic outbound executor (P6a T6d-1): the host half of
//! `sender.build-request`.
//!
//! A platform overlay (`world connector-tools`) shapes a platform request as
//! an `http-request-tpl` carrying only SLOTS -- never a token, never a raw
//! destination. This module is what makes that safe to run:
//!
//! ```text
//!  queue entry ──(drain: pin_sig HMAC + origin_channel == channel, FIRST gate)──┐
//!                                                                                v
//!  FlagGate(waddles.connector-tools) ─ off ─> FlagOff (fail loud, nothing runs)  │
//!  pin re-check ─> TemplateBuilder (pinned component digest) ─> verify::verify ──┤  typed errors,
//!        undeclared slot / `{{` stray / invisible char / userinfo / non-https /  │  never silent
//!        host not declared / header not allowlisted / Authorization from the     │
//!        template / body bounds                                                  │
//!  SlotSource (stamp channel, per-community connection + credential, per call,   │
//!        never cached) ─> subst::render (percent-encode URL, JSON-escape body)   │
//!  GuardedClient (SSRF guard, per-hop revalidation, size/time bounds) ──> wire ──┘
//! ```
//!
//! Pinned-target slots come from the host (the signed origin stamp and the
//! community's connection record); credential slots come from the per-call
//! credential resolution and live only in zeroizing [`SlotValue`]s. No error,
//! log line or metric label ever carries a URL, header value, slot text or
//! secret.
//!
//! Supported kinds (see [`PlatformPolicy`]): `chat.send` and `chat.delete`
//! on Twitch and Discord, `chat.send` on YouTube. `dm.send` is declared but
//! refused with [`ExecError::HostDutyMissing`] until a [`DutyHook`] claims it
//! (T6d-2). With `discord_duties::DiscordDuties` registered, Discord `dm.send`
//! and `role.add`/`role.remove` run as host duties.

pub mod discord_duties;
pub mod discord_slots;
pub mod policy;
pub mod subst;
pub mod twitch_slots;
pub mod verify;
pub mod wire_builder;
pub mod youtube_slots;

use std::collections::{BTreeMap, HashMap};
use std::net::IpAddr;
use std::sync::{Arc, LazyLock};
use std::time::{Duration, Instant};

use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use tracing::Instrument;

pub use policy::{KindPolicy, PlatformPolicy, SlotOrigin};
pub use subst::{PreparedRequest, SlotValue, SubstError};
pub use verify::TemplateError;
pub use wire_builder::WireTemplateBuilder;

use crate::outbound_ops::OutboundAction;
use crate::outbound_pin::PinError;

/// PostHog flag the executor enforces (default OFF, fail closed).
pub const CONNECTOR_TOOLS_FLAG: &str = "waddles.connector-tools";

/// Mirror of WIT `sender.action`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SenderAction {
    /// `chat.send`, `chat.delete`, ...
    pub kind: String,
    /// Opaque binding id the host derived (never a channel/guild name).
    pub target_source_id: String,
    /// Kind-specific JSON object built by the HOST from the queue entry.
    pub payload_json: String,
}

/// Mirror of WIT `sender.http-request-tpl`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestTpl {
    /// HTTP method.
    pub method: String,
    /// URL with slots.
    pub url: String,
    /// Header name/value pairs.
    pub headers: Vec<(String, String)>,
    /// Body bytes.
    pub body: Option<Vec<u8>>,
    /// `(slot, handle)` pairs.
    pub secret_refs: Vec<(String, String)>,
}

/// A template plus the digest of the component that produced it.
#[derive(Clone, Debug)]
pub struct BuiltTemplate {
    /// The guest's template (untrusted until [`verify::verify`]).
    pub tpl: RequestTpl,
    /// `sha256:<hex>` of the pinned component that ran `build-request`.
    pub component_digest: String,
}

/// Why a template could not be built.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum BuildError {
    /// The guest returned a `frame-error` (`unsupported`/`malformed`/`backend`).
    #[error("connector refused to build the request ({0})")]
    Guest(&'static str),
    /// A sandbox containment trip (trap, fuel, deadline, memory).
    #[error("connector sandbox trip ({0})")]
    Sandbox(&'static str),
    /// The pinned component could not be reached.
    #[error("connector build unavailable")]
    Unavailable,
}

/// Runs `sender.build-request` on the pinned component for a platform.
///
/// Production implementation: [`WireTemplateBuilder`] (`sender.build-request`
/// over the executor wire, `penguin-bundle-host` 0.5.0
/// `ExportKind::ConnectorBuildRequest`). It is built but NOT yet wired into
/// boot -- see the "Boot wiring seam" section of [`wire_builder`]. The
/// executor and every test drive it through this seam.
#[allow(async_fn_in_trait)] // service-binary-internal, see outbound.rs::IrcOutbound
pub trait TemplateBuilder: Send + Sync {
    /// Builds the template for `action` on `platform`.
    async fn build_request(
        &self,
        platform: &str,
        action: &SenderAction,
    ) -> Result<BuiltTemplate, BuildError>;
}

/// Feature-flag read. Fail closed: `false` on any doubt.
#[allow(async_fn_in_trait)] // service-binary-internal
pub trait FlagGate: Send + Sync {
    /// `true` only when `flag` is positively ON.
    async fn enabled(&self, flag: &str) -> bool;
}

/// [`FlagGate`] over the PostHog-backed license client; no client => OFF.
pub struct LicenseFlagGate {
    client: Option<Arc<penguin_licensing::LicenseClient>>,
}

impl LicenseFlagGate {
    /// Wraps the (optional) license client.
    #[must_use]
    pub fn new(client: Option<Arc<penguin_licensing::LicenseClient>>) -> Self {
        Self { client }
    }
}

impl FlagGate for LicenseFlagGate {
    async fn enabled(&self, flag: &str) -> bool {
        match &self.client {
            Some(c) => c.flag_enabled(flag).await,
            None => false,
        }
    }
}

/// What the host needs resolved for one send.
#[derive(Debug)]
pub struct SlotRequest<'a> {
    /// `twitch` / `discord` / `youtube`.
    pub platform: &'a str,
    /// Numeric `communities.id` of the entry (`None` = no scope).
    pub community_id: Option<i64>,
    /// The stamp-verified channel (canonical `#login` / snowflake), when the
    /// platform has one. The resolver MUST prove the community owns it.
    pub pinned_channel: Option<&'a str>,
    /// The overlay `secret-refs` handles to resolve (`token`, `client_id`,
    /// `broadcaster_id`, `sender_id`, `youtube.oauth`, ...).
    pub handles: &'a [&'static str],
}

/// Why slot resolution failed. Reasons are stable codes, never content.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum SlotError {
    /// Policy-level refusal (no community scope, no connection, channel not
    /// owned, connection unusable, handle unknown). Not retryable.
    #[error("slot resolution refused: {0}")]
    Refused(&'static str),
    /// Transient failure (resolver unreachable); retryable.
    #[error("slot resolution unavailable: {0}")]
    Unavailable(&'static str),
}

/// Per-community slot / credential resolution, invoked on EVERY send. An
/// implementation must not cache secrets and must never fall back to another
/// community's credential.
#[allow(async_fn_in_trait)] // service-binary-internal
pub trait SlotSource: Send + Sync {
    /// Resolves `req.handles`; every requested handle must be present in the
    /// answer or the call is an error.
    async fn resolve(&self, req: &SlotRequest<'_>)
        -> Result<HashMap<String, SlotValue>, SlotError>;
}

/// Context a [`DutyHook`] works from.
#[derive(Debug)]
pub struct DutyCx<'a> {
    /// Platform slug.
    pub platform: &'a str,
    /// The action being executed.
    pub action: &'a OutboundAction,
    /// Numeric community id of the entry.
    pub community_id: Option<i64>,
    /// Stamp-verified channel, if any.
    pub pinned_channel: Option<&'a str>,
    /// The executor's SSRF-guarded client, for duties that must call the
    /// platform (role list, membership, DM open). The only egress handle a
    /// duty gets.
    pub client: &'a GuardedClient,
}

/// T6d-2 plug-in point: platform host duties that need more than slot
/// substitution -- Discord DM (host opens the DM channel and fills
/// `{dm.channel_id}`; `send_dm_in_community` gate; `{user:<uuid>}` resolved
/// via community-scoped tokenization) and managed-role ops (per-binding role
/// allowlist refusing `ADMINISTRATOR`/`MANAGE_ROLES`/`MANAGE_GUILD`).
#[allow(async_fn_in_trait)] // service-binary-internal
pub trait DutyHook: Send + Sync {
    /// Whether this hook implements `kind` on `platform`.
    fn claims(&self, platform: &str, kind: &str) -> bool;

    /// Policy gate run BEFORE a template is built (e.g.
    /// `send_dm_in_community`).
    async fn gate(&self, cx: &DutyCx<'_>) -> Result<(), ExecError>;

    /// Normalizes the overlay's template BEFORE verification. Runs only for
    /// duty kinds. A hook may rewrite the placeholders and strip the
    /// pseudo-headers that carry exactly the values the HOST put into the
    /// payload (so the verifier still refuses every other placeholder). The
    /// default leaves the template untouched.
    async fn bind_template(
        &self,
        _cx: &DutyCx<'_>,
        _tpl: &mut RequestTpl,
    ) -> Result<(), ExecError> {
        Ok(())
    }

    /// The `payload-json` handed to `build-request` for a claimed kind
    /// (e.g. a `{user:<uuid>}` recipient placeholder).
    async fn payload(&self, cx: &DutyCx<'_>) -> Result<String, ExecError>;

    /// Value for a [`SlotOrigin::Duty`] slot (e.g. the DM channel id from
    /// the reply to the host's own `POST /users/@me/channels`).
    async fn dynamic_slot(
        &self,
        cx: &DutyCx<'_>,
        slot: &str,
        client: &GuardedClient,
    ) -> Result<SlotValue, ExecError>;
}

/// The default hook: claims nothing, so every duty kind is refused loudly.
#[derive(Clone, Copy, Debug, Default)]
pub struct NoDuties;

impl DutyHook for NoDuties {
    fn claims(&self, _platform: &str, _kind: &str) -> bool {
        false
    }

    async fn gate(&self, _cx: &DutyCx<'_>) -> Result<(), ExecError> {
        Err(ExecError::HostDutyMissing)
    }

    async fn payload(&self, _cx: &DutyCx<'_>) -> Result<String, ExecError> {
        Err(ExecError::HostDutyMissing)
    }

    async fn dynamic_slot(
        &self,
        _cx: &DutyCx<'_>,
        _slot: &str,
        _client: &GuardedClient,
    ) -> Result<SlotValue, ExecError> {
        Err(ExecError::HostDutyMissing)
    }
}

/// Every way an execution can end short of success. `Display` is
/// content-free; [`Self::code`] is the stable metric/ack code.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum ExecError {
    /// `waddles.connector-tools` is OFF: the executor refuses to run.
    #[error("connector-tools flag is off")]
    FlagOff,
    /// No policy for this platform.
    #[error("platform has no outbound policy")]
    UnsupportedPlatform,
    /// The platform's policy does not support this kind.
    #[error("kind is not supported on this platform")]
    UnsupportedKind,
    /// The kind needs a host duty (T6d-2) that no hook claims.
    #[error("kind needs a host duty that is not registered")]
    HostDutyMissing,
    /// No component digest is pinned for this platform.
    #[error("no connector digest is pinned for this platform")]
    NotPinned,
    /// The component that built the template is not the pinned one.
    #[error("template was not built by the pinned connector digest")]
    DigestMismatch,
    /// The origin stamp / pin gate refused the destination.
    #[error("destination is not the host-pinned origin")]
    Pin(PinError),
    /// The queue entry cannot be expressed as an overlay payload.
    #[error("outbound entry cannot be expressed as a connector action")]
    BadAction,
    /// The connector could not build the template.
    #[error("{0}")]
    Build(BuildError),
    /// The template was refused by verification.
    #[error("template refused: {0}")]
    Template(TemplateError),
    /// Slot / credential resolution failed.
    #[error("{0}")]
    Slot(SlotError),
    /// Substitution failed.
    #[error("substitution failed: {0}")]
    Subst(SubstError),
    /// The guarded client refused or failed (class only).
    #[error("egress failed ({0})")]
    Egress(&'static str),
    /// The platform answered with a redirect (never followed by policy).
    #[error("platform answered with a redirect")]
    Redirect,
    /// HTTP 429.
    #[error("platform rate limited the request")]
    RateLimited {
        /// `Retry-After`, milliseconds, when the platform gave one.
        retry_after_ms: Option<u64>,
    },
    /// Any other non-2xx status.
    #[error("platform answered http {0}")]
    Upstream(u16),
}

impl ExecError {
    /// Stable low-cardinality code: metric `outcome`, ack code, log field.
    #[must_use]
    pub fn code(&self) -> &'static str {
        match self {
            Self::FlagOff => "flag_off",
            Self::UnsupportedPlatform => "unsupported_platform",
            Self::UnsupportedKind => "unsupported_kind",
            Self::HostDutyMissing => "host_duty_missing",
            Self::NotPinned => "digest_not_pinned",
            Self::DigestMismatch => "digest_mismatch",
            Self::Pin(e) => e.reason(),
            Self::BadAction => "bad_action",
            Self::Build(BuildError::Guest(_)) => "build_refused",
            Self::Build(BuildError::Sandbox(_)) => "build_sandbox",
            Self::Build(BuildError::Unavailable) => "build_unavailable",
            Self::Template(e) => e.code(),
            Self::Slot(SlotError::Refused(r) | SlotError::Unavailable(r)) => r,
            Self::Subst(e) => e.code(),
            Self::Egress(c) => c,
            Self::Redirect => "redirect",
            Self::RateLimited { .. } => "rate_limited",
            Self::Upstream(_) => "upstream_error",
        }
    }

    /// `true` for a refusal decided on the host's own policy before any
    /// request left (maps to `SenderError::Rejected`); `false` for failures
    /// of the attempt itself (maps to `SenderError::Failed`).
    #[must_use]
    pub fn is_policy_refusal(&self) -> bool {
        !matches!(
            self,
            Self::Build(BuildError::Sandbox(_) | BuildError::Unavailable)
                | Self::Slot(SlotError::Unavailable(_))
                | Self::Egress(_)
                | Self::Redirect
                | Self::RateLimited { .. }
                | Self::Upstream(_)
        )
    }

    /// Bounded metric outcome label.
    fn outcome(&self) -> &'static str {
        match self {
            Self::FlagOff => "flag_off",
            Self::UnsupportedPlatform | Self::UnsupportedKind => "unsupported",
            Self::HostDutyMissing => "host_duty_missing",
            Self::NotPinned | Self::DigestMismatch => "digest_refused",
            Self::Pin(_) => "pin_refused",
            Self::BadAction => "bad_action",
            Self::Build(_) => "build_failed",
            Self::Template(_) => "template_refused",
            Self::Slot(_) => "slot_failed",
            Self::Subst(_) => "subst_failed",
            Self::Egress(_) | Self::Redirect => "egress_failed",
            Self::RateLimited { .. } => "rate_limited",
            Self::Upstream(_) => "upstream_error",
        }
    }
}

/// A successful send.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExecOutcome {
    /// Final 2xx status.
    pub status: u16,
}

/// What to execute.
#[derive(Debug)]
pub struct ExecRequest<'a> {
    /// Queue platform (`twitch` / `discord` / `youtube`).
    pub platform: &'a str,
    /// The parsed, signature-verified queue action.
    pub action: &'a OutboundAction,
    /// Producer-stamped numeric community id.
    pub community_id: Option<i64>,
}

/// Configuration failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum ConfigError {
    /// A pinned digest is not `sha256:<64 lowercase hex>`.
    #[error("pinned digest is not sha256:<64 hex>")]
    BadDigest,
}

/// Executor configuration.
#[derive(Debug, Clone)]
pub struct ExecutorConfig {
    policies: Vec<PlatformPolicy>,
    digests: HashMap<String, String>,
    limits: GuardedLimits,
    allow_ips: Vec<IpAddr>,
}

impl ExecutorConfig {
    /// Production defaults: the three overlay policies, no redirects
    /// followed, 10 s deadline, 64 KiB response ceiling, empty SSRF allowlist.
    /// `digests` maps platform to its pinned `sha256:<hex>`.
    ///
    /// # Errors
    /// [`ConfigError::BadDigest`] for a malformed digest.
    pub fn production(digests: HashMap<String, String>) -> Result<Self, ConfigError> {
        Self::new(PlatformPolicy::production_set(), digests)
    }

    /// As [`Self::production`] with explicit policies.
    ///
    /// # Errors
    /// [`ConfigError::BadDigest`] for a malformed digest.
    pub fn new(
        policies: Vec<PlatformPolicy>,
        digests: HashMap<String, String>,
    ) -> Result<Self, ConfigError> {
        for d in digests.values() {
            let ok = d.strip_prefix("sha256:").is_some_and(|h| {
                h.len() == 64 && h.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
            });
            if !ok {
                return Err(ConfigError::BadDigest);
            }
        }
        Ok(Self {
            policies,
            digests,
            limits: GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(10),
                max_response_bytes: 64 * 1024,
            },
            allow_ips: Vec::new(),
        })
    }

    /// Reads `SENDER_EXEC_DIGEST_<PLATFORM>` (`sha256:<hex>`) for each
    /// production platform from `lookup`.
    ///
    /// # Errors
    /// [`ConfigError::BadDigest`] for a malformed digest.
    pub fn from_env_with(lookup: impl Fn(&str) -> Option<String>) -> Result<Self, ConfigError> {
        let mut digests = HashMap::new();
        for p in ["twitch", "discord", "youtube"] {
            if let Some(v) = lookup(&format!("SENDER_EXEC_DIGEST_{}", p.to_ascii_uppercase())) {
                digests.insert(p.to_string(), v);
            }
        }
        Self::production(digests)
    }

    /// Test seam: tweak the guarded-client bounds / SSRF allowlist.
    #[must_use]
    pub fn with_client(mut self, limits: GuardedLimits, allow_ips: Vec<IpAddr>) -> Self {
        self.limits = limits;
        self.allow_ips = allow_ips;
        self
    }
}

static EXEC_TOTAL: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_sender_exec_total",
            "Outbound executor runs, labeled by platform/kind/outcome (ok or a bounded failure class)",
        ),
        &["platform", "kind", "outcome"],
    )
    .expect("valid metric definition")
});

static EXEC_SECONDS: LazyLock<prometheus::HistogramVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_ingest_sender_exec_seconds",
            "Outbound executor wall time (build + verify + resolve + send), by platform/kind",
        ),
        &["platform", "kind"],
    )
    .expect("valid metric definition")
});

/// Registers the executor series on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(EXEC_TOTAL.clone()))?;
    registry.register(Box::new(EXEC_SECONDS.clone()))
}

/// Count of one label set (test/observability helper).
#[must_use]
pub fn exec_count(platform: &str, kind: &str, outcome: &str) -> u64 {
    EXEC_TOTAL
        .with_label_values(&[platform, kind, outcome])
        .get()
}

fn platform_label(p: &str) -> &'static str {
    match p {
        "twitch" => "twitch",
        "discord" => "discord",
        "youtube" => "youtube",
        _ => "other",
    }
}

fn kind_label(k: &str) -> &'static str {
    match k {
        "chat.send" => "chat.send",
        "chat.delete" => "chat.delete",
        "dm.send" => "dm.send",
        "role.add" => "role.add",
        "role.remove" => "role.remove",
        _ => "other",
    }
}

/// The outbound executor. See the module doc.
pub struct SenderExecutor<B, S, F, D = NoDuties> {
    builder: B,
    slots: S,
    flags: F,
    duties: D,
    client: GuardedClient,
    policies: Vec<PlatformPolicy>,
    digests: HashMap<String, String>,
}

impl<B, S, F> SenderExecutor<B, S, F, NoDuties>
where
    B: TemplateBuilder,
    S: SlotSource,
    F: FlagGate,
{
    /// Builds an executor with no host duties registered.
    #[must_use]
    pub fn new(builder: B, slots: S, flags: F, cfg: ExecutorConfig) -> Self {
        Self::with_duties(builder, slots, flags, NoDuties, cfg)
    }
}

impl<B, S, F, D> SenderExecutor<B, S, F, D>
where
    B: TemplateBuilder,
    S: SlotSource,
    F: FlagGate,
    D: DutyHook,
{
    /// Builds an executor with a [`DutyHook`] (T6d-2).
    #[must_use]
    pub fn with_duties(builder: B, slots: S, flags: F, duties: D, cfg: ExecutorConfig) -> Self {
        Self {
            builder,
            slots,
            flags,
            duties,
            client: GuardedClient::new(cfg.limits, cfg.allow_ips),
            policies: cfg.policies,
            digests: cfg.digests,
        }
    }

    /// Executes one queue action end to end.
    ///
    /// # Errors
    /// A typed [`ExecError`]; on any error NOTHING was sent unless the
    /// variant is one of the post-send ones (`Redirect`, `RateLimited`,
    /// `Upstream`, `Egress` transport failure).
    pub async fn execute(&self, req: &ExecRequest<'_>) -> Result<ExecOutcome, ExecError> {
        let platform = platform_label(req.platform);
        let kind = kind_label(req.action.op());
        let span = tracing::info_span!("sender_exec", platform, kind);
        let start = Instant::now();
        let res = self.execute_inner(req).instrument(span).await;
        EXEC_SECONDS
            .with_label_values(&[platform, kind])
            .observe(start.elapsed().as_secs_f64());
        let outcome = match &res {
            Ok(_) => "ok",
            Err(e) => e.outcome(),
        };
        EXEC_TOTAL
            .with_label_values(&[platform, kind, outcome])
            .inc();
        match &res {
            Ok(o) => tracing::debug!(platform, kind, status = o.status, "outbound executed"),
            Err(e) => tracing::warn!(
                platform,
                kind,
                code = e.code(),
                "outbound executor refused or failed"
            ),
        }
        res
    }

    async fn execute_inner(&self, req: &ExecRequest<'_>) -> Result<ExecOutcome, ExecError> {
        // Gate 1: the flag. OFF => the executor does not run at all.
        if !self.flags.enabled(CONNECTOR_TOOLS_FLAG).await {
            return Err(ExecError::FlagOff);
        }
        let policy = self
            .policies
            .iter()
            .find(|p| p.platform == req.platform)
            .ok_or(ExecError::UnsupportedPlatform)?;
        let kind_name = req.action.op();
        let kind = policy.kind(kind_name).ok_or(ExecError::UnsupportedKind)?;
        let digest = self.digests.get(req.platform).ok_or(ExecError::NotPinned)?;

        // Gate 2: the destination. Re-runs `outbound_pin::pin_target` so the
        // executor cannot be handed an unproven channel even by a caller that
        // skipped the drain's check.
        let uses_stamp = policy
            .slots
            .iter()
            .any(|s| s.origin == SlotOrigin::Stamp && kind.slots.contains(&s.name));
        let pinned = pin_channel(req.platform, req.action, uses_stamp)?;
        let cx = DutyCx {
            platform: req.platform,
            action: req.action,
            community_id: req.community_id,
            pinned_channel: pinned.as_deref(),
            client: &self.client,
        };

        let payload_json = if kind.needs_duty {
            if !self.duties.claims(req.platform, kind_name) {
                return Err(ExecError::HostDutyMissing);
            }
            self.duties.gate(&cx).await?;
            self.duties.payload(&cx).await?
        } else {
            default_payload(kind, req.action)?
        };
        let action = SenderAction {
            kind: kind_name.to_string(),
            target_source_id: req
                .community_id
                .map_or_else(|| "unscoped".to_string(), |c| format!("community-{c}")),
            payload_json,
        };
        let mut built = self
            .builder
            .build_request(req.platform, &action)
            .await
            .map_err(ExecError::Build)?;
        if built.component_digest != *digest {
            return Err(ExecError::DigestMismatch);
        }
        if kind.needs_duty {
            self.duties.bind_template(&cx, &mut built.tpl).await?;
        }
        let verified = verify::verify(policy, kind, &built.tpl).map_err(ExecError::Template)?;
        drop(built);

        // Resolve host-supplied values: stamp, per-community resolver, duty.
        let mut handles: Vec<&'static str> = Vec::new();
        for idx in &verified.used {
            let d = &policy.slots[*idx];
            if d.origin == SlotOrigin::Resolver {
                handles.push(d.handle);
            }
        }
        for c in &verified.header_creds {
            handles.push(c.handle);
        }
        let resolved = if handles.is_empty() {
            HashMap::new()
        } else {
            self.slots
                .resolve(&SlotRequest {
                    platform: req.platform,
                    community_id: req.community_id,
                    pinned_channel: pinned.as_deref(),
                    handles: &handles,
                })
                .await
                .map_err(ExecError::Slot)?
        };
        let unresolved = ExecError::Slot(SlotError::Refused("slot_unresolved"));
        let mut values: BTreeMap<usize, SlotValue> = BTreeMap::new();
        for idx in &verified.used {
            let d = &policy.slots[*idx];
            let v = match d.origin {
                SlotOrigin::Stamp => SlotValue::new(
                    pinned
                        .clone()
                        .ok_or(ExecError::Pin(PinError::MissingOrigin))?,
                ),
                SlotOrigin::Resolver => resolved
                    .get(d.handle)
                    .cloned()
                    .ok_or_else(|| unresolved.clone())?,
                SlotOrigin::Duty => self.duties.dynamic_slot(&cx, d.name, &self.client).await?,
            };
            values.insert(*idx, v);
        }
        let mut creds: BTreeMap<&'static str, SlotValue> = BTreeMap::new();
        for c in &verified.header_creds {
            let v = resolved
                .get(c.handle)
                .cloned()
                .ok_or_else(|| unresolved.clone())?;
            creds.insert(c.handle, v);
        }
        drop(resolved);

        let prepared =
            subst::render(policy, &verified, &values, &creds).map_err(ExecError::Subst)?;
        drop((values, creds));
        self.send(prepared).await
    }

    /// The ONLY egress call in the executor: the SSRF-guarded client.
    async fn send(&self, p: PreparedRequest) -> Result<ExecOutcome, ExecError> {
        let method = reqwest::Method::from_bytes(p.method.as_bytes())
            .map_err(|_| ExecError::Egress("method"))?;
        let headers: Vec<(String, String)> = p
            .headers
            .iter()
            .map(|(n, v)| (n.clone(), v.expose().to_string()))
            .collect();
        let resp = self
            .client
            .fetch(method, &p.url, headers, p.body.clone())
            .await
            .map_err(|e| ExecError::Egress(e.class()))?;
        match resp.status {
            200..=299 => Ok(ExecOutcome {
                status: resp.status,
            }),
            300..=399 => Err(ExecError::Redirect),
            429 => Err(ExecError::RateLimited {
                retry_after_ms: retry_after_ms(&resp.headers),
            }),
            s => Err(ExecError::Upstream(s)),
        }
    }
}

/// `Retry-After` in whole seconds -> ms, bounded to one hour; anything else
/// (HTTP-date, junk) is `None`.
fn retry_after_ms(headers: &[(String, String)]) -> Option<u64> {
    headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("retry-after"))
        .and_then(|(_, v)| v.trim().parse::<u64>().ok())
        .map(|s| s.min(3600) * 1000)
}

/// Re-runs the destination pin and returns the canonical channel (`#login`,
/// snowflake, or the YouTube live-chat id the resolver must prove the
/// community owns). A platform the pin gate does not know is only tolerated
/// when the kind uses no stamp slot.
fn pin_channel(
    platform: &str,
    action: &OutboundAction,
    uses_stamp: bool,
) -> Result<Option<String>, ExecError> {
    let (channel, origin) = match action {
        OutboundAction::ChatSend {
            channel,
            origin_channel,
            ..
        }
        | OutboundAction::ChatDelete {
            channel,
            origin_channel,
            ..
        } => (channel.as_str(), origin_channel.as_deref()),
        OutboundAction::DmSend { origin_channel, .. }
        | OutboundAction::RoleOp { origin_channel, .. } => {
            (origin_channel.as_str(), Some(origin_channel.as_str()))
        }
    };
    if !matches!(platform, "twitch" | "discord" | "youtube") {
        return if uses_stamp {
            Err(ExecError::Pin(PinError::UnsupportedPlatform))
        } else {
            Ok(None)
        };
    }
    crate::outbound_pin::pin_target(platform, channel, origin)
        .map(Some)
        .map_err(ExecError::Pin)
}

/// The overlay payload for a plain (non-duty) kind: ONLY the text / message
/// id from the verified queue entry -- never a channel, guild, user or
/// credential field (overlays reject those outright).
fn default_payload(kind: &KindPolicy, action: &OutboundAction) -> Result<String, ExecError> {
    let v = match action {
        OutboundAction::ChatSend { text, .. } => {
            serde_json::json!({ kind.text_key: text })
        }
        OutboundAction::ChatDelete { message_id, .. } => {
            serde_json::json!({ "message_id": message_id })
        }
        OutboundAction::DmSend { .. } | OutboundAction::RoleOp { .. } => {
            return Err(ExecError::BadAction)
        }
    };
    serde_json::to_string(&v).map_err(|_| ExecError::BadAction)
}

#[cfg(test)]
mod discord_tests;
#[cfg(test)]
pub(crate) mod tests;
#[cfg(test)]
mod youtube_tests;
