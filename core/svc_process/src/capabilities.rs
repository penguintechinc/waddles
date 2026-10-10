//! Host capability implementations serviced on the stage side of the
//! host-API connection (spec §7.4): the executor issues a `host-call`
//! frame naming a `capability`/`op`, and this module answers it, replying
//! `host-result`.
//!
//! **Per-invoke scoping (the corrected design -- see `crate::host_api`'s
//! module doc for the full rationale).** `core/svc_action`'s landed M3
//! `StageCapabilities` is constructed once per host-API *connection* with
//! a fixed `(tenant, community, app_id)`, which is wrong the moment a
//! single connection ever serves more than one activation (the
//! then-latent bug the security review flagged). This module's
//! [`StageCapabilities`] is instead constructed **fresh for every
//! `invoke`**, scoped to the specific envelope `crate::execute` is
//! currently servicing, and handed to `crate::host_api::Connection::invoke`
//! so only host-calls carrying *that* invoke's `call_id` are ever answered
//! by it (spec §5.11: "No bundle host call accepts a tenant or community
//! argument at all" -- every one is scope-implicit, taken from the
//! invocation in flight, never from a connection-lifetime default).
//!
//! This landing wires `context`/`clock`/`log` fully (the three
//! capabilities every bundle needs regardless of manifest declarations,
//! spec §6.5's capability table: "Always granted"), plus `kv` -- backed by
//! `bundle_host_kv::KvHost` (the crate shared with `core/svc_action` --
//! see that crate's own module doc for the key-derivation/isolation/quota
//! design) over a direct Valkey connection opened once at startup
//! (`crate::lib::connect_kv`) and cloned into every per-invoke
//! [`StageCapabilities`] this loop constructs (`crate::spine::
//! ProcessDeps::kv_conn`'s doc) -- and `http`, backed by
//! [`StageCapabilities::egress`]/[`HttpEgressCatalog`]'s docs for the
//! shared `bundle_host_http::egress::EgressGuard` pipeline (extracted from
//! `core/svc_action`, PR #459 follow-up). `db` is wired too (PR #498) --
//! structured insert/get/update/delete against a bundle's own
//! `app_core`/`app_community` table via [`DbWiring`], gated by
//! `crate::license::BUNDLE_DB_CAPABILITY_FLAG` (unseen/OFF denies
//! `feature_disabled`); `query` remains unimplemented (see
//! `bundle_host_db`'s crate doc). **Every wired capability, `http` and `db`
//! included, is authorized by `core/bundle_capability_gate::authorize`
//! FIRST** (spec SS5, PR #433) -- superseding this stage's own interim
//! catalog-based capability-gate seam, which remains harmlessly redundant
//! defense-in-depth until retired in a follow-on cleanup. For `http`
//! specifically, the extracted host is classified into its
//! `NetHttpFqdn`/`NetHttpPublicIp`/`NetHttpPrivateIp` permission family
//! (see [`classify_net_http_permission`]) and run through the gate
//! *before* the request ever reaches [`StageCapabilities::handle_http`]'s
//! `EgressGuard` -- the gate is the permission boundary, the guard is the
//! SSRF/allowlist boundary; both are mandatory and neither substitutes for
//! the other. Fail-closed, no kill-switch (spec SS5.2). `flags` remains a
//! documented seam -- see [`StageCapabilities::handle`]'s match arms.
//! `relay` is never granted to a process-stage bundle at all (spec §6.5:
//! "Capability: granted only to action-stage bundles") and is denied
//! unconditionally, not merely unimplemented.
//!
//! A bundle never holds a platform credential or a tenant/community
//! argument (spec §4.3, §5.11): every capability here resolves its own
//! scope from `self`, never from the `args`/`op` the guest supplied.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, RwLock};

use bundle_capability_gate::{
    AppScopedResource, CapabilityGate, Denied, EconomyTarget, HostInvokeScopeBuilder,
    PermissionFamily, PermissionId, Quota, ReputationTarget, ResourceRef, ScopeKind, TenantTier,
};
use bundle_host_db::{
    CapabilitySnapshot as DbCapabilitySnapshot, DbError, DbHost, DbScope, DbValue, PostgresBackend,
    SchemaCache,
};
use bundle_host_economy::{EconomyCaps, EconomyError, EconomyScope, EconomyStore};
use bundle_host_http::egress::{EgressGuard, EgressRuleRow, EgressRuleSource};
use bundle_host_kv::{KvBackend, KvError, KvHost, KvScope};
use bundle_host_reputation::{ReputationCaps, ReputationError, ReputationScope, ReputationStore};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody, HostResultError};

use crate::identity::{
    self, IdentityError, IdentityScope, IdentityWiring, InvocationIdentity, MAX_MENTION_TOKEN_LEN,
};
use crate::license::FeatureGate;

/// The durable rolling-24h absolute-delta caps the store enforces for
/// `reputation.community.write` -- the catalog's own `per_user_daily_abs_max`
/// (per community, user) and `per_scope_daily_abs_max` (per app across the
/// community), so the in-memory gate quota and the durable store caps can
/// never disagree about the ceilings.
fn reputation_caps() -> ReputationCaps {
    match PermissionFamily::ReputationCommunityWrite
        .catalog_entry()
        .default_quota
    {
        Quota::ReputationDelta {
            per_user_daily_abs_max,
            per_scope_daily_abs_max,
            ..
        } => ReputationCaps {
            per_user_daily_abs_max,
            per_scope_daily_abs_max,
        },
        // The catalog entry is statically ReputationDelta-shaped (unit-tested
        // in the gate crate); any other shape denies every write by capping
        // both at zero rather than guessing a number.
        _ => ReputationCaps {
            per_user_daily_abs_max: 0,
            per_scope_daily_abs_max: 0,
        },
    }
}

/// Maps a store [`ReputationError`] onto the `{code, message}` wire error the
/// executor decodes back into the WIT `reputation.error` variant
/// (`bundle_executor::host::stage_next_imports`).
fn reputation_error_to_host(err: ReputationError) -> HostResultError {
    if let ReputationError::Backend(detail) = &err {
        // Detail may carry DB internals; log it, never hand it to a guest.
        tracing::error!(error = %detail, "reputation store backend error");
        return denied("backend", "reputation store backend error");
    }
    denied(err.wire_code(), err.to_string())
}

/// The durable rolling-24h aggregate caps the store enforces for a
/// money-moving economy permission -- the catalog's own
/// `per_user_daily_abs_max` / `per_scope_daily_abs_max`, so the gate's
/// in-memory quota and the store's durable caps can never disagree about the
/// ceilings. Any non-`EconomyAmount` shape (a wiring bug, unit-tested away in
/// the gate crate) caps both at zero, refusing every call rather than guessing.
fn economy_caps(family: PermissionFamily) -> EconomyCaps {
    match family.catalog_entry().default_quota {
        Quota::EconomyAmount {
            per_user_daily_abs_max,
            per_scope_daily_abs_max,
            ..
        } => EconomyCaps {
            per_user_daily_max: per_user_daily_abs_max,
            per_scope_daily_max: per_scope_daily_abs_max,
        },
        _ => EconomyCaps {
            per_user_daily_max: 0,
            per_scope_daily_max: 0,
        },
    }
}

/// Maps a store [`EconomyError`] onto the `{code, message}` wire error the
/// executor decodes back into the WIT `economy.error` variant
/// (`bundle_executor::host::stage_next_economy`). `insufficient_funds` /
/// `over_cap` carry their number as the bare-decimal message.
fn economy_error_to_host(err: EconomyError) -> HostResultError {
    if let EconomyError::Backend(detail) = &err {
        // Detail may carry DB internals; log it, never hand it to a guest.
        tracing::error!(error = %detail, "economy store backend error");
        return denied("backend", "economy store backend error");
    }
    denied(err.wire_code(), err.wire_message())
}

/// Maps an [`IdentityError`] onto the `{code, message}` wire error the
/// executor decodes back into the WIT `identity.error` variant
/// (`bundle_executor::host::stage_next_identity`). Messages are the fixed,
/// PII-free constants of [`IdentityError`]; a backend failure's detail is
/// logged (by `crate::identity`) and never handed to a guest.
fn identity_error_to_host(err: IdentityError) -> HostResultError {
    if let IdentityError::Backend(_) = &err {
        return denied("backend", "identity store backend error");
    }
    denied(err.wire_code(), err.to_string())
}

/// Reads a required positive-or-zero integer argument as an `i64` -- the
/// WIT `u64` amounts arrive as JSON numbers; anything non-integer, negative or
/// above `i64::MAX` (the `BIGINT` balance ceiling) is `invalid_args`.
fn economy_amount_arg(
    args: &serde_json::Value,
    key: &str,
    min: i64,
) -> Result<i64, HostResultError> {
    args.get(key)
        .and_then(|v| v.as_u64())
        .and_then(|v| i64::try_from(v).ok())
        .filter(|v| *v >= min)
        .ok_or_else(|| {
            denied(
                "invalid_args",
                format!("{key} must be an integer >= {min} and <= {}", i64::MAX),
            )
        })
}

/// Reads a required UUID-string argument.
fn economy_user_arg(args: &serde_json::Value, key: &str) -> Result<uuid::Uuid, HostResultError> {
    args.get(key)
        .and_then(|v| v.as_str())
        .ok_or_else(|| denied("invalid_args", format!("{key} must be a UUID string")))
        .and_then(|s| {
            uuid::Uuid::parse_str(s)
                .map_err(|_| denied("invalid_args", format!("{key} must be a UUID string")))
        })
}

/// Maps a gate [`Denied`] onto the `{code, message}` shape every `host-call`
/// error reply carries (spec SS5.4's stable `reason` vocabulary).
fn denied_from_gate(err: Denied) -> HostResultError {
    denied(err.reason_str(), err.to_string())
}

/// Answers one `host-call` for a given `capability`/`op`. Object-safe (a
/// manually-boxed future rather than `async fn` in a trait) so the host-API
/// connection can hold `Arc<dyn CapabilityHandler>` without an `async_trait`
/// dependency. Identical shape to `core/svc_action::capabilities::
/// CapabilityHandler` -- kept as a separate trait per crate rather than a
/// shared one in `penguin-bundle-host`, since the two stages' capability
/// sets differ (spec §6.5's grant table).
pub trait CapabilityHandler: Send + Sync {
    /// Services one host call, returning the capability-specific JSON
    /// result or a `{code, message}` error the executor forwards to the
    /// guest as `error-code::access-denied`/`internal-error` per the WIT
    /// world's `host-error` variant (spec §6.5).
    fn handle<'a>(
        &'a self,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>>;
}

fn denied(code: &str, message: impl Into<String>) -> HostResultError {
    HostResultError {
        code: code.to_string(),
        message: message.into(),
    }
}

/// This crate's own sanity bound on a bundle's `log` host-call message
/// length -- same rationale and value as `core/svc_action::capabilities::
/// MAX_BUNDLE_LOG_MESSAGE_LEN`: the spec (§5.12/§7.4) does not set one, so
/// this caps unbounded memory/log-volume from a single `host-call`.
const MAX_BUNDLE_LOG_MESSAGE_LEN: usize = 4096;

/// Strips CR/LF and every other control character -- the same
/// CRLF-injection defense `core/svc_action::capabilities::
/// sanitize_irc_component` applies, reused here for a bundle-supplied log
/// message (process-stage bundles have no `relay` capability to sanitize
/// for, but a log message is still guest-controlled text reaching this
/// process's own log stream).
fn strip_control_chars(value: &str) -> String {
    value.chars().filter(|c| !c.is_control()).collect()
}

/// Sanitizes a bundle-supplied `log` host-call `message` before it ever
/// reaches `tracing` (spec §7.4: "`fields-json` is sanitized with the
/// `penguin-logging` `SENSITIVE_KEYS` rule before anything is emitted").
/// Byte-for-byte the same three-defense pipeline as
/// `core/svc_action::capabilities::sanitize_bundle_log_message`: PII
/// redaction, control-character stripping, then length truncation.
fn sanitize_bundle_log_message(raw_message: &str) -> String {
    let mut wrapped = serde_json::Map::with_capacity(1);
    wrapped.insert(
        "message".to_string(),
        serde_json::Value::String(raw_message.to_string()),
    );
    let sanitized = penguin_logging::sanitize::sanitize_object(&wrapped);
    let sanitized_message = sanitized
        .get("message")
        .and_then(|v| v.as_str())
        .unwrap_or("<empty>");

    let mut message = strip_control_chars(sanitized_message);
    if message.chars().count() > MAX_BUNDLE_LOG_MESSAGE_LEN {
        message = message.chars().take(MAX_BUNDLE_LOG_MESSAGE_LEN).collect();
    }
    message
}

/// Per-`app_id` egress-allowlist source for this stage's `http` capability
/// (`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-
/// gate.md`). This is the SSRF/allowlist boundary [`EgressGuard`]
/// enforces *after* `core/bundle_capability_gate::authorize` (PR #433) has
/// already granted the classified `NetHttpFqdn`/`NetHttpPublicIp`/
/// `NetHttpPrivateIp` permission -- see the module doc for why both checks
/// run, in that order, for every `http` call: **undeclared means denied**.
/// An `app_id` this snapshot has never been told about (every app, today --
/// no writer populates it yet, same honest gap `crate::capabilities`'s own
/// module doc already documents for `db`/`flags`) resolves to `None`,
/// which [`EgressGuard`] treats identically to a declared-but-empty
/// `egress` list: every `http.send` call is refused `host_not_declared`.
/// [`HttpEgressCatalog::update`] is the write side a future DB-driven
/// active-set loader (mirroring `svc_action::bundle_loader`) will call
/// once this stage grows one -- not wired to any real data source in this
/// landing.
#[derive(Default)]
pub struct HttpEgressCatalog(RwLock<HashMap<String, EgressRuleRow>>);

impl HttpEgressCatalog {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    /// Replaces `app_id`'s declared `net.http:<host>` egress allowlist
    /// wholesale (never merges) -- see the type doc for the future writer
    /// this is built for.
    pub fn update(&self, app_id: impl Into<String>, row: EgressRuleRow) {
        self.0
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .insert(app_id.into(), row);
    }
}

impl EgressRuleSource for HttpEgressCatalog {
    fn resolve(&self, app_id: &str) -> Option<EgressRuleRow> {
        self.0
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(app_id)
            .cloned()
    }
}

/// Everything `handle_db` needs, shared across every invocation this
/// process serves -- constructed once at startup (never per-invoke, unlike
/// [`StageCapabilities`] itself) and cloned cheaply into each one via
/// `Arc` (`crate::spine`'s call site). `schemas`/`capabilities` are
/// refreshed by this service's own `bundle_loader` poll (not wired in this
/// landing -- see `bundle_host_db`'s crate doc "remaining work"); an
/// `app_id` neither has ever heard of fails closed by construction
/// ([`DbHost`]'s own resolve-then-authorize order).
#[derive(Clone)]
pub struct DbWiring {
    pub host: Arc<DbHost<PostgresBackend>>,
    pub schemas: Arc<SchemaCache>,
    pub capabilities: Arc<DbCapabilitySnapshot>,
    /// `crate::license::BUNDLE_DB_CAPABILITY_FLAG` gate -- OFF denies every
    /// `db` call `feature_disabled` before any schema/authorize lookup.
    pub flag: Arc<dyn FeatureGate>,
}

/// Live wiring for the `reputation` bundle host capability (issue #726):
/// the durable store plus the opt-in flag. Built once at startup
/// (`crate::lib::try_build_reputation_wiring`) and cloned (cheap, `Arc`) into
/// every per-invoke [`StageCapabilities`], like [`DbWiring`]. `None` on the
/// handler means every `reputation.*` call denies `not_implemented`.
#[derive(Clone)]
pub struct ReputationWiring {
    pub store: Arc<dyn ReputationStore>,
    /// `crate::license::BUNDLE_REPUTATION_CAPABILITY_FLAG` gate -- OFF denies
    /// every call `feature_disabled` before the store is touched.
    pub flag: Arc<dyn FeatureGate>,
}

/// Live wiring for the `economy` bundle host capability (issue #714): the
/// durable store plus the opt-in flag. Built once at startup
/// (`crate::lib::try_build_economy_wiring`) and cloned (cheap, `Arc`) into
/// every per-invoke [`StageCapabilities`], like [`ReputationWiring`]. `None`
/// on the handler means every `economy.*` call denies `not_implemented`.
#[derive(Clone)]
pub struct EconomyWiring {
    pub store: Arc<dyn EconomyStore>,
    /// `crate::license::BUNDLE_ECONOMY_CAPABILITY_FLAG` gate -- OFF denies
    /// every call `feature_disabled` before the store is touched.
    pub flag: Arc<dyn FeatureGate>,
}

/// The real capability implementation this stage wires today, scoped to
/// exactly one invocation's `(tenant, community, app_id)` -- see the
/// module doc for why this is constructed per-invoke, never per-connection.
/// Generic over [`KvBackend`] (defaulted to the production connection
/// type) for the same reason `core/svc_action::capabilities::
/// StageCapabilities` is: `handle_kv`'s argument-parsing/error-mapping is
/// unit-testable against a fake implementing the public
/// `bundle_host_kv::KvBackend` trait, with no live Valkey server -- see
/// this module's `tests::FakeKvBackend`. [`egress`](Self::new) is the one
/// exception to "everything scope-implicit, nothing shared" -- it is a
/// per-*process* singleton (owns rate-limit token buckets keyed by
/// `app_id` across every invoke, spec §8.2 step 8), built once by this
/// stage's own startup wiring and cloned (cheap, `Arc`) into every
/// per-invoke `StageCapabilities`.
pub struct StageCapabilities<K: KvBackend = redis::aio::MultiplexedConnection> {
    tenant: String,
    community: Option<String>,
    app_id: String,
    /// Numeric `tenants.id`/`communities.id` (spec
    /// `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
    /// SS5.1/SS4 `GrantScopeKey`) -- the caller's already-resolved scope
    /// (`crate::spine::ProcessDeps::tenant_id`'s doc), set via
    /// `bundle_active_set::scope::resolve_scope` at startup when
    /// `BUNDLE_SCOPE_TENANT_ID` and a DB reader account are configured, `0`
    /// otherwise (this stage's env-only dispatch mode) -- fails closed
    /// (denies every non-platform permission) rather than silently matching
    /// the wrong tenant's grants.
    tenant_id: i32,
    community_id: i32,
    /// The resolved `app_versions.id` for the caller's configured bundle
    /// version -- see `crate::spine::ProcessDeps::app_version`'s doc. `0`
    /// when no bundle version is configured.
    app_version: i64,
    /// See [`Self::with_kv`]'s doc; `None` until it is called (a bundle
    /// sees `not_implemented` rather than this loop failing to start if
    /// the Valkey connection for `kv` was never configured).
    kv: Option<KvHost<K>>,
    /// The shared, per-process [`EgressGuard`] backing the `http`
    /// capability -- see [`Self::handle_http`].
    egress: Arc<EgressGuard>,
    /// The standard enforcement gate (spec SS5) every arm of
    /// [`CapabilityHandler::handle`] calls first, `http` included (see
    /// the module doc for why both the gate and the egress guard run, in
    /// that order). Mandatory, never bypassable (spec SS5.2 "no
    /// kill-switch").
    gate: Arc<CapabilityGate>,
    /// `None` until `crate::lib`'s startup wiring provisions a live
    /// Postgres pool + flag client -- every `db` call denies
    /// `feature_disabled` in that state, never panics (mirrors every other
    /// unimplemented-seam capability's fail-closed default in
    /// [`Self::handle`]).
    db: Option<DbWiring>,
    /// `None` until [`Self::with_reputation`] -- every `reputation.*` call
    /// denies `not_implemented` in that state (never panics, never a silent
    /// default balance).
    reputation: Option<ReputationWiring>,
    /// `None` until [`Self::with_economy`] -- every `economy.*` call denies
    /// `not_implemented` in that state (never panics, never a silent default
    /// balance).
    economy: Option<EconomyWiring>,
    /// `None` until [`Self::with_identity`] -- every `identity.*` call denies
    /// `not_implemented` in that state (never panics, never a default or
    /// guessed identity).
    identity: Option<IdentityWiring>,
    /// The HOST-derived identity facts of THIS invocation (who triggered it,
    /// which mentions its message carried) -- built by `crate::spine` from the
    /// event it delivered, never from guest input. Paired with [`Self::identity`]
    /// by [`Self::with_identity`].
    invocation_identity: Option<Arc<InvocationIdentity>>,
}

impl<K: KvBackend> StageCapabilities<K> {
    /// Builds the capability set for exactly one `invoke` -- `context` and
    /// every other capability are scope-implicit (spec §5.11: "No bundle
    /// host call accepts a tenant or community argument at all"). `kv`
    /// starts unconfigured; see [`Self::with_kv`]. `egress` is the shared,
    /// per-process [`EgressGuard`] -- see the struct doc. `gate` is
    /// mandatory: every arm calls `gate.authorize()` first (spec SS5),
    /// typically backed by `crate::grant_gate::build_production_gate` so
    /// `context`/`clock`/`log` stay granted regardless of what the real
    /// grant tables answer. `tenant_id`/`community_id`/`app_version` are
    /// the caller's already-resolved numeric scope (`crate::spine::
    /// ProcessDeps::tenant_id`'s doc) -- never guessed here, and never
    /// defaulted to `0` internally the way this constructor used to.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        tenant: String,
        community: Option<String>,
        app_id: String,
        tenant_id: i32,
        community_id: i32,
        app_version: i64,
        egress: Arc<EgressGuard>,
        gate: Arc<CapabilityGate>,
    ) -> Self {
        Self {
            tenant,
            community,
            app_id,
            tenant_id,
            community_id,
            app_version,
            kv: None,
            egress,
            gate,
            db: None,
            reputation: None,
            economy: None,
            identity: None,
            invocation_identity: None,
        }
    }

    /// Builds the gate's host-only [`bundle_capability_gate::InvokeScope`]
    /// from this already-trusted per-invoke scope (spec SS5.1:
    /// `HostInvokeScopeBuilder` is the sole constructor). `tenant_tier`
    /// defaults to [`TenantTier::Free`] -- no capability wired in this
    /// stage today reads it (documented rather than silently guessed, same
    /// posture `core/svc_action::capabilities::InvokeScope::gate_scope`
    /// takes).
    fn gate_scope(&self) -> bundle_capability_gate::InvokeScope {
        HostInvokeScopeBuilder::new()
            .tenant_id(self.tenant_id)
            .community_id(self.community_id)
            .app_id(self.app_id.clone())
            .app_version(self.app_version)
            .tenant_tier(TenantTier::Free)
            .build()
            // `app_id` is always non-empty here -- always `deps.app_id`,
            // itself sourced from a live bundle assignment, never guest
            // input, never constructed empty.
            .expect("StageCapabilities::app_id is never empty for a live invocation")
    }

    /// Enables the `kv` capability over `backend` (production:
    /// `redis::aio::MultiplexedConnection`, cloned from
    /// `crate::spine::ProcessDeps::kv_conn` on every invoke -- a cheap
    /// handle clone over one shared connection, not a new socket).
    /// Builder-style so a deployment where the Valkey connection failed to
    /// open at startup can still construct every other capability and
    /// simply skip this call.
    ///
    /// `capabilities` is the manifest-declared-capability snapshot
    /// `bundle_host_kv::authorize::authorize_kv` checks -- the same
    /// `Arc<CapabilitySnapshot>` `crate::bundle_loader`'s DB-driven poll
    /// loop updates every tick.
    pub fn with_kv(
        mut self,
        backend: K,
        capabilities: std::sync::Arc<bundle_host_kv::CapabilitySnapshot>,
    ) -> Self {
        self.kv = Some(KvHost::new(backend, capabilities));
        self
    }

    /// Attaches the `db` capability's live wiring -- called once at
    /// startup (`crate::lib`) with the shared, process-wide [`DbWiring`],
    /// never per-invoke. Builder-style so existing `StageCapabilities::new`
    /// call sites (including every current test) are unaffected.
    pub fn with_db(mut self, db: DbWiring) -> Self {
        self.db = Some(db);
        self
    }

    /// Attaches the `reputation` capability's live wiring -- called once per
    /// invoke with a clone of the process-wide [`ReputationWiring`].
    pub fn with_reputation(mut self, reputation: ReputationWiring) -> Self {
        self.reputation = Some(reputation);
        self
    }

    /// Attaches the `economy` capability's live wiring -- called once per
    /// invoke with a clone of the process-wide [`EconomyWiring`].
    pub fn with_economy(mut self, economy: EconomyWiring) -> Self {
        self.economy = Some(economy);
        self
    }

    /// Attaches the `identity` capability's live wiring plus THIS invocation's
    /// host-derived identity facts -- called once per invoke with a clone of the
    /// process-wide [`IdentityWiring`] and an [`InvocationIdentity`] built from
    /// the event being delivered. The two are inseparable by construction: the
    /// wiring says HOW to resolve, the invocation says WHO.
    pub fn with_identity(
        mut self,
        identity: IdentityWiring,
        invocation: Arc<InvocationIdentity>,
    ) -> Self {
        self.identity = Some(identity);
        self.invocation_identity = Some(invocation);
        self
    }

    /// `identity.resolve_actor` / `identity.resolve_mention`
    /// (`wit/waddle-bundle/stage.wit` `interface identity`), carried as
    /// `capability = db`, `op = "identity.*"` (`penguin-bundle-host`'s closed
    /// `CapabilityKind` has no identity member; see the WIT doc).
    ///
    /// Same fail-loud order as [`Self::handle_economy`]: (1) argument parsing --
    /// `resolve_mention` needs a bounded, clean `token` string, `resolve_actor`
    /// takes none (and nothing in `args` can name whose identity to resolve:
    /// the actor is host-derived); (2) the activation must be community-scoped
    /// with a resolved numeric tenant AND community; (3) **the gate authorizes
    /// FIRST** (`identity.resolve`: grant, rate limit, instance policy) so an
    /// ungranted call reports `not_granted`, never `not_implemented`/
    /// `feature_disabled`; (4) wiring/flag state; (5) the resolution itself
    /// ([`identity::resolve_actor`] / [`identity::resolve_mention`]). Scope
    /// (tenant/community) is always `self`'s host-derived scope, never an
    /// argument. The success value is `{"user": "<uuid>"}` and nothing else.
    async fn handle_identity(
        &self,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostResultError> {
        enum Op<'a> {
            Actor,
            Mention(&'a str),
        }
        let op = match call.op.as_str() {
            "identity.resolve_actor" => Op::Actor,
            "identity.resolve_mention" => {
                let token = call
                    .args
                    .get("token")
                    .and_then(|v| v.as_str())
                    .filter(|t| !t.is_empty() && t.len() <= MAX_MENTION_TOKEN_LEN)
                    .ok_or_else(|| {
                        denied(
                            "invalid_args",
                            format!("token must be a string of 1..={MAX_MENTION_TOKEN_LEN} bytes"),
                        )
                    })?;
                Op::Mention(token)
            }
            other => {
                return Err(denied(
                    "unknown_op",
                    format!("identity op {other:?} not supported"),
                ))
            }
        };

        if self.tenant_id == 0 || self.community_id == 0 || self.community.is_none() {
            return Err(denied(
                "invalid_args",
                "identity is community-scoped; this activation has no resolved tenant/community",
            ));
        }

        self.gate
            .authorize(
                &self.gate_scope(),
                PermissionId::IdentityResolve,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;

        let Some(wiring) = &self.identity else {
            return Err(denied(
                "not_implemented",
                "identity capability is not provisioned in this deployment \
                 (no DB reader account configured for the stage)",
            ));
        };
        if !wiring.flag.enabled().await {
            return Err(denied(
                "feature_disabled",
                "identity capability is disabled (waddles.bundle-identity-capability is OFF)",
            ));
        }
        let Some(invocation) = &self.invocation_identity else {
            return Err(denied(
                "not_implemented",
                "this invocation carries no triggering-event identity to resolve",
            ));
        };

        let scope = IdentityScope {
            tenant_id: self.tenant_id,
            community_id: self.community_id,
        };
        let user = match op {
            Op::Actor => identity::resolve_actor(wiring, invocation, scope).await,
            Op::Mention(token) => identity::resolve_mention(wiring, invocation, scope, token).await,
        }
        .map_err(identity_error_to_host)?;
        Ok(serde_json::json!({ "user": user.to_string() }))
    }

    /// `economy.balance` / `economy.wager` / `economy.transfer` /
    /// `economy.max_bet` / `economy.leaderboard` (`wit/waddle-bundle/stage.wit`
    /// `interface economy`), carried as `capability = db`, `op = "economy.*"`
    /// (`penguin-bundle-host`'s closed `CapabilityKind` has no economy member
    /// yet; see the WIT doc).
    ///
    /// Same fail-loud order as [`Self::handle_reputation`]: (1) argument
    /// parsing -- users must be UUIDs, amounts positive in-range integers (a
    /// transfer to oneself is malformed); (2) the community must be a real one;
    /// (3) **the gate authorizes FIRST** with an `EconomyScoped` resource, so
    /// grant, declared `max_bet`/`max_amount`, the economy's own amount quotas,
    /// instance policy and the membership pre-filter (every named user) all run
    /// before anything else can leak state; (4) wiring/flag state; (5) the
    /// store, which re-verifies membership and enforces the per-call cap --
    /// computed HERE from the grant's declared bound clamped to the catalog
    /// ceiling, never from guest input -- and the durable per-user/per-scope
    /// rolling-24h caps ([`economy_caps`]) inside its own atomic write. Scope (tenant/community/
    /// app) is always `self`'s host-derived scope, never an argument.
    async fn handle_economy(
        &self,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostResultError> {
        // Each op carries exactly the users it names, so an op can never run
        // against a missing/defaulted user: the parse below either yields every
        // user an op needs or refuses the call.
        #[derive(Clone, Copy)]
        enum Op {
            Balance(uuid::Uuid),
            MaxBet(uuid::Uuid),
            Wager {
                user: uuid::Uuid,
                stake: i64,
                payout: i64,
            },
            Transfer {
                from: uuid::Uuid,
                to: uuid::Uuid,
                amount: i64,
            },
            Leaderboard(u32),
        }

        let args = &call.args;
        let op = match call.op.as_str() {
            "economy.balance" => Op::Balance(economy_user_arg(args, "user")?),
            "economy.max_bet" => Op::MaxBet(economy_user_arg(args, "user")?),
            "economy.wager" => {
                let user = economy_user_arg(args, "user")?;
                let stake = economy_amount_arg(args, "stake", 1)?;
                let payout = economy_amount_arg(args, "payout", 0)?;
                Op::Wager {
                    user,
                    stake,
                    payout,
                }
            }
            "economy.transfer" => {
                let from = economy_user_arg(args, "from")?;
                let to = economy_user_arg(args, "to")?;
                if from == to {
                    return Err(denied("invalid_args", "cannot transfer to oneself"));
                }
                let amount = economy_amount_arg(args, "amount", 1)?;
                Op::Transfer { from, to, amount }
            }
            "economy.leaderboard" => {
                let limit = args
                    .get("limit")
                    .and_then(|v| v.as_u64())
                    .and_then(|v| u32::try_from(v).ok())
                    .filter(|v| (1..=bundle_host_economy::MAX_LEADERBOARD_LIMIT).contains(v))
                    .ok_or_else(|| {
                        denied(
                            "invalid_args",
                            format!(
                                "limit must be 1..={}",
                                bundle_host_economy::MAX_LEADERBOARD_LIMIT
                            ),
                        )
                    })?;
                Op::Leaderboard(limit)
            }
            other => {
                return Err(denied(
                    "unknown_op",
                    format!("economy op {other:?} not supported"),
                ))
            }
        };

        if self.community_id == 0 || self.community.is_none() {
            return Err(denied(
                "invalid_args",
                "economy is community-scoped; this activation has no community",
            ));
        }

        // `max_bet` describes the WAGER capability's own limit, so it is
        // authorized under `economy.wager` (carrying no amount: it is rate
        // limited, not metered); balance/leaderboard are `economy.read`.
        let (permission, amount, target_user, counterparty) = match op {
            Op::Balance(user) => (PermissionId::EconomyRead, None, Some(user), None),
            Op::Leaderboard(_) => (PermissionId::EconomyRead, None, None, None),
            Op::MaxBet(user) => (PermissionId::EconomyWager, None, Some(user), None),
            Op::Wager { user, stake, .. } => {
                (PermissionId::EconomyWager, Some(stake), Some(user), None)
            }
            Op::Transfer { from, to, amount } => (
                PermissionId::EconomyTransfer,
                Some(amount),
                Some(from),
                Some(to),
            ),
        };
        let family = permission.family();
        let authorized = self
            .gate
            .authorize(
                &self.gate_scope(),
                permission,
                ResourceRef::EconomyScoped(EconomyTarget {
                    target_user,
                    counterparty,
                    amount,
                }),
            )
            .map_err(denied_from_gate)?;

        let Some(wiring) = &self.economy else {
            return Err(denied(
                "not_implemented",
                "economy capability is not provisioned in this deployment \
                 (BUNDLE_ECONOMY_PASSWORD unset or connection failed)",
            ));
        };
        if !wiring.flag.enabled().await {
            return Err(denied(
                "feature_disabled",
                "economy capability is disabled (waddles.bundle-economy-capability is OFF)",
            ));
        }

        let scope = EconomyScope {
            tenant_id: self.tenant_id,
            community_id: self.community_id,
            app_id: self.app_id.clone(),
        };
        let result = match op {
            Op::Balance(user) => wiring
                .store
                .balance(&scope, user)
                .await
                .map(|balance| serde_json::json!({ "balance": balance })),
            Op::MaxBet(user) => {
                // The cap is the grant's declared `max_bet` clamped to the
                // catalog ceiling: the exact number the gate enforces on a
                // wager's stake.
                let cap = family
                    .economy_amount_bound(&authorized.params)
                    .ok_or_else(|| denied("backend", "economy cap unavailable"))?;
                wiring
                    .store
                    .max_bet(&scope, user, cap)
                    .await
                    .map(|max_bet| serde_json::json!({ "max_bet": max_bet }))
            }
            Op::Wager {
                user,
                stake,
                payout,
            } => {
                let cap = family
                    .economy_amount_bound(&authorized.params)
                    .ok_or_else(|| denied("backend", "economy cap unavailable"))?;
                wiring
                    .store
                    .wager(&scope, user, stake, payout, cap, economy_caps(family))
                    .await
                    .map(|balance| serde_json::json!({ "balance": balance }))
            }
            Op::Transfer { from, to, amount } => {
                let cap = family
                    .economy_amount_bound(&authorized.params)
                    .ok_or_else(|| denied("backend", "economy cap unavailable"))?;
                wiring
                    .store
                    .transfer(&scope, from, to, amount, cap, economy_caps(family))
                    .await
                    .map(|()| serde_json::json!({}))
            }
            Op::Leaderboard(limit) => wiring.store.leaderboard(&scope, limit).await.map(|rows| {
                serde_json::json!({
                    "entries": rows
                        .into_iter()
                        .map(|r| serde_json::json!({
                            "user": r.user.to_string(),
                            "balance": r.balance,
                        }))
                        .collect::<Vec<_>>(),
                })
            }),
        };
        result.map_err(economy_error_to_host)
    }

    /// `reputation.get` / `reputation.adjust` (`wit/waddle-bundle/stage.wit`
    /// `interface reputation`), carried as `capability = db`,
    /// `op = "reputation.*"` (`penguin-bundle-host`'s closed `CapabilityKind`
    /// has no reputation member yet; see the WIT doc).
    ///
    /// Order is load-bearing and fail-loud at every step: (1) argument
    /// parsing -- `user` must be a UUID, `delta` a non-zero in-range i32 (a
    /// zero delta consumes no quota yet would append a ledger row per call),
    /// `reason` a machine code; (2) the community must be a real one (a tenant-wide
    /// `community_id == 0` activation has no community to score in); (3)
    /// **the gate authorizes FIRST** with a `ReputationScoped` resource, so
    /// grant, declared delta bounds, per-call/per-user/per-scope quotas,
    /// instance policy and the membership pre-filter all run before anything
    /// else can leak state (an ungranted call reports `not_granted`, never
    /// `not_implemented`/`feature_disabled`); (4) wiring/flag state; (5) the
    /// store, which re-verifies membership and the durable per-user and
    /// per-scope daily caps inside its own transaction. Scope (tenant/community/app) is always
    /// `self`'s host-derived scope, never an argument.
    async fn handle_reputation(
        &self,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostResultError> {
        let is_adjust = match call.op.as_str() {
            "reputation.get" => false,
            "reputation.adjust" => true,
            other => {
                return Err(denied(
                    "unknown_op",
                    format!("reputation op {other:?} not supported"),
                ))
            }
        };

        let user = call
            .args
            .get("user")
            .and_then(|v| v.as_str())
            .ok_or_else(|| denied("invalid_args", "user must be a UUID string"))
            .and_then(|s| {
                uuid::Uuid::parse_str(s)
                    .map_err(|_| denied("invalid_args", "user must be a UUID string"))
            })?;
        let (delta, reason) = if is_adjust {
            let delta = call
                .args
                .get("delta")
                .and_then(|v| v.as_i64())
                .and_then(|v| i32::try_from(v).ok())
                .ok_or_else(|| denied("invalid_args", "delta must be an i32"))?;
            bundle_host_reputation::validate_delta(delta).map_err(reputation_error_to_host)?;
            let reason = call
                .args
                .get("reason")
                .and_then(|v| v.as_str())
                .ok_or_else(|| denied("invalid_args", "reason must be a string"))?;
            bundle_host_reputation::validate_reason(reason).map_err(reputation_error_to_host)?;
            (Some(delta), Some(reason))
        } else {
            (None, None)
        };

        if self.community_id == 0 || self.community.is_none() {
            return Err(denied(
                "invalid_args",
                "reputation is community-scoped; this activation has no community",
            ));
        }

        let permission = if is_adjust {
            PermissionId::ReputationCommunityWrite
        } else {
            PermissionId::ReputationRead
        };
        self.gate
            .authorize(
                &self.gate_scope(),
                permission,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: user,
                    scope_kind: ScopeKind::Community,
                    delta,
                }),
            )
            .map_err(denied_from_gate)?;

        let Some(wiring) = &self.reputation else {
            return Err(denied(
                "not_implemented",
                "reputation capability is not provisioned in this deployment \
                 (BUNDLE_REPUTATION_PASSWORD unset or connection failed)",
            ));
        };
        if !wiring.flag.enabled().await {
            return Err(denied(
                "feature_disabled",
                "reputation capability is disabled (waddles.bundle-reputation-capability is OFF)",
            ));
        }

        let scope = ReputationScope {
            tenant_id: self.tenant_id,
            community_id: self.community_id,
            app_id: self.app_id.clone(),
        };
        let balance = match (delta, reason) {
            (Some(delta), Some(reason)) => {
                wiring
                    .store
                    .adjust(&scope, user, delta, reason, reputation_caps())
                    .await
            }
            _ => wiring.store.get(&scope, user).await,
        }
        .map_err(reputation_error_to_host)?;
        Ok(serde_json::json!({ "balance": balance }))
    }

    /// `http.send` (`wit/waddle-bundle/stage.wit` `interface http`) --
    /// forwards to the shared, per-process [`EgressGuard`] once the caller
    /// (`CapabilityHandler::handle`'s `Http` arm) has already run the
    /// classified permission through `self.gate` (module doc). Identical
    /// wire contract to `core/svc_action::capabilities::StageCapabilities::
    /// handle_http`.
    async fn handle_http(
        &self,
        args: &serde_json::Value,
    ) -> Result<serde_json::Value, HostResultError> {
        self.egress.send(&self.app_id, args).await
    }

    fn handle_clock(&self, op: &str) -> Result<serde_json::Value, HostResultError> {
        self.gate
            .authorize(
                &self.gate_scope(),
                PermissionId::PlatformClock,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        match op {
            "now-millis" => {
                let millis = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as u64)
                    .unwrap_or(0);
                Ok(serde_json::json!(millis))
            }
            "now-rfc3339" => Ok(serde_json::json!(
                chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
            )),
            "monotonic-nanos" => {
                // No process-wide monotonic epoch exists to subtract
                // against; a bundle only ever diffs two calls of its own,
                // for which an arbitrary fixed origin is sufficient and
                // matches `std::time::Instant`'s own "opaque, comparable
                // only to itself" contract.
                static ORIGIN: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
                let origin = *ORIGIN.get_or_init(std::time::Instant::now);
                Ok(serde_json::json!(origin.elapsed().as_nanos() as u64))
            }
            other => Err(denied(
                "unknown_op",
                format!("clock op {other:?} not supported"),
            )),
        }
    }

    fn handle_context(&self) -> Result<serde_json::Value, HostResultError> {
        self.gate
            .authorize(
                &self.gate_scope(),
                PermissionId::PlatformContext,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        // Spec §7.4: "Tenant and community come from the key, never from
        // payload." Never includes a credential or secret.
        Ok(serde_json::json!({
            "tenant": self.tenant,
            "community": self.community,
            "app_id": self.app_id,
        }))
    }

    fn handle_log(&self, args: &serde_json::Value) -> Result<serde_json::Value, HostResultError> {
        self.gate
            .authorize(
                &self.gate_scope(),
                PermissionId::PlatformLog,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        let level = args.get("level").and_then(|v| v.as_str()).unwrap_or("info");
        let raw_message = args
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("<empty>");
        let message = sanitize_bundle_log_message(raw_message);
        let message = message.as_str();

        match level {
            "error" => {
                tracing::error!(app_id = %self.app_id, tenant = %self.tenant, bundle_log = %message, "bundle log")
            }
            "warn" => {
                tracing::warn!(app_id = %self.app_id, tenant = %self.tenant, bundle_log = %message, "bundle log")
            }
            "debug" => {
                tracing::debug!(app_id = %self.app_id, tenant = %self.tenant, bundle_log = %message, "bundle log")
            }
            _ => {
                tracing::info!(app_id = %self.app_id, tenant = %self.tenant, bundle_log = %message, "bundle log")
            }
        }
        Ok(serde_json::json!({}))
    }

    /// `kv.get`/`kv.set`/`kv.delete`/`kv.increment` (`wit/waddle-bundle/
    /// stage.wit` `interface kv`). Argument shapes match exactly what
    /// `core/bundle_executor::host::imports`'s `kv::Host` impl sends/
    /// expects -- see `core/svc_action::capabilities::StageCapabilities::
    /// handle_kv`'s identical doc for the wire contract this must not
    /// drift from (byte-for-byte the same parsing/mapping, duplicated
    /// rather than shared only because the two stages' `handle` methods
    /// take `scope` differently -- `self` here vs. a separate parameter
    /// there -- module doc). Tenant/community/app_id come from `self`
    /// (never `call.app_id`).
    async fn handle_kv(&self, call: &HostCallBody) -> Result<serde_json::Value, HostResultError> {
        // Gate call FIRST (spec SS5) -- `storage.kv`, `AppScoped`. This
        // supersedes `bundle_host_kv::authorize::authorize_kv`'s interim
        // always-grant stand-in as the real security boundary; that inner
        // seam remains harmlessly redundant until it is retired in a
        // follow-on cleanup.
        self.gate
            .authorize(
                &self.gate_scope(),
                PermissionId::StorageKv,
                ResourceRef::AppScoped(AppScopedResource::KvState),
            )
            .map_err(denied_from_gate)?;
        let Some(kv) = &self.kv else {
            return Err(denied(
                "not_implemented",
                "kv capability is not configured on this stage (no Valkey connection)",
            ));
        };
        let kv_scope = KvScope::new(
            self.tenant.clone(),
            self.community.clone(),
            self.app_id.clone(),
        );

        match call.op.as_str() {
            "get" => {
                let args: KvKeyArgs = parse_kv_args(&call.args)?;
                let value = kv
                    .get(&kv_scope, call.call_id, &args.key)
                    .await
                    .map_err(kv_err_to_host)?;
                Ok(serde_json::json!({ "value": value }))
            }
            "set" => {
                let args: KvSetArgs = parse_kv_args(&call.args)?;
                kv.set(
                    &kv_scope,
                    call.call_id,
                    &args.key,
                    &args.value,
                    args.ttl_seconds,
                )
                .await
                .map_err(kv_err_to_host)?;
                Ok(serde_json::json!({}))
            }
            "delete" => {
                let args: KvKeyArgs = parse_kv_args(&call.args)?;
                kv.delete(&kv_scope, call.call_id, &args.key)
                    .await
                    .map_err(kv_err_to_host)?;
                Ok(serde_json::json!({}))
            }
            "increment" => {
                let args: KvIncrementArgs = parse_kv_args(&call.args)?;
                let value = kv
                    .increment(
                        &kv_scope,
                        call.call_id,
                        &args.key,
                        args.delta,
                        args.ttl_seconds,
                    )
                    .await
                    .map_err(kv_err_to_host)?;
                Ok(serde_json::json!({ "value": value }))
            }
            other => Err(denied(
                "unknown_op",
                format!("kv op {other:?} not supported"),
            )),
        }
    }

    /// Structured insert/get/update/delete against this app's own
    /// `app_core`/`app_community` table (design doc
    /// `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md`).
    /// `query` is not implemented in this landing -- returns `not_implemented`
    /// (see `bundle_host_db`'s crate doc "remaining work").
    ///
    /// Op/args shape (host-API `{capability, op, args}` dispatch, ahead of
    /// a corresponding `stage.wit`/`bundle_executor` change -- see this
    /// PR's description for the proposed WIT shape):
    /// - `insert`: `args.column_values` = `{col: value, ...}` -> `{row_id, version, columns}`
    /// - `get`: `args.row_id` (string) -> `{row_id, version, columns}`
    /// - `update`: `args.row_id`, `args.expected_version` (u64), `args.column_values` -> `{row_id, version, columns}`
    /// - `delete`: `args.row_id`, `args.expected_version` (u64) -> `{}`
    /// - `query`: `args.limit`/`args.offset` (both optional u32, clamped to
    ///   `MAX_QUERY_LIMIT`) -> `{rows: [{row_id, version, columns}, ...]}`
    ///   -- host-side op ready for the proposed WIT shape, not yet
    ///   guest-reachable in this landing (see this PR's description)
    ///
    /// Every value is a JSON scalar (`null`/bool/number/string); `bytes`
    /// values are not supported over this JSON args shape in this landing.
    async fn handle_db(&self, call: &HostCallBody) -> Result<serde_json::Value, HostResultError> {
        // Gate call FIRST (spec SS5, PR #433), same posture as `http` in
        // `CapabilityHandler::handle` -- checked before the feature-flag/
        // wiring state below so an ungranted call always reports
        // `not_granted`, never `not_implemented`/`feature_disabled`.
        self.gate
            .authorize(
                &self.gate_scope(),
                PermissionId::StorageTables,
                ResourceRef::AppScoped(AppScopedResource::Table),
            )
            .map_err(denied_from_gate)?;
        let Some(db) = &self.db else {
            return Err(denied(
                "not_implemented",
                "db capability is not wired in this build -- TODO(M4+)",
            ));
        };

        if !db.flag.enabled().await {
            return Err(denied(
                "feature_disabled",
                "db capability is disabled (waddles.bundle-db-capability is OFF)",
            ));
        }

        let scope = DbScope::new(
            self.tenant.clone(),
            self.community.clone(),
            self.app_id.clone(),
        );

        let column_values =
            |args: &serde_json::Value| -> Result<Vec<(String, DbValue)>, HostResultError> {
                let obj = args
                    .get("column_values")
                    .and_then(|v| v.as_object())
                    .ok_or_else(|| denied("invalid_args", "column_values must be a JSON object"))?;
                obj.iter()
                    .map(|(k, v)| Ok((k.clone(), json_to_db_value(v)?)))
                    .collect()
            };

        let row_id = |args: &serde_json::Value| -> Result<String, HostResultError> {
            args.get("row_id")
                .and_then(|v| v.as_str())
                .map(str::to_string)
                .ok_or_else(|| denied("invalid_args", "row_id must be a string"))
        };

        let expected_version = |args: &serde_json::Value| -> Result<u64, HostResultError> {
            args.get("expected_version")
                .and_then(|v| v.as_u64())
                .ok_or_else(|| denied("invalid_args", "expected_version must be a u64"))
        };

        let result = match call.op.as_str() {
            "insert" => {
                let values = column_values(&call.args)?;
                db.host
                    .insert(&scope, &db.schemas, &db.capabilities, values)
                    .await
            }
            "get" => {
                let id = row_id(&call.args)?;
                db.host
                    .get(&scope, &db.schemas, &db.capabilities, &id)
                    .await
            }
            "update" => {
                let id = row_id(&call.args)?;
                let version = expected_version(&call.args)?;
                let values = column_values(&call.args)?;
                db.host
                    .update(&scope, &db.schemas, &db.capabilities, &id, version, values)
                    .await
            }
            "delete" => {
                let id = row_id(&call.args)?;
                let version = expected_version(&call.args)?;
                return db
                    .host
                    .delete(&scope, &db.schemas, &db.capabilities, &id, version)
                    .await
                    .map(|()| serde_json::json!({}))
                    .map_err(db_error_to_host_error);
            }
            "query" => {
                let limit = call
                    .args
                    .get("limit")
                    .and_then(|v| v.as_u64())
                    .and_then(|v| u32::try_from(v).ok())
                    .unwrap_or(bundle_host_db::MAX_QUERY_LIMIT);
                let offset = call
                    .args
                    .get("offset")
                    .and_then(|v| v.as_u64())
                    .and_then(|v| u32::try_from(v).ok())
                    .unwrap_or(0);
                let order_by = match parse_order_by(&call.args) {
                    Ok(o) => o,
                    Err(e) => return Err(e),
                };
                return db
                    .host
                    .query(
                        &scope,
                        &db.schemas,
                        &db.capabilities,
                        limit,
                        offset,
                        order_by,
                    )
                    .await
                    .map(|rows| {
                        serde_json::json!({
                            "rows": rows.into_iter().map(row_to_json).collect::<Vec<_>>(),
                        })
                    })
                    .map_err(db_error_to_host_error);
            }
            other => {
                return Err(denied(
                    "unknown_op",
                    format!("db op {other:?} not supported"),
                ))
            }
        };

        result.map(row_to_json).map_err(db_error_to_host_error)
    }

    /// `enabled` op: `{"key": String, "default_value": bool}` ->
    /// `{"enabled": bool}` -- un-stubs the `flags` WIT capability
    /// (`core/bundle_executor::host::imports::flags::Host::enabled`'s own
    /// `call(...)` host-call) via `crate::license::resolve_flag`'s real
    /// `penguin_licensing::LicenseClient`-backed fallback chain (live ->
    /// cached -> this call's own `default_value`, see that function's
    /// doc). Never returns an error for a well-formed call -- a flag
    /// lookup failure must fail open to `default_value`, not deny the
    /// host-call (spec §7.4/§6.5's "fail-open to the supplied
    /// `default-value` on a flag-server outage, never an exception",
    /// `core/bundle_executor::host::imports::flags::Host::enabled`'s own
    /// doc). `tier` is not wired in this landing.
    async fn handle_flags(
        &self,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostResultError> {
        // Gate call FIRST (spec SS5, PR #433), same posture as `http`/`db`.
        self.gate
            .authorize(
                &self.gate_scope(),
                PermissionId::FlagsRead,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        match call.op.as_str() {
            "enabled" => {
                let args: FlagsEnabledArgs =
                    serde_json::from_value(call.args.clone()).map_err(|e| {
                        denied(
                            "invalid_args",
                            format!("malformed flags.enabled host-call args: {e}"),
                        )
                    })?;
                let value = crate::license::resolve_flag(&args.key, args.default_value).await;
                Ok(serde_json::json!({ "enabled": value }))
            }
            other => Err(denied(
                "not_implemented",
                format!("flags op {other:?} is not wired in this build -- TODO(M4+)"),
            )),
        }
    }
}

/// `{"key": String, "default_value": bool}` -- `flags.enabled`'s args
/// (`core/bundle_executor::host::imports::flags::Host::enabled`'s own
/// `serde_json::json!` shape).
#[derive(serde::Deserialize)]
struct FlagsEnabledArgs {
    key: String,
    default_value: bool,
}

fn json_to_db_value(v: &serde_json::Value) -> Result<DbValue, HostResultError> {
    Ok(match v {
        serde_json::Value::Null => DbValue::Null,
        serde_json::Value::Bool(b) => DbValue::Bool(*b),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                DbValue::Int(i)
            } else if let Some(f) = n.as_f64() {
                DbValue::Float(f)
            } else {
                return Err(denied("invalid_args", "unsupported numeric value"));
            }
        }
        serde_json::Value::String(s) => DbValue::Text(s.clone()),
        _ => {
            return Err(denied(
                "invalid_args",
                "unsupported value shape (array/object)",
            ))
        }
    })
}

fn db_value_to_json(v: &DbValue) -> serde_json::Value {
    match v {
        DbValue::Null => serde_json::Value::Null,
        DbValue::Bool(b) => serde_json::json!(b),
        DbValue::Int(i) => serde_json::json!(i),
        DbValue::Float(f) => serde_json::json!(f),
        DbValue::Text(s) => serde_json::json!(s),
        DbValue::Bytes(b) => serde_json::json!(b),
    }
}

/// Parses `query`'s optional `order_by` arg: `{"random": true}` or
/// `{"column": "<name>", "descending": <bool>}` -- `None`/absent means the
/// backend's own default (`row_id ASC`). Column-name validation itself
/// happens host-side in `bundle_host_db::backend::order_by_sql` (never
/// trusted from this JSON alone) -- this function only shapes the
/// wire-level args into [`bundle_host_db::OrderBy`].
fn parse_order_by(
    args: &serde_json::Value,
) -> Result<Option<bundle_host_db::OrderBy>, HostResultError> {
    let Some(order_by) = args.get("order_by") else {
        return Ok(None);
    };
    if order_by.is_null() {
        return Ok(None);
    }
    if order_by.get("random").and_then(|v| v.as_bool()) == Some(true) {
        return Ok(Some(bundle_host_db::OrderBy::Random));
    }
    let name = order_by
        .get("column")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            denied(
                "invalid_args",
                "order_by must be {\"random\": true} or {\"column\": string, \"descending\": bool}",
            )
        })?;
    let descending = order_by
        .get("descending")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    Ok(Some(bundle_host_db::OrderBy::Column {
        name: name.to_string(),
        descending,
    }))
}

fn row_to_json(row: bundle_host_db::Row) -> serde_json::Value {
    let columns: serde_json::Map<String, serde_json::Value> = row
        .columns
        .into_iter()
        .map(|(k, v)| (k, db_value_to_json(&v)))
        .collect();
    serde_json::json!({
        "row_id": row.row_id,
        "version": row.version,
        "columns": columns,
    })
}

/// Maps [`DbError`] to the `{code, message}` shape the executor forwards
/// to the guest -- never leaks a raw SQL error string beyond `DbError`'s
/// own already-sanitized `Backend` variant (no row data or PII in any
/// `DbError` variant to begin with, `bundle_host_db`'s own doc).
fn db_error_to_host_error(err: DbError) -> HostResultError {
    denied(err.code(), err.to_string())
}

/// `{"key": String}` -- `kv.get`/`kv.delete`'s args.
#[derive(serde::Deserialize)]
struct KvKeyArgs {
    key: String,
}

/// `{"key": String, "value": Vec<u8>, "ttl_seconds": u32}` -- `kv.set`'s args.
#[derive(serde::Deserialize)]
struct KvSetArgs {
    key: String,
    value: Vec<u8>,
    ttl_seconds: u32,
}

/// `{"key": String, "delta": i64, "ttl_seconds": u32}` -- `kv.increment`'s args.
#[derive(serde::Deserialize)]
struct KvIncrementArgs {
    key: String,
    delta: i64,
    ttl_seconds: u32,
}

fn parse_kv_args<T: serde::de::DeserializeOwned>(
    args: &serde_json::Value,
) -> Result<T, HostResultError> {
    serde_json::from_value(args.clone())
        .map_err(|e| denied("invalid_args", format!("malformed kv host-call args: {e}")))
}

/// Extracts the lowercased host (no scheme, no port, no path) from an
/// `http.send` call's `url` argument -- a deliberately minimal parse (not
/// a full URL parser) since only the host is needed to classify the
/// permission before the request is handed to [`EgressGuard`] (which does
/// the real parse/DNS-resolution/SSRF work). `None` if `url` is missing,
/// not a string, or has no host component -- see `core/svc_action::
/// capabilities::extract_http_host`'s identical helper.
fn extract_http_host(args: &serde_json::Value) -> Option<String> {
    let url = args.get("url").and_then(|v| v.as_str())?;
    let after_scheme = url.split("://").nth(1).unwrap_or(url);
    let host_port = after_scheme.split('/').next().unwrap_or(after_scheme);
    let host = host_port.split(':').next().unwrap_or(host_port);
    if host.is_empty() {
        None
    } else {
        Some(host.to_ascii_lowercase())
    }
}

/// Classifies an extracted `http.send` host into the specific
/// [`PermissionId`] family `bundle_capability_gate`'s catalog actually
/// grants against (`NetHttpFqdn`/`NetHttpPublicIp`/`NetHttpPrivateIp`) --
/// see `core/svc_action::capabilities`'s identical helper for the full
/// rationale (literal IPs classified directly, hostnames as `NetHttpFqdn`;
/// this is a permission-family choice, not the SSRF wall itself).
fn classify_net_http_permission(host: &str) -> PermissionId {
    match host.parse::<std::net::IpAddr>() {
        Ok(std::net::IpAddr::V4(v4)) => {
            if v4.is_private() || v4.is_loopback() || v4.is_link_local() {
                PermissionId::NetHttpPrivateIp(host.to_string())
            } else {
                PermissionId::NetHttpPublicIp(host.to_string())
            }
        }
        Ok(std::net::IpAddr::V6(v6)) => {
            let is_unique_local = (v6.segments()[0] & 0xfe00) == 0xfc00;
            if v6.is_loopback() || v6.is_unicast_link_local() || is_unique_local {
                PermissionId::NetHttpPrivateIp(host.to_string())
            } else {
                PermissionId::NetHttpPublicIp(host.to_string())
            }
        }
        Err(_) => PermissionId::NetHttpFqdn(host.to_string()),
    }
}

/// Maps [`KvError`] onto the `{code, message}` shape every `host-call`
/// error reply carries -- see `core/svc_action::capabilities::
/// kv_err_to_host`'s identical doc.
fn kv_err_to_host(err: KvError) -> HostResultError {
    denied(err.wire_code(), err.wire_message())
}

impl<K: KvBackend> CapabilityHandler for StageCapabilities<K> {
    fn handle<'a>(
        &'a self,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>> {
        Box::pin(async move {
            match call.capability {
                CapabilityKind::Clock => self.handle_clock(&call.op),
                CapabilityKind::Context => self.handle_context(),
                CapabilityKind::Log => self.handle_log(&call.args),
                CapabilityKind::Kv => self.handle_kv(&call).await,
                // Gate call FIRST (spec SS5, PR #433): the classified
                // `NetHttpFqdn`/`NetHttpPublicIp`/`NetHttpPrivateIp`
                // permission must be authorized before the request ever
                // reaches the shared `EgressGuard` (module doc) -- a
                // granted call still passes through the guard's own
                // allowlist/DNS-pinning/redirect-recheck/SSRF checks
                // (`self.handle_http`), which are enforced regardless of
                // grant status.
                CapabilityKind::Http => {
                    let host = extract_http_host(&call.args).ok_or_else(|| {
                        denied("invalid_args", "http.send requires a 'url' string")
                    })?;
                    self.gate
                        .authorize(
                            &self.gate_scope(),
                            classify_net_http_permission(&host),
                            ResourceRef::AppScoped(AppScopedResource::None),
                        )
                        .map_err(denied_from_gate)?;
                    self.handle_http(&call.args).await
                }
                // `db` is wired to `bundle_host_db::DbHost` (PR #498) --
                // `handle_db` itself gate-checks FIRST (spec SS5) before
                // consulting its own feature-flag/wiring state.
                //
                // `reputation.*` ops (issue #726) ride the same wire kind
                // (`CapabilityKind` is closed in penguin-bundle-host) and are
                // dispatched on the op prefix BEFORE the `storage.tables`
                // path: they carry their own gate permission
                // (`reputation.read`/`reputation.community.write`), never
                // `storage.tables`.
                CapabilityKind::Db if call.op.starts_with("reputation.") => {
                    self.handle_reputation(&call).await
                }
                // `economy.*` ops (issue #714) likewise: own gate permissions
                // (`economy.read`/`economy.wager`/`economy.transfer`), own
                // handler, never the `storage.tables` path.
                CapabilityKind::Db if call.op.starts_with("economy.") => {
                    self.handle_economy(&call).await
                }
                // `identity.*` ops likewise: own gate permission
                // (`identity.resolve`), own handler, never the
                // `storage.tables` path.
                CapabilityKind::Db if call.op.starts_with("identity.") => {
                    self.handle_identity(&call).await
                }
                CapabilityKind::Db => self.handle_db(&call).await,
                // `enabled` is wired to a real `penguin_licensing::
                // LicenseClient` (`crate::license::resolve_flag`) -- see
                // that module's doc for the fallback semantics (live ->
                // cached -> caller default, never an error). `tier` is
                // not wired in this landing. `handle_flags` itself
                // gate-checks FIRST (spec SS5).
                CapabilityKind::Flags => self.handle_flags(&call).await,
                // Spec §6.5: "Capability: granted only to action-stage
                // bundles" -- never granted to a process-stage bundle at
                // all. The gate is still consulted first for uniform audit
                // (module doc), but this arm never trusts a `Granted`
                // outcome alone: even if a grant somehow existed (a
                // misconfiguration this stage's own manifest/linker should
                // never produce), the hard denial below still fires --
                // belt-and-suspenders against ever running relay
                // process-side.
                CapabilityKind::Relay => {
                    let provider = call
                        .args
                        .get("provider")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown")
                        .to_string();
                    let _ = self.gate.authorize(
                        &self.gate_scope(),
                        PermissionId::ChatSend(provider),
                        ResourceRef::AppScoped(AppScopedResource::None),
                    );
                    Err(denied(
                        "not_granted",
                        "relay is an action-stage-only capability, never granted to a process-stage bundle",
                    ))
                }
            }
        })
    }
}

/// A [`CapabilityHandler`] that denies every call -- the fallback answer
/// for a `host-call` whose `call_id` matches no in-flight invoke (see
/// `crate::host_api::Connection`'s per-invoke scope registry), and for
/// tests exercising the host-API connection layer in isolation from
/// capability semantics.
pub struct DenyAllCapabilities;

impl CapabilityHandler for DenyAllCapabilities {
    fn handle<'a>(
        &'a self,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>> {
        Box::pin(async move {
            Err(denied(
                "not_implemented",
                format!(
                    "{:?} capability has no in-flight invoke scope for this call_id",
                    call.capability
                ),
            ))
        })
    }
}

/// Type-erased convenience so callers can hold either implementation
/// behind one `Arc<dyn CapabilityHandler>`.
pub fn boxed(handler: impl CapabilityHandler + 'static) -> Arc<dyn CapabilityHandler> {
    Arc::new(handler)
}

#[cfg(test)]
mod tests {
    use super::*;
    use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
    use std::time::Duration;

    /// Grants every permission this file's existing (pre-gate) tests already
    /// exercised, at `StageCapabilities::new`'s `(0, 0, 0)` default scope --
    /// so every happy-path test keeps proving its own capability logic, not
    /// this landing's gate wiring (the dedicated `gate_*` tests below
    /// exercise that directly, including the deny-without-grant cases).
    /// Includes every host the `http` capability's own tests below exercise
    /// (module doc: the gate runs *before* the `EgressGuard`, so those tests
    /// need a grant to even reach the guard's allowlist/SSRF checks).
    fn permissive_gate() -> Arc<CapabilityGate> {
        let snapshot = bundle_capability_gate::InMemoryGrantSnapshot::new();
        let mut grants = std::collections::HashMap::new();
        for id in [
            "platform.context",
            "platform.clock",
            "platform.log",
            "storage.kv",
            "storage.tables",
            "flags.read",
            "net.http.fqdn:example.com",
            "net.http.fqdn:api.example.com",
            "net.http.fqdn:evil.example.com",
            "net.http.private-ip:10.0.0.5",
            "net.http.public-ip:93.184.216.34",
            // The `db_flags_capabilities_are_documented_seams` test calls
            // `Http` with no `url` arg at all in some historical variants --
            // `extract_http_host` returning `None` now denies `invalid_args`
            // before the gate is even consulted for that case, but this
            // entry is kept for any host that resolves to an empty string.
            "net.http.fqdn:",
        ] {
            grants.insert(
                id.to_string(),
                bundle_capability_gate::GrantedPermission {
                    permission_id: id.to_string(),
                    params: serde_json::json!({}),
                },
            );
        }
        snapshot.set(
            bundle_capability_gate::GrantScopeKey {
                tenant_id: 0,
                community_id: 0,
                app_id: "waddles.bot.commands.default".to_string(),
                app_version: 0,
            },
            bundle_capability_gate::GrantSet {
                permission_snapshot_hash: "test".to_string(),
                grants,
            },
        );
        Arc::new(CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ))
    }

    /// A gate with an empty snapshot -- every permission fails closed
    /// `not_granted`, for tests proving the gate is actually consulted.
    fn deny_all_gate() -> Arc<CapabilityGate> {
        Arc::new(CapabilityGate::new(
            Arc::new(bundle_capability_gate::InMemoryGrantSnapshot::new()),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ))
    }

    fn test_egress_metrics() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_svc_process_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap()
    }

    fn test_egress_limits() -> bundle_host_http::egress::EgressLimits {
        bundle_host_http::egress::EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        }
    }

    /// A bare [`EgressGuard`] over an empty catalog -- for fixtures that
    /// don't exercise `http` at all and just need a valid value to satisfy
    /// [`StageCapabilities::new`]'s required `egress` parameter. Every
    /// `app_id` is undeclared, so `http` denies every call
    /// `host_not_declared` (this stage's deny-by-default posture, see
    /// [`HttpEgressCatalog`]'s doc). Tests exercising a granted call build
    /// their own guard via [`caps_with_egress_rule`] instead.
    fn test_egress() -> Arc<EgressGuard> {
        Arc::new(EgressGuard::new(
            Arc::new(ReqwestTransport::new()),
            test_egress_limits(),
            HttpEgressCatalog::new(),
            test_egress_metrics(),
            bundle_host_http::egress::boxed(StaticFlag(true)),
        ))
    }

    /// The default fixture: [`test_egress`] (an empty catalog, so every
    /// `http` call denies `host_not_declared` once the gate has already
    /// granted the classified permission) plus a [`permissive_gate`].
    fn caps() -> StageCapabilities {
        StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            0,
            0,
            0,
            test_egress(),
            permissive_gate(),
        )
    }

    /// A [`StageCapabilities`] whose `egress` catalog declares exactly one
    /// `(host, methods)` entry for `waddles.bot.commands.default`, gated by
    /// `gate`.
    fn caps_with_egress_rule_and_gate(
        host: &str,
        methods: &[&str],
        gate: Arc<CapabilityGate>,
    ) -> StageCapabilities {
        let catalog = HttpEgressCatalog::new();
        catalog.update(
            "waddles.bot.commands.default",
            EgressRuleRow::from_legacy_patterns(
                vec![(
                    host.to_string(),
                    methods.iter().map(|m| m.to_string()).collect(),
                )],
                None,
                HashMap::new(),
            ),
        );
        let egress = Arc::new(EgressGuard::new(
            Arc::new(ReqwestTransport::new()),
            test_egress_limits(),
            catalog,
            test_egress_metrics(),
            bundle_host_http::egress::boxed(StaticFlag(true)),
        ));
        StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            0,
            0,
            0,
            egress,
            gate,
        )
    }

    /// A [`StageCapabilities`] whose `egress` catalog declares exactly one
    /// `(host, methods)` entry for `waddles.bot.commands.default`, gated by
    /// [`permissive_gate`] (same rationale as [`caps`]).
    fn caps_with_egress_rule(host: &str, methods: &[&str]) -> StageCapabilities {
        caps_with_egress_rule_and_gate(host, methods, permissive_gate())
    }

    /// [`permissive_gate`] plus an explicit instance-policy override
    /// allowing `net.http.private-ip` (deny-by-default instance-wide, see
    /// `bundle_capability_gate::instance_policy`'s module doc) -- isolates
    /// the `http_denies_a_declared_host_that_is_actually_a_private_ip` test
    /// to proving [`EgressGuard`]'s own SSRF address check, not the gate's
    /// separate (and separately tested, `bundle_capability_gate::gate`'s
    /// own suite) instance-policy denial.
    fn permissive_gate_with_private_ip_allowed() -> Arc<CapabilityGate> {
        let snapshot = bundle_capability_gate::InMemoryGrantSnapshot::new();
        snapshot.set(
            bundle_capability_gate::GrantScopeKey {
                tenant_id: 0,
                community_id: 0,
                app_id: "waddles.bot.commands.default".to_string(),
                app_version: 0,
            },
            bundle_capability_gate::GrantSet {
                permission_snapshot_hash: "test".to_string(),
                grants: std::collections::HashMap::from([(
                    "net.http.private-ip:10.0.0.5".to_string(),
                    bundle_capability_gate::GrantedPermission {
                        permission_id: "net.http.private-ip:10.0.0.5".to_string(),
                        params: serde_json::json!({}),
                    },
                )]),
            },
        );
        let instance_policy = bundle_capability_gate::InMemoryInstancePolicySnapshot::new();
        instance_policy.set(
            bundle_capability_gate::PermissionFamily::NetHttpPrivateIp,
            bundle_capability_gate::InstanceAction::Allow,
        );
        Arc::new(CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(instance_policy),
        ))
    }

    fn caps_denied() -> StageCapabilities {
        StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            0,
            0,
            0,
            test_egress(),
            deny_all_gate(),
        )
    }

    fn caps_denied_with_kv() -> StageCapabilities<FakeKvBackend> {
        StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            0,
            0,
            0,
            test_egress(),
            deny_all_gate(),
        )
        .with_kv(
            FakeKvBackend::default(),
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
        )
    }

    /// Grants `storage.kv` to `"waddles.bot.commands.default"` -- the
    /// `app_id` every `caps()`/`call()` helper in this module uses -- so
    /// every existing `kv_*` test below (testing `handle_kv`'s argument
    /// parsing/error mapping, not the gate itself) is unaffected by the
    /// "undeclared means denied" default.
    /// `kv_call_is_denied_when_storage_kv_is_undeclared` below is the one
    /// test exercising an ungranted app.
    fn caps_with_kv() -> StageCapabilities<FakeKvBackend> {
        caps_with_kv_and_capabilities(&["waddles.bot.commands.default"])
    }

    fn caps_with_kv_and_capabilities(granted_app_ids: &[&str]) -> StageCapabilities<FakeKvBackend> {
        let snapshot = bundle_host_kv::CapabilitySnapshot::new();
        for app_id in granted_app_ids {
            snapshot.update(
                *app_id,
                [bundle_host_kv::authorize::KV_PERMISSION_ID.to_string()],
            );
        }
        StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            0,
            0,
            0,
            test_egress(),
            permissive_gate(),
        )
        .with_kv(FakeKvBackend::default(), std::sync::Arc::new(snapshot))
    }

    /// A gate with a grant seeded under a specific, non-zero `app_version`
    /// -- models `source_supervisor::resolve_binding_app_version`'s real
    /// resolved value (never the `0` interim placeholder
    /// `run_binding_consumer` used to hardcode).
    fn gate_seeded_at_version(app_version: i64) -> Arc<CapabilityGate> {
        let snapshot = bundle_capability_gate::InMemoryGrantSnapshot::new();
        snapshot.set(
            bundle_capability_gate::GrantScopeKey {
                tenant_id: 7,
                community_id: 3,
                app_id: "waddles.bot.commands.default".to_string(),
                app_version,
            },
            bundle_capability_gate::GrantSet {
                permission_snapshot_hash: "test".to_string(),
                grants: std::collections::HashMap::from([(
                    "storage.kv".to_string(),
                    bundle_capability_gate::GrantedPermission {
                        permission_id: "storage.kv".to_string(),
                        params: serde_json::json!({}),
                    },
                )]),
            },
        );
        Arc::new(CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ))
    }

    /// Regression (gh-433): a source-binding invocation carrying the
    /// resolved `app_version` (`source_supervisor::
    /// resolve_binding_app_version`'s real value) is ALLOWED by a grant
    /// seeded under that same version.
    #[tokio::test]
    async fn source_binding_invocation_with_the_resolved_app_version_is_allowed_by_a_seeded_grant()
    {
        let kv_capabilities = bundle_host_kv::CapabilitySnapshot::new();
        kv_capabilities.update(
            "waddles.bot.commands.default",
            [bundle_host_kv::authorize::KV_PERMISSION_ID.to_string()],
        );
        let caps = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            7,
            3,
            42,
            test_egress(),
            gate_seeded_at_version(42),
        )
        .with_kv(FakeKvBackend::default(), Arc::new(kv_capabilities));
        caps.handle(call(
            CapabilityKind::Kv,
            "get",
            serde_json::json!({"key": "k"}),
        ))
        .await
        .expect("a grant seeded under the resolved app_version allows the call");
    }

    /// Regression (gh-433): an unresolvable/mismatched `app_version` --
    /// modeled as a scope carrying a different version than any seeded
    /// grant -- is DENIED, never silently authorized under the wrong
    /// version's permissions.
    #[tokio::test]
    async fn source_binding_invocation_with_an_unresolved_app_version_is_denied() {
        let caps = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            7,
            3,
            999,
            test_egress(),
            gate_seeded_at_version(42),
        )
        .with_kv(
            FakeKvBackend::default(),
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
        );
        let err = caps
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    /// A minimal in-memory [`KvBackend`] fake, mirroring
    /// `bundle_host_kv::backend::fake::FakeBackend`'s semantics (that one
    /// is crate-private to `bundle_host_kv`, so `handle_kv`'s own
    /// argument-parsing/error-mapping is exercised here against a fresh,
    /// independent implementation of the public `KvBackend` trait -- no
    /// live Valkey server needed for this module's own tests). Identical
    /// to `core/svc_action::capabilities::tests::FakeKvBackend`.
    #[derive(Default)]
    struct FakeKvBackend {
        data: std::sync::Mutex<std::collections::HashMap<String, Vec<u8>>>,
        counts: std::sync::Mutex<std::collections::HashMap<String, u64>>,
    }

    impl KvBackend for FakeKvBackend {
        fn get<'a>(
            &'a self,
            data_key: &'a str,
        ) -> bundle_host_kv::BoxFuture<'a, Result<Option<Vec<u8>>, String>> {
            let value = self.data.lock().unwrap().get(data_key).cloned();
            Box::pin(async move { Ok(value) })
        }

        fn set_with_quota<'a>(
            &'a self,
            data_key: &'a str,
            count_key: &'a str,
            value: &'a [u8],
            _ttl_seconds: u32,
            max_keys: u64,
        ) -> bundle_host_kv::BoxFuture<'a, Result<bundle_host_kv::QuotaOutcome<()>, String>>
        {
            let mut data = self.data.lock().unwrap();
            let existed = data.contains_key(data_key);
            if !existed {
                let mut counts = self.counts.lock().unwrap();
                let count = *counts.get(count_key).unwrap_or(&0);
                if count >= max_keys {
                    return Box::pin(
                        async move { Ok(bundle_host_kv::QuotaOutcome::QuotaExceeded) },
                    );
                }
                counts.insert(count_key.to_string(), count + 1);
            }
            data.insert(data_key.to_string(), value.to_vec());
            Box::pin(async move { Ok(bundle_host_kv::QuotaOutcome::Admitted(())) })
        }

        fn delete<'a>(
            &'a self,
            data_key: &'a str,
            count_key: &'a str,
        ) -> bundle_host_kv::BoxFuture<'a, Result<bool, String>> {
            let existed = self.data.lock().unwrap().remove(data_key).is_some();
            if existed {
                let mut counts = self.counts.lock().unwrap();
                let count = *counts.get(count_key).unwrap_or(&0);
                counts.insert(count_key.to_string(), count.saturating_sub(1));
            }
            Box::pin(async move { Ok(existed) })
        }

        fn increment_with_quota<'a>(
            &'a self,
            data_key: &'a str,
            count_key: &'a str,
            delta: i64,
            _ttl_seconds: u32,
            max_keys: u64,
        ) -> bundle_host_kv::BoxFuture<'a, Result<bundle_host_kv::QuotaOutcome<i64>, String>>
        {
            let mut data = self.data.lock().unwrap();
            let existed = data.contains_key(data_key);
            if !existed {
                let mut counts = self.counts.lock().unwrap();
                let count = *counts.get(count_key).unwrap_or(&0);
                if count >= max_keys {
                    return Box::pin(
                        async move { Ok(bundle_host_kv::QuotaOutcome::QuotaExceeded) },
                    );
                }
                counts.insert(count_key.to_string(), count + 1);
            }
            let current = data
                .get(data_key)
                .and_then(|v| std::str::from_utf8(v).ok())
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            let new_value = current + delta;
            data.insert(data_key.to_string(), new_value.to_string().into_bytes());
            Box::pin(async move { Ok(bundle_host_kv::QuotaOutcome::Admitted(new_value)) })
        }

        fn increment_rate<'a>(
            &'a self,
            _rate_key: &'a str,
            _window_seconds: u64,
        ) -> bundle_host_kv::BoxFuture<'a, Result<u64, String>> {
            // Unbounded in this fake -- `handle_kv`'s own rate limit is
            // `bundle_host_kv::KvHost`'s responsibility, already covered by
            // that crate's own tests; this module only needs to prove its
            // argument parsing/error mapping, not re-prove the limiter.
            Box::pin(async move { Ok(1) })
        }

        fn reconcile_count_if_missing<'a>(
            &'a self,
            _count_key: &'a str,
            _data_scan_pattern: &'a str,
            _lock_key: &'a str,
            _lock_ttl_ms: u64,
            _scan_limit: u64,
        ) -> bundle_host_kv::BoxFuture<'a, Result<bundle_host_kv::ReconcileOutcome, String>>
        {
            // Always "already present" -- the eviction self-heal path is
            // `bundle_host_kv`'s own responsibility, covered by that
            // crate's tests; this module only needs argument parsing/error
            // mapping.
            Box::pin(async move { Ok(bundle_host_kv::ReconcileOutcome::AlreadyPresent) })
        }
    }

    fn call(capability: CapabilityKind, op: &str, args: serde_json::Value) -> HostCallBody {
        HostCallBody {
            app_id: "waddles.bot.commands.default".to_string(),
            capability,
            op: op.to_string(),
            args,
            call_id: 1,
        }
    }

    fn mock_db_wiring(flag_on: bool) -> DbWiring {
        let conn = sea_orm::MockDatabase::new(sea_orm::DatabaseBackend::Postgres).into_connection();
        DbWiring {
            host: Arc::new(DbHost::new(bundle_host_db::PostgresBackend::new(conn))),
            schemas: Arc::new(SchemaCache::new()),
            capabilities: Arc::new(DbCapabilitySnapshot::new()),
            flag: Arc::new(crate::license::test_support::FixedGate(flag_on)),
        }
    }

    #[tokio::test]
    async fn db_capability_denies_not_implemented_when_never_wired() {
        let capabilities = caps();
        let err = capabilities
            .handle_db(&call(
                CapabilityKind::Db,
                "get",
                serde_json::json!({"row_id": "x"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_implemented");
    }

    #[tokio::test]
    async fn db_capability_denies_feature_disabled_when_flag_is_off() {
        let capabilities = caps().with_db(mock_db_wiring(false));
        let err = capabilities
            .handle_db(&call(
                CapabilityKind::Db,
                "get",
                serde_json::json!({"row_id": "x"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "feature_disabled");
    }

    #[tokio::test]
    async fn db_capability_denies_no_table_when_flag_on_but_unprovisioned() {
        let capabilities = caps().with_db(mock_db_wiring(true));
        capabilities.db.as_ref().unwrap().capabilities.update(
            "waddles.bot.commands.default",
            ["storage.tables".to_string()],
        );
        let err = capabilities
            .handle_db(&call(
                CapabilityKind::Db,
                "get",
                serde_json::json!({"row_id": "00000000-0000-0000-0000-000000000000"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "no_table");
    }

    #[tokio::test]
    async fn db_capability_rejects_an_unknown_op() {
        let capabilities = caps().with_db(mock_db_wiring(true));
        let err = capabilities
            .handle_db(&call(CapabilityKind::Db, "truncate", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
    }

    #[tokio::test]
    async fn db_capability_rejects_missing_row_id_for_get() {
        let capabilities = caps().with_db(mock_db_wiring(true));
        let err = capabilities
            .handle_db(&call(CapabilityKind::Db, "get", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn db_capability_query_op_denies_no_table_when_unprovisioned() {
        // `query` is dispatched the same as insert/get/update/delete at
        // this untyped op layer -- proves the new arm actually reaches
        // `DbHost::query` (resolve-then-authorize denies `no_table` here,
        // same as every other op against an unprovisioned schema) rather
        // than falling through to `unknown_op`.
        let capabilities = caps().with_db(mock_db_wiring(true));
        capabilities.db.as_ref().unwrap().capabilities.update(
            "waddles.bot.commands.default",
            ["storage.tables".to_string()],
        );
        let err = capabilities
            .handle_db(&call(
                CapabilityKind::Db,
                "query",
                serde_json::json!({"limit": 10, "offset": 0}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "no_table");
    }

    #[tokio::test]
    async fn db_capability_query_op_defaults_limit_and_offset_when_omitted() {
        // No `limit`/`offset` in args must not be `invalid_args` -- both
        // are optional, defaulting to MAX_QUERY_LIMIT/0 respectively.
        let capabilities = caps().with_db(mock_db_wiring(true));
        let err = capabilities
            .handle_db(&call(CapabilityKind::Db, "query", serde_json::json!({})))
            .await
            .unwrap_err();
        // Not `invalid_args` -- both args are optional and were accepted;
        // this app simply never declared `storage.tables`
        // (`DbHost::resolve_and_authorize` maps a denied authorize() to
        // `DbError::InvalidColumn`, whose `code()` is `"invalid_column"`).
        assert_eq!(err.code, "invalid_column");
    }

    #[test]
    fn parse_order_by_is_none_when_absent_or_null() {
        assert!(parse_order_by(&serde_json::json!({})).unwrap().is_none());
        assert!(parse_order_by(&serde_json::json!({"order_by": null}))
            .unwrap()
            .is_none());
    }

    #[test]
    fn parse_order_by_parses_random() {
        let order_by = parse_order_by(&serde_json::json!({"order_by": {"random": true}}))
            .unwrap()
            .unwrap();
        assert_eq!(order_by, bundle_host_db::OrderBy::Random);
    }

    #[test]
    fn parse_order_by_parses_column_and_direction() {
        let order_by = parse_order_by(
            &serde_json::json!({"order_by": {"column": "score", "descending": true}}),
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            order_by,
            bundle_host_db::OrderBy::Column {
                name: "score".to_string(),
                descending: true,
            }
        );
    }

    #[test]
    fn parse_order_by_column_defaults_descending_to_false() {
        let order_by = parse_order_by(&serde_json::json!({"order_by": {"column": "score"}}))
            .unwrap()
            .unwrap();
        assert_eq!(
            order_by,
            bundle_host_db::OrderBy::Column {
                name: "score".to_string(),
                descending: false,
            }
        );
    }

    #[test]
    fn parse_order_by_rejects_a_malformed_order_by() {
        let err = parse_order_by(&serde_json::json!({"order_by": "not-an-object"})).unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn db_capability_query_op_accepts_order_by_random() {
        // Proves `order_by` actually reaches `DbHost::query` through the
        // untyped op layer (denied `no_table`, same as every other query
        // test above, rather than `invalid_args`).
        let capabilities = caps().with_db(mock_db_wiring(true));
        capabilities.db.as_ref().unwrap().capabilities.update(
            "waddles.bot.commands.default",
            ["storage.tables".to_string()],
        );
        let err = capabilities
            .handle_db(&call(
                CapabilityKind::Db,
                "query",
                serde_json::json!({"limit": 1, "order_by": {"random": true}}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "no_table");
    }

    #[test]
    fn sanitize_bundle_log_message_strips_crlf_and_control_characters() {
        assert_eq!(
            sanitize_bundle_log_message("line1\r\nline2\tafter-tab"),
            "line1line2after-tab"
        );
    }

    #[test]
    fn sanitize_bundle_log_message_redacts_an_email_shaped_value() {
        let sanitized = sanitize_bundle_log_message("user@example.com logged in");
        assert!(!sanitized.contains("user@example.com"));
        assert!(sanitized.starts_with("[email]@example.com"));
    }

    #[test]
    fn sanitize_bundle_log_message_truncates_to_the_length_cap() {
        let huge = "a".repeat(MAX_BUNDLE_LOG_MESSAGE_LEN + 500);
        let sanitized = sanitize_bundle_log_message(&huge);
        assert_eq!(sanitized.chars().count(), MAX_BUNDLE_LOG_MESSAGE_LEN);
    }

    #[tokio::test]
    async fn clock_now_millis_returns_a_positive_integer() {
        let result = caps()
            .handle(call(
                CapabilityKind::Clock,
                "now-millis",
                serde_json::json!({}),
            ))
            .await
            .expect("clock succeeds");
        assert!(result.as_u64().unwrap() > 0);
    }

    #[tokio::test]
    async fn clock_now_rfc3339_returns_a_z_suffixed_string() {
        let result = caps()
            .handle(call(
                CapabilityKind::Clock,
                "now-rfc3339",
                serde_json::json!({}),
            ))
            .await
            .expect("clock succeeds");
        assert!(result.as_str().unwrap().ends_with('Z'));
    }

    #[tokio::test]
    async fn clock_monotonic_nanos_is_non_decreasing_across_two_calls() {
        let c = caps();
        let first = c
            .handle(call(
                CapabilityKind::Clock,
                "monotonic-nanos",
                serde_json::json!({}),
            ))
            .await
            .expect("clock succeeds")
            .as_u64()
            .unwrap();
        let second = c
            .handle(call(
                CapabilityKind::Clock,
                "monotonic-nanos",
                serde_json::json!({}),
            ))
            .await
            .expect("clock succeeds")
            .as_u64()
            .unwrap();
        assert!(second >= first);
    }

    #[tokio::test]
    async fn clock_unknown_op_is_denied() {
        let err = caps()
            .handle(call(CapabilityKind::Clock, "bogus", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
    }

    #[tokio::test]
    async fn context_reports_scope_without_secrets() {
        let result = caps()
            .handle(call(CapabilityKind::Context, "get", serde_json::json!({})))
            .await
            .expect("context succeeds");
        assert_eq!(result["tenant"], "acme");
        assert_eq!(result["community"], "main");
        assert_eq!(result["app_id"], "waddles.bot.commands.default");
    }

    #[tokio::test]
    async fn log_write_at_every_level_succeeds() {
        let c = caps();
        for level in ["error", "warn", "debug", "info", "unrecognized"] {
            let result = c
                .handle(call(
                    CapabilityKind::Log,
                    "write",
                    serde_json::json!({"level": level, "message": "hello"}),
                ))
                .await
                .expect("log write succeeds");
            assert_eq!(result, serde_json::json!({}));
        }
    }

    #[tokio::test]
    async fn db_flags_capabilities_are_documented_seams() {
        // `kv` and `http` are no longer unconditional seams -- see
        // `kv_is_not_implemented_when_no_backend_was_configured` for `kv`'s
        // own (backend-unconfigured) not_implemented case, the `kv_*` tests
        // below for its fully-wired behavior, and the `http_*` tests below
        // for `http`'s gate-then-egress-guard behavior. `caps()`'s
        // `permissive_gate()` grants both `storage.tables`/`flags.read`, so
        // this proves the gate-THEN-not_implemented ordering, not a denial.
        let c = caps();
        for capability in [CapabilityKind::Db, CapabilityKind::Flags] {
            let err = c
                .handle(call(capability, "anything", serde_json::json!({})))
                .await
                .unwrap_err();
            assert_eq!(err.code, "not_implemented");
        }
    }

    // -- `http` capability (interim capability-gate: undeclared means
    // denied, `HttpEgressCatalog`'s doc) --

    /// The default fixture's catalog is empty -- every `app_id` is
    /// undeclared, so `http` denies exactly like an explicit
    /// manifest-egress miss in `svc_action`.
    #[tokio::test]
    async fn http_denies_an_undeclared_app_as_host_not_declared() {
        let err = caps()
            .handle(call(
                CapabilityKind::Http,
                "send",
                serde_json::json!({"method": "GET", "url": "https://api.example.com/"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    /// A host the app's manifest never declared is denied even when the
    /// same app *does* have other declared hosts -- declaring one host
    /// buys access to nothing else.
    #[tokio::test]
    async fn http_denies_a_non_allowlisted_host_for_a_declared_app() {
        let err = caps_with_egress_rule("api.example.com", &["GET"])
            .handle(call(
                CapabilityKind::Http,
                "send",
                serde_json::json!({"method": "GET", "url": "https://evil.example.com/"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    /// A declared host resolving to a private IP is still blocked by the
    /// guard's always-enforced SSRF address check -- declaring a host
    /// never bypasses step 6 (`bundle_host_http::egress::
    /// is_forbidden_address`'s doc).
    #[tokio::test]
    async fn http_denies_a_declared_host_that_is_actually_a_private_ip() {
        let err = caps_with_egress_rule_and_gate(
            "10.0.0.5",
            &["GET"],
            permissive_gate_with_private_ip_allowed(),
        )
        .handle(call(
            CapabilityKind::Http,
            "send",
            serde_json::json!({"method": "GET", "url": "https://10.0.0.5/"}),
        ))
        .await
        .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    /// The allow path: a declared public host, reached through the real
    /// `EgressGuard` pipeline via a live local server standing in for
    /// `api.example.com` (the guard's own DNS-pinning connects to whatever
    /// `Resolver` returns -- `ReqwestTransport` here resolves the
    /// loopback listener through the OS resolver override below).
    #[tokio::test]
    async fn http_send_reaches_the_transport_for_a_declared_allowlisted_host() {
        // `EgressGuard`'s production `Resolver` is `tokio::net::
        // lookup_host`, not swappable outside this crate -- so the
        // end-to-end allow path is proven the same way
        // `bundle_host_http::egress`'s own moved test suite proves it
        // (`FakeTransport`/`TestCatalog`-equivalent), via a fresh
        // `EgressGuard` built directly rather than through `caps_with_
        // egress_rule` (which pins `ReqwestTransport`, a *real* TLS
        // client this test must not depend on network access).
        struct FakeTransport(
            Arc<std::sync::Mutex<Vec<bundle_host_http::egress::TransportRequest>>>,
        );
        impl bundle_host_http::egress::HttpTransport for FakeTransport {
            fn send<'a>(
                &'a self,
                req: bundle_host_http::egress::TransportRequest,
                _timeout: Duration,
                _max_response_bytes: usize,
            ) -> Pin<
                Box<
                    dyn Future<
                            Output = Result<
                                bundle_host_http::egress::TransportResponse,
                                HostResultError,
                            >,
                        > + Send
                        + 'a,
                >,
            > {
                self.0.lock().unwrap().push(req);
                Box::pin(async move {
                    Ok(bundle_host_http::egress::TransportResponse {
                        status: 200,
                        headers: vec![],
                        body: b"{}".to_vec(),
                        truncated: false,
                    })
                })
            }
        }

        let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
        let catalog = HttpEgressCatalog::new();
        catalog.update(
            "waddles.bot.commands.default",
            EgressRuleRow::from_legacy_patterns(
                vec![("93.184.216.34".to_string(), vec!["GET".to_string()])],
                None,
                HashMap::new(),
            ),
        );
        let egress = Arc::new(EgressGuard::new(
            Arc::new(FakeTransport(Arc::clone(&seen))),
            test_egress_limits(),
            catalog,
            test_egress_metrics(),
            bundle_host_http::egress::boxed(StaticFlag(true)),
        ));
        let caps: StageCapabilities = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            0,
            0,
            0,
            egress,
            permissive_gate(),
        );

        let result = caps
            .handle(call(
                CapabilityKind::Http,
                "send",
                serde_json::json!({"method": "GET", "url": "https://93.184.216.34/"}),
            ))
            .await
            .expect("declared public IP is permitted");
        assert_eq!(result["status"], 200);
        assert_eq!(seen.lock().unwrap().len(), 1);
    }

    /// Secret-handle substitution (connector spec condition 8) works
    /// identically through this stage's `http` capability -- the guest's
    /// own `secret_refs` name is resolved against this bundle's granted
    /// map, never handed a raw env-var name.
    #[tokio::test]
    async fn http_send_substitutes_a_granted_secret_ref_as_a_header() {
        struct FakeTransport(
            Arc<std::sync::Mutex<Vec<bundle_host_http::egress::TransportRequest>>>,
        );
        impl bundle_host_http::egress::HttpTransport for FakeTransport {
            fn send<'a>(
                &'a self,
                req: bundle_host_http::egress::TransportRequest,
                _timeout: Duration,
                _max_response_bytes: usize,
            ) -> Pin<
                Box<
                    dyn Future<
                            Output = Result<
                                bundle_host_http::egress::TransportResponse,
                                HostResultError,
                            >,
                        > + Send
                        + 'a,
                >,
            > {
                self.0.lock().unwrap().push(req);
                Box::pin(async move {
                    Ok(bundle_host_http::egress::TransportResponse {
                        status: 200,
                        headers: vec![],
                        body: b"{}".to_vec(),
                        truncated: false,
                    })
                })
            }
        }

        // SAFETY: test-process-local env var, unique name avoids
        // cross-test collisions under parallel `cargo test` execution.
        unsafe { std::env::set_var("SVC_PROCESS_EGRESS_TEST_TOKEN", "s3cr3t") };
        let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
        let catalog = HttpEgressCatalog::new();
        catalog.update(
            "waddles.bot.commands.default",
            EgressRuleRow::from_legacy_patterns(
                vec![("93.184.216.34".to_string(), vec!["POST".to_string()])],
                None,
                HashMap::from([(
                    "TOKEN_REF".to_string(),
                    "SVC_PROCESS_EGRESS_TEST_TOKEN".to_string(),
                )]),
            ),
        );
        let egress = Arc::new(EgressGuard::new(
            Arc::new(FakeTransport(Arc::clone(&seen))),
            test_egress_limits(),
            catalog,
            test_egress_metrics(),
            bundle_host_http::egress::boxed(StaticFlag(true)),
        ));
        let caps: StageCapabilities = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            0,
            0,
            0,
            egress,
            permissive_gate(),
        );

        caps.handle(call(
            CapabilityKind::Http,
            "send",
            serde_json::json!({
                "method": "POST",
                "url": "https://93.184.216.34/",
                "secret_refs": {"Authorization": "TOKEN_REF"}
            }),
        ))
        .await
        .expect("send succeeds");
        unsafe { std::env::remove_var("SVC_PROCESS_EGRESS_TEST_TOKEN") };

        let requests = seen.lock().unwrap();
        assert!(requests[0]
            .headers
            .iter()
            .any(|(k, v)| k == "Authorization" && v == "s3cr3t"));
    }

    #[tokio::test]
    async fn kv_is_not_implemented_when_no_backend_was_configured() {
        let err = caps()
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_implemented");
    }

    #[tokio::test]
    async fn kv_call_is_denied_when_storage_kv_is_undeclared() {
        // A configured `kv` backend, but the app's `CapabilitySnapshot`
        // grants nothing -- "undeclared means denied", distinct from
        // `kv_is_not_implemented_when_no_backend_was_configured`'s
        // "backend never configured at all" case.
        let c = caps_with_kv_and_capabilities(&[]);
        let err = c
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "backend"); // KvError::wire_code() collapse
    }

    #[tokio::test]
    async fn kv_call_succeeds_when_storage_kv_is_declared() {
        let c = caps_with_kv_and_capabilities(&["waddles.bot.commands.default"]);
        c.handle(call(
            CapabilityKind::Kv,
            "set",
            serde_json::json!({"key": "k", "value": [1], "ttl_seconds": 0}),
        ))
        .await
        .expect("set succeeds when storage.kv is declared");
    }

    #[tokio::test]
    async fn kv_set_then_get_round_trips_through_the_handler() {
        let c = caps_with_kv();
        c.handle(call(
            CapabilityKind::Kv,
            "set",
            serde_json::json!({"key": "counter", "value": [1, 2, 3], "ttl_seconds": 0}),
        ))
        .await
        .expect("set succeeds");

        let result = c
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "counter"}),
            ))
            .await
            .expect("get succeeds");
        assert_eq!(result["value"], serde_json::json!([1, 2, 3]));
    }

    #[tokio::test]
    async fn kv_get_of_an_absent_key_returns_a_null_value_not_an_error() {
        let c = caps_with_kv();
        let result = c
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "absent"}),
            ))
            .await
            .expect("get succeeds");
        assert_eq!(result["value"], serde_json::Value::Null);
    }

    #[tokio::test]
    async fn kv_delete_then_get_returns_null() {
        let c = caps_with_kv();
        c.handle(call(
            CapabilityKind::Kv,
            "set",
            serde_json::json!({"key": "k", "value": [9], "ttl_seconds": 0}),
        ))
        .await
        .unwrap();
        c.handle(call(
            CapabilityKind::Kv,
            "delete",
            serde_json::json!({"key": "k"}),
        ))
        .await
        .expect("delete succeeds");
        let result = c
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .expect("get succeeds");
        assert_eq!(result["value"], serde_json::Value::Null);
    }

    #[tokio::test]
    async fn kv_increment_accumulates_across_calls() {
        let c = caps_with_kv();
        let first = c
            .handle(call(
                CapabilityKind::Kv,
                "increment",
                serde_json::json!({"key": "hits", "delta": 5, "ttl_seconds": 0}),
            ))
            .await
            .expect("increment succeeds");
        assert_eq!(first["value"], serde_json::json!(5));

        let second = c
            .handle(call(
                CapabilityKind::Kv,
                "increment",
                serde_json::json!({"key": "hits", "delta": 3, "ttl_seconds": 0}),
            ))
            .await
            .expect("increment succeeds");
        assert_eq!(second["value"], serde_json::json!(8));
    }

    #[tokio::test]
    async fn kv_set_with_malformed_args_is_rejected_as_invalid_args() {
        let c = caps_with_kv();
        let err = c
            .handle(call(
                CapabilityKind::Kv,
                "set",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn kv_unknown_op_is_rejected() {
        let c = caps_with_kv();
        let err = c
            .handle(call(CapabilityKind::Kv, "bogus", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
    }

    #[tokio::test]
    async fn kv_rejects_a_guest_key_that_attempts_a_namespace_escape() {
        let c = caps_with_kv();
        let err = c
            .handle(call(
                CapabilityKind::Kv,
                "set",
                serde_json::json!({"key": "other:app:data:secret", "value": [1], "ttl_seconds": 0}),
            ))
            .await
            .unwrap_err();
        // `KvError::wire_code` collapses every non-`too_large` reason to
        // `"backend"` at the host-call boundary (module doc) -- the
        // finer-grained `invalid_key` reason is what `bundle_host_kv`'s own
        // tests assert against `KvError::code()` directly.
        assert_eq!(err.code, "backend");
    }

    #[tokio::test]
    async fn relay_is_permanently_denied_never_granted_to_process() {
        let err = caps()
            .handle(call(CapabilityKind::Relay, "push", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[tokio::test]
    async fn gate_denies_kv_without_a_grant() {
        let err = caps_denied_with_kv()
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[tokio::test]
    async fn gate_denies_context_clock_log_without_a_grant() {
        for capability in [
            CapabilityKind::Context,
            CapabilityKind::Clock,
            CapabilityKind::Log,
        ] {
            let err = caps_denied()
                .handle(call(capability, "now-millis", serde_json::json!({})))
                .await
                .unwrap_err();
            assert_eq!(err.code, "not_granted");
        }
    }

    #[tokio::test]
    async fn gate_denies_http_db_flags_without_a_grant() {
        // `Http` needs a well-formed `url` arg to even reach the gate
        // (`extract_http_host` denies `invalid_args` first otherwise, module
        // doc) -- `Db`/`Flags` don't parse their args before the gate call.
        for (capability, args) in [
            (
                CapabilityKind::Http,
                serde_json::json!({"method": "GET", "url": "https://api.example.com/"}),
            ),
            (CapabilityKind::Db, serde_json::json!({})),
            (CapabilityKind::Flags, serde_json::json!({})),
        ] {
            let err = caps_denied()
                .handle(call(capability, "anything", args))
                .await
                .unwrap_err();
            assert_eq!(err.code, "not_granted");
        }
    }

    /// An undeclared permission (granted set has entries, but not the one
    /// this call needs) denies exactly like an empty grant set.
    #[tokio::test]
    async fn gate_denies_a_permission_the_app_was_never_granted() {
        let snapshot = bundle_capability_gate::InMemoryGrantSnapshot::new();
        snapshot.set(
            bundle_capability_gate::GrantScopeKey {
                tenant_id: 0,
                community_id: 0,
                app_id: "waddles.bot.commands.default".to_string(),
                app_version: 0,
            },
            bundle_capability_gate::GrantSet {
                permission_snapshot_hash: "test".to_string(),
                grants: std::collections::HashMap::from([(
                    "flags.read".to_string(),
                    bundle_capability_gate::GrantedPermission {
                        permission_id: "flags.read".to_string(),
                        params: serde_json::json!({}),
                    },
                )]),
            },
        );
        let gate = Arc::new(CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ));
        let caps = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            0,
            0,
            0,
            test_egress(),
            gate,
        )
        .with_kv(
            FakeKvBackend::default(),
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
        );

        let err = caps
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    /// Revocation mid-stream (spec SS4/SS5.3): a grant present at
    /// construction, then invalidated, denies the very next call.
    #[tokio::test]
    async fn gate_revocation_mid_stream_denies_the_next_call() {
        let key = bundle_capability_gate::GrantScopeKey {
            tenant_id: 0,
            community_id: 0,
            app_id: "waddles.bot.commands.default".to_string(),
            app_version: 0,
        };
        let loader = Arc::new(bundle_capability_gate::InMemoryGrantLoader::new());
        loader.set(
            key.clone(),
            bundle_capability_gate::GrantSet {
                permission_snapshot_hash: "v1".to_string(),
                grants: std::collections::HashMap::from([(
                    "storage.kv".to_string(),
                    bundle_capability_gate::GrantedPermission {
                        permission_id: "storage.kv".to_string(),
                        params: serde_json::json!({}),
                    },
                )]),
            },
        );
        let cache = Arc::new(bundle_capability_gate::GrantCache::new(loader));
        cache.refresh(&key).await.unwrap();
        let gate = Arc::new(CapabilityGate::new(
            cache.clone(),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ));
        let kv_capabilities = bundle_host_kv::CapabilitySnapshot::new();
        kv_capabilities.update(
            "waddles.bot.commands.default",
            [bundle_host_kv::authorize::KV_PERMISSION_ID.to_string()],
        );
        let caps = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            0,
            0,
            0,
            test_egress(),
            gate,
        )
        .with_kv(FakeKvBackend::default(), Arc::new(kv_capabilities));

        caps.handle(call(
            CapabilityKind::Kv,
            "get",
            serde_json::json!({"key": "k"}),
        ))
        .await
        .expect("granted before revocation");

        cache.invalidate(&key);

        let err = caps
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[tokio::test]
    async fn deny_all_denies_every_capability() {
        let handler = DenyAllCapabilities;
        let result = handler
            .handle(call(
                CapabilityKind::Clock,
                "now-millis",
                serde_json::json!({}),
            ))
            .await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn boxed_wraps_a_handler_as_an_arc_dyn() {
        let handler: Arc<dyn CapabilityHandler> = boxed(DenyAllCapabilities);
        let result = handler
            .handle(call(CapabilityKind::Log, "write", serde_json::json!({})))
            .await;
        assert!(result.is_err());
    }
}

#[cfg(test)]
#[path = "capabilities_reputation_tests.rs"]
mod reputation_tests;

#[cfg(test)]
#[path = "capabilities_economy_tests.rs"]
mod economy_tests;

#[cfg(test)]
#[path = "capabilities_identity_tests.rs"]
mod identity_tests;
