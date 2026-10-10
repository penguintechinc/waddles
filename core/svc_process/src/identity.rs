//! The `identity` bundle host capability (`wit/waddle-bundle/stage.wit`
//! `interface identity`, `world stage-next`): resolves WHO an invocation is
//! about -- the triggering actor, or a mention target the triggering message
//! carried -- to the community `user_uuid` the `economy` and `reputation`
//! capabilities require. It is the prerequisite of every points-game bundle
//! (`!gamble` needs the actor; `!steal @user` / `!duel @user` need the actor
//! AND the target).
//!
//! # Why the stage resolves it, not the bundle
//!
//! A bundle only ever holds the tokenized `{user:<uuid>}` placeholder
//! (`crate::pii_tokenize`). That token is a hub-api-minted pseudonymous UUID, but
//! it is NOT guaranteed to equal `community_members.user_uuid` (the value the
//! economy/reputation membership checks match): an account linked through
//! `community_members.user_id` without a `hub_user_identities` row mints to a
//! different UUID, a Twitch `@handle` mention mints a `handle:<name>` pseudonym
//! that is nobody's real identity, and an unresolved member has a NULL
//! `user_uuid`. So the bundle asks the host, and the host answers from the same
//! authoritative table the economy checks.
//!
//! # What crosses to the bundle: a UUID, never PII
//!
//! The success value is `Uuid::to_string()` and nothing else (the executor
//! additionally refuses any non-canonical-UUID `user` -- see
//! `bundle_executor::host::stage_next_identity`). The bundle supplies no
//! tenant, community, platform or user argument that selects whose identity is
//! resolved:
//!
//! * the **actor** is derived host-side from the event the stage delivered
//!   ([`InvocationIdentity::from_event`]): the platform account id it carries,
//!   resolved under the invocation's host-derived `(tenant, community)`;
//! * a **mention** is resolved only from the per-invocation table of
//!   references THIS message carried ([`InvocationIdentity::mentions`]), keyed
//!   by the opaque token the bundle was shown. An unknown token is
//!   [`IdentityError::NotFound`]: the call is not a directory lookup, so a
//!   bundle cannot probe whether an arbitrary handle exists.
//!
//! The raw references (`<@123>`, `@handle`, a platform account id) live only in
//! this module's host-side structures; their `Debug` impls are redacted, and no
//! log line, error message or metric label ever carries one.
//!
//! # Fail-closed
//!
//! Every refusal is an explicit [`IdentityError`]; no path returns a default,
//! guessed or pseudonym UUID. A free-text handle is resolved inside hub-api's PII
//! boundary (`IdentityService.ResolveHandle`, tenant-scoped; zero matches ->
//! `NotFound`, several -> `Ambiguous`, never guessed) and the result is then
//! CONFIRMED an active member of this community through the same live
//! membership view -- an identity that exists in the tenant but not in this
//! community is `NotAMember`, so the call is also not a cross-community oracle.
//!
//! The read path is the existing PII-free data-plane view
//! `community_member_identities` (alembic 0043/0045/0052) under the read-only
//! `waddles_bundle_reader` role the stage already holds for the grant tables --
//! no new role, password or privilege.

use std::collections::HashMap;
use std::fmt;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};
use penguin_spine::PlatformEvent;
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement, Value};
use uuid::Uuid;

use crate::license::FeatureGate;

/// Boxed future, so the two resolver seams are object-safe without
/// `async_trait` (same convention as `crate::license::FeatureGate`).
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Longest mention token the stage will even consider. A real token is a
/// 36-byte UUID (or, with inbound tokenization off, a `<@id>` / `@handle`
/// reference of at most ~30 bytes).
pub const MAX_MENTION_TOKEN_LEN: usize = 256;

/// Upper bound on distinct mention references one invocation's table holds.
/// A message with more is truncated (the surplus is simply not resolvable:
/// `NotFound`), never an unbounded per-event allocation.
pub const MAX_MENTIONS_PER_INVOCATION: usize = 64;

/// Longest platform account id the actor path will bind (the
/// `community_members.platform_user_id` column is `VARCHAR(255)`).
const MAX_PLATFORM_USER_ID_LEN: usize = 255;

/// Hard ceiling on one directory query so a stuck connection can never hold a
/// bundle invocation past its host-call deadline.
const DIRECTORY_QUERY_TIMEOUT: Duration = Duration::from_secs(3);

/// The host-derived `(tenant, community)` an identity lookup is scoped to --
/// the numeric ids the capability gate already authorized the call under,
/// never anything the guest supplied.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IdentityScope {
    pub tenant_id: i32,
    pub community_id: i32,
}

/// A raw mention reference exactly as a chatter typed it, held HOST-SIDE only.
/// `Debug` is redacted: a raw handle or platform id must never reach a log
/// line via `{:?}`.
#[derive(Clone, PartialEq, Eq)]
pub enum MentionRef {
    /// A Discord-style `<@123>` / `<@!123>` mention: the platform account id.
    PlatformId(String),
    /// A free-text `@handle` (Twitch/IRC): the handle without its `@`.
    Handle(String),
}

impl MentionRef {
    /// A fixed, PII-free label for logs and metrics.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::PlatformId(_) => "platform_id",
            Self::Handle(_) => "handle",
        }
    }
}

impl fmt::Debug for MentionRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "MentionRef::{}(<redacted>)", self.kind())
    }
}

/// One mention the triggering message carried: the opaque `key` the bundle was
/// shown (the `<uuid>` inside a `{user:<uuid>}` placeholder when inbound
/// tokenization ran, or the raw `<@123>` / `@handle` text when it is switched
/// off) and the host-side reference it stands for. `Debug` is redacted.
#[derive(Clone, PartialEq, Eq)]
pub struct MentionBinding {
    pub key: String,
    pub reference: MentionRef,
}

impl fmt::Debug for MentionBinding {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "MentionBinding({:?})", self.reference)
    }
}

/// Every way an identity resolution can fail. [`IdentityError::wire_code`] is
/// the stable string the stage puts in the host-result error and the executor
/// maps back onto the WIT `identity.error` variant. Messages are fixed,
/// PII-free constants.
#[derive(Debug, Clone, thiserror::Error, PartialEq, Eq)]
pub enum IdentityError {
    /// The identity is a member (or the event names an account) but has no
    /// resolved community `user_uuid` (`community_members.user_uuid IS NULL`),
    /// or the triggering event carries no platform account id at all.
    #[error("identity is not linked: no resolved community user_uuid")]
    NotLinked,
    /// The identity resolves but is not an active member of this community.
    #[error("identity is not an active member of this community")]
    NotAMember,
    /// The mention is not one this message carried, or matches no identity in
    /// the tenant.
    #[error("no such mention in this message, or no such identity")]
    NotFound,
    /// A free-text handle matches more than one identity; never guessed.
    #[error("the handle matches more than one identity")]
    Ambiguous,
    /// Malformed argument.
    #[error("invalid argument: {0}")]
    Invalid(String),
    /// The resolver is not provisioned / not reachable (flag OFF is handled by
    /// the capability handler as `feature_disabled`).
    #[error("identity resolution unavailable: {0}")]
    Unavailable(String),
    /// Database/internal failure (detail is logged, never handed to a guest).
    #[error("backend error: {0}")]
    Backend(String),
}

impl IdentityError {
    /// Stable machine code carried over the host-API wire.
    pub fn wire_code(&self) -> &'static str {
        match self {
            Self::NotLinked => "not_linked",
            Self::NotAMember => "not_a_member",
            Self::NotFound => "not_found",
            Self::Ambiguous => "ambiguous",
            Self::Invalid(_) => "invalid_args",
            Self::Unavailable(_) => "unavailable",
            Self::Backend(_) => "backend",
        }
    }
}

/// The read side of community membership: platform account id -> community
/// `user_uuid`, and live "is this UUID an active member" confirmation. Both are
/// tenant- AND community-scoped by construction.
pub trait MemberDirectory: Send + Sync {
    /// The `user_uuid` of the ACTIVE member of `scope` with this platform
    /// account. No row or an inactive row -> [`IdentityError::NotAMember`]; an
    /// active row whose `user_uuid` is NULL -> [`IdentityError::NotLinked`].
    fn member_by_platform_id<'a>(
        &'a self,
        scope: IdentityScope,
        platform: &'a str,
        platform_user_id: &'a str,
    ) -> BoxFuture<'a, Result<Uuid, IdentityError>>;

    /// Confirms `user` is an ACTIVE member of `scope` and returns it back;
    /// otherwise [`IdentityError::NotAMember`].
    fn confirm_member<'a>(
        &'a self,
        scope: IdentityScope,
        user: Uuid,
    ) -> BoxFuture<'a, Result<Uuid, IdentityError>>;
}

/// Resolves a free-text handle reference to its stable identity UUID entirely
/// inside hub-api's PII boundary (`IdentityService.ResolveHandle`).
pub trait HandleResolver: Send + Sync {
    /// `reference` is the handle as typed (with its `@`). Zero matches ->
    /// [`IdentityError::NotFound`]; several -> [`IdentityError::Ambiguous`].
    fn resolve_handle<'a>(
        &'a self,
        tenant_id: i32,
        platform: &'a str,
        reference: &'a str,
    ) -> BoxFuture<'a, Result<Uuid, IdentityError>>;
}

/// Live wiring for the `identity` capability: the membership directory, the
/// optional hub-api handle resolver, and the opt-in flag. Built once at startup
/// (`crate::lib::try_build_identity_wiring`) and cloned (cheap, `Arc`) into every
/// per-invoke `StageCapabilities`, like `ReputationWiring`/`EconomyWiring`.
/// `None` on the handler means every `identity.*` call denies `not_implemented`.
#[derive(Clone)]
pub struct IdentityWiring {
    pub directory: Arc<dyn MemberDirectory>,
    /// `None` when hub-api's internal gRPC is not configured: platform-id
    /// mentions and the actor still resolve; only free-text handles are
    /// `unavailable` (fail-closed, never a guess).
    pub handles: Option<Arc<dyn HandleResolver>>,
    /// `crate::license::BUNDLE_IDENTITY_CAPABILITY_FLAG` gate -- OFF denies
    /// every call `feature_disabled` before the directory is touched.
    pub flag: Arc<dyn FeatureGate>,
}

/// Host-derived identity facts of ONE invocation: who triggered it and which
/// mentions its message carried. Built by the stage from the event it
/// delivered; never from guest input. Everything raw is private and `Debug`
/// is counts-only.
pub struct InvocationIdentity {
    platform: String,
    actor_platform_user_id: Option<String>,
    mentions: HashMap<String, MentionRef>,
    /// Per-invocation success cache (`actor` / `mention:<key>`), so a bundle
    /// resolving the same identity repeatedly costs one lookup. Successes only
    /// -- a refusal is always re-evaluated. Never held across an `.await`.
    resolved: Mutex<HashMap<String, Uuid>>,
}

impl fmt::Debug for InvocationIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("InvocationIdentity")
            .field("platform", &self.platform)
            .field("has_actor", &self.actor_platform_user_id.is_some())
            .field("mentions", &self.mentions.len())
            .finish()
    }
}

/// `true` iff `s` is a usable platform account id / token: non-empty, bounded,
/// no control characters.
fn is_clean(s: &str, max: usize) -> bool {
    !s.is_empty() && s.len() <= max && !s.chars().any(char::is_control)
}

/// Normalizes a mention token as a bundle may present it: surrounding
/// whitespace trimmed, a `{user:<uuid>}` placeholder unwrapped to its inner
/// token, lower-cased (UUIDs and handles are case-insensitive). `None` for an
/// empty, oversized or control-character-bearing token.
pub fn normalize_token(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    let inner = trimmed
        .strip_prefix("{user:")
        .and_then(|rest| rest.strip_suffix('}'))
        .unwrap_or(trimmed)
        .trim();
    is_clean(inner, MAX_MENTION_TOKEN_LEN).then(|| inner.to_ascii_lowercase())
}

impl InvocationIdentity {
    /// Builds the invocation's identity facts from the RAW inbound `event` (the
    /// one the stage received, before any tokenization) and the mention
    /// `bindings` the tokenization pass produced for the bundle-visible text.
    pub fn from_event(event: &PlatformEvent, bindings: Vec<MentionBinding>) -> Self {
        let actor_platform_user_id = ["user_id", "author_id"]
            .iter()
            .find_map(|k| event.payload.get(*k).and_then(serde_json::Value::as_str))
            .filter(|id| is_clean(id, MAX_PLATFORM_USER_ID_LEN))
            .map(str::to_string);
        let mut mentions = HashMap::new();
        for binding in bindings {
            if mentions.len() >= MAX_MENTIONS_PER_INVOCATION {
                tracing::debug!(
                    "identity: mention table full; surplus mentions are not resolvable"
                );
                break;
            }
            if let Some(key) = normalize_token(&binding.key) {
                mentions.entry(key).or_insert(binding.reference);
            }
        }
        Self {
            platform: event.platform.clone(),
            actor_platform_user_id,
            mentions,
            resolved: Mutex::new(HashMap::new()),
        }
    }

    /// As [`Self::from_event`] for the opt-out path where inbound PII
    /// tokenization is switched off and the bundle is shown the RAW text: the
    /// table is keyed by the raw reference exactly as the bundle sees it
    /// (`<@123>`, `@bob`) -- still limited to references THIS message carried,
    /// so it is no more of a lookup oracle than the tokenized path.
    pub fn for_untokenized_event(event: &PlatformEvent) -> Self {
        let bindings: Vec<MentionBinding> = match event
            .payload
            .get("text")
            .and_then(serde_json::Value::as_str)
        {
            Some(text) => crate::pii_tokenize::scan_mentions(text)
                .into_iter()
                .map(|m| MentionBinding {
                    key: m.matched_text,
                    reference: m.reference,
                })
                .collect(),
            // A non-text event (no `text` string) names no one: an empty
            // mention table is the correct state, not a swallowed error.
            None => {
                tracing::debug!(
                    platform = %event.platform,
                    "identity: untokenized event carries no text; no mentions to bind"
                );
                Vec::new()
            }
        };
        Self::from_event(event, bindings)
    }

    /// Test/fixture constructor with explicit raw facts.
    pub fn new(
        platform: impl Into<String>,
        actor_platform_user_id: Option<String>,
        mentions: Vec<MentionBinding>,
    ) -> Self {
        let mut table = HashMap::new();
        for binding in mentions {
            if let Some(key) = normalize_token(&binding.key) {
                table.entry(key).or_insert(binding.reference);
            }
        }
        Self {
            platform: platform.into(),
            actor_platform_user_id,
            mentions: table,
            resolved: Mutex::new(HashMap::new()),
        }
    }

    /// Number of resolvable mentions this invocation's message carried.
    pub fn mention_count(&self) -> usize {
        self.mentions.len()
    }

    fn cached(&self, key: &str) -> Option<Uuid> {
        self.resolved.lock().ok()?.get(key).copied()
    }

    fn remember(&self, key: String, user: Uuid) {
        if let Ok(mut map) = self.resolved.lock() {
            map.insert(key, user);
        }
    }
}

struct Instruments {
    duration_seconds: Histogram<f64>,
    resolutions_total: Counter<u64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = global::meter("svc_process_identity");
        Instruments {
            duration_seconds: meter
                .f64_histogram("waddles_bundle_identity_resolve_duration_seconds")
                .with_description("Bundle `identity` resolution latency, by op/via/outcome")
                .with_unit("s")
                .build(),
            resolutions_total: meter
                .u64_counter("waddles_bundle_identity_resolutions_total")
                .with_description("Bundle `identity` resolutions, by op, via and outcome")
                .build(),
        }
    })
}

/// Records one resolution's latency and outcome. Labels are fixed vocabularies
/// only -- never a user id, handle, platform id or token.
fn record(op: &'static str, via: &'static str, outcome: &'static str, seconds: f64) {
    let attrs = [
        KeyValue::new("op", op),
        KeyValue::new("via", via),
        KeyValue::new("outcome", outcome),
    ];
    instruments().duration_seconds.record(seconds, &attrs);
    instruments().resolutions_total.add(1, &attrs);
}

/// A non-nil UUID or a loud backend error: a nil UUID is never a legitimate
/// identity and would be exactly the "default value" this capability forbids.
fn non_nil(user: Uuid) -> Result<Uuid, IdentityError> {
    if user.is_nil() {
        tracing::error!("identity: a resolver returned the nil UUID; refusing it");
        return Err(IdentityError::Backend(
            "resolver returned an invalid identity".to_string(),
        ));
    }
    Ok(user)
}

/// Resolves the TRIGGERING actor of `inv` to its community `user_uuid`.
pub async fn resolve_actor(
    wiring: &IdentityWiring,
    inv: &InvocationIdentity,
    scope: IdentityScope,
) -> Result<Uuid, IdentityError> {
    let started = Instant::now();
    if let Some(user) = inv.cached("actor") {
        record("actor", "cache", "ok", started.elapsed().as_secs_f64());
        return Ok(user);
    }
    let result = async {
        let Some(platform_user_id) = inv.actor_platform_user_id.as_deref() else {
            tracing::debug!(
                platform = %inv.platform,
                "identity.resolve_actor: the triggering event carries no platform account id"
            );
            return Err(IdentityError::NotLinked);
        };
        let user = wiring
            .directory
            .member_by_platform_id(scope, &inv.platform, platform_user_id)
            .await
            .and_then(non_nil)?;
        inv.remember("actor".to_string(), user);
        Ok(user)
    }
    .await;
    finish("actor", "directory", scope, &result, started);
    result
}

/// Resolves a mention `token` the bundle presented to its community
/// `user_uuid`. Answers ONLY for references present in `inv`'s message.
pub async fn resolve_mention(
    wiring: &IdentityWiring,
    inv: &InvocationIdentity,
    scope: IdentityScope,
    token: &str,
) -> Result<Uuid, IdentityError> {
    let started = Instant::now();
    let Some(key) = normalize_token(token) else {
        let err = IdentityError::Invalid("mention token is empty or malformed".to_string());
        finish("mention", "none", scope, &Err(err.clone()), started);
        return Err(err);
    };
    let cache_key = format!("mention:{key}");
    if let Some(user) = inv.cached(&cache_key) {
        record("mention", "cache", "ok", started.elapsed().as_secs_f64());
        return Ok(user);
    }
    let Some(reference) = inv.mentions.get(&key) else {
        // Deliberately the same answer for "never in this message" and "no such
        // identity": a bundle must not be able to tell the two apart.
        let err = IdentityError::NotFound;
        finish("mention", "none", scope, &Err(err.clone()), started);
        return Err(err);
    };
    let via = reference.kind();
    let result = async {
        let user = match reference {
            MentionRef::PlatformId(platform_user_id) => {
                wiring
                    .directory
                    .member_by_platform_id(scope, &inv.platform, platform_user_id)
                    .await?
            }
            MentionRef::Handle(handle) => {
                let handles = wiring.handles.as_ref().ok_or_else(|| {
                    IdentityError::Unavailable(
                        "handle resolution is not provisioned (hub-api resolver not configured)"
                            .to_string(),
                    )
                })?;
                let reference = format!("@{handle}");
                let identity = handles
                    .resolve_handle(scope.tenant_id, &inv.platform, &reference)
                    .await?;
                // hub-api resolves within the TENANT; the capability is
                // community-scoped, so confirm live membership here.
                wiring
                    .directory
                    .confirm_member(scope, non_nil(identity)?)
                    .await?
            }
        };
        let user = non_nil(user)?;
        inv.remember(cache_key, user);
        Ok(user)
    }
    .await;
    finish("mention", via, scope, &result, started);
    result
}

/// Emits the PII-free outcome log line + metric for one resolution.
fn finish(
    op: &'static str,
    via: &'static str,
    scope: IdentityScope,
    result: &Result<Uuid, IdentityError>,
    started: Instant,
) {
    let outcome = match result {
        Ok(_) => "ok",
        Err(e) => e.wire_code(),
    };
    record(op, via, outcome, started.elapsed().as_secs_f64());
    match result {
        Ok(_) => tracing::debug!(
            op,
            via,
            tenant_id = scope.tenant_id,
            community_id = scope.community_id,
            "identity resolved"
        ),
        Err(IdentityError::Backend(detail)) => tracing::error!(
            op,
            via,
            tenant_id = scope.tenant_id,
            community_id = scope.community_id,
            error = %detail,
            "identity resolution backend error"
        ),
        Err(IdentityError::Unavailable(detail)) => tracing::warn!(
            op,
            via,
            tenant_id = scope.tenant_id,
            community_id = scope.community_id,
            error = %detail,
            "identity resolution unavailable"
        ),
        Err(e) => tracing::debug!(
            op,
            via,
            tenant_id = scope.tenant_id,
            community_id = scope.community_id,
            outcome = e.wire_code(),
            "identity resolution refused"
        ),
    }
}

/// Active-member row by platform account id. `LIMIT 2` so a (constraint-
/// forbidden) duplicate is detected rather than silently first-wins.
const ACTOR_SQL: &str = "SELECT user_uuid::text AS user_uuid, is_active_member AS is_active \
     FROM community_member_identities \
     WHERE tenant_id = $1 AND community_id = $2 AND platform = $3 AND platform_user_id = $4 \
     LIMIT 2";

/// Active-membership confirmation by `user_uuid` (the partial unique index
/// `(community_id, user_uuid)` guarantees at most one row).
const CONFIRM_SQL: &str = "SELECT is_active_member AS is_active \
     FROM community_member_identities \
     WHERE tenant_id = $1 AND community_id = $2 AND user_uuid = $3::uuid \
     LIMIT 2";

/// Production [`MemberDirectory`]: reads the PII-free
/// `community_member_identities` view over the read-only
/// `waddles_bundle_reader` connection (alembic 0052). Every query binds
/// `(tenant_id, community_id)` from the host-derived [`IdentityScope`].
pub struct PgMemberDirectory {
    db: DatabaseConnection,
}

impl PgMemberDirectory {
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }

    async fn query(
        &self,
        sql: &'static str,
        values: Vec<Value>,
    ) -> Result<Vec<sea_orm::QueryResult>, IdentityError> {
        let stmt = Statement::from_sql_and_values(DbBackend::Postgres, sql, values);
        match tokio::time::timeout(DIRECTORY_QUERY_TIMEOUT, self.db.query_all_raw(stmt)).await {
            Ok(Ok(rows)) => Ok(rows),
            Ok(Err(e)) => Err(IdentityError::Backend(format!(
                "identity directory query: {e}"
            ))),
            Err(_) => Err(IdentityError::Unavailable(
                "identity directory query timed out".to_string(),
            )),
        }
    }
}

fn backend(e: impl fmt::Display) -> IdentityError {
    IdentityError::Backend(format!("identity directory row: {e}"))
}

impl MemberDirectory for PgMemberDirectory {
    fn member_by_platform_id<'a>(
        &'a self,
        scope: IdentityScope,
        platform: &'a str,
        platform_user_id: &'a str,
    ) -> BoxFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            let rows = self
                .query(
                    ACTOR_SQL,
                    vec![
                        Value::Int(Some(scope.tenant_id)),
                        Value::Int(Some(scope.community_id)),
                        Value::String(Some(platform.to_string())),
                        Value::String(Some(platform_user_id.to_string())),
                    ],
                )
                .await?;
            let row = match rows.as_slice() {
                [] => return Err(IdentityError::NotAMember),
                [row] => row,
                _ => {
                    return Err(IdentityError::Backend(
                        "more than one membership row for one platform account".to_string(),
                    ))
                }
            };
            let active: bool = row.try_get("", "is_active").map_err(backend)?;
            if !active {
                return Err(IdentityError::NotAMember);
            }
            let user: Option<String> = row.try_get("", "user_uuid").map_err(backend)?;
            match user {
                None => Err(IdentityError::NotLinked),
                Some(text) => Uuid::parse_str(&text)
                    .map_err(|_| IdentityError::Backend("malformed user_uuid column".to_string())),
            }
        })
    }

    fn confirm_member<'a>(
        &'a self,
        scope: IdentityScope,
        user: Uuid,
    ) -> BoxFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            let rows = self
                .query(
                    CONFIRM_SQL,
                    vec![
                        Value::Int(Some(scope.tenant_id)),
                        Value::Int(Some(scope.community_id)),
                        Value::String(Some(user.to_string())),
                    ],
                )
                .await?;
            let row = match rows.as_slice() {
                [] => return Err(IdentityError::NotAMember),
                [row] => row,
                _ => {
                    return Err(IdentityError::Backend(
                        "more than one membership row for one user_uuid".to_string(),
                    ))
                }
            };
            let active: bool = row.try_get("", "is_active").map_err(backend)?;
            if active {
                Ok(user)
            } else {
                Err(IdentityError::NotAMember)
            }
        })
    }
}

/// Production [`HandleResolver`]: a thin adapter over
/// `hub_client::HubClient::resolve_handle` (`IdentityService.ResolveHandle`,
/// #748). The client must be connected with the `identity:handle:resolve`
/// machine-JWT scope (a client is single-scope -- see `crate::lib`'s
/// `build_hub_handle_client`).
pub struct HubHandleResolver(pub Arc<hub_client::HubClient>);

/// Maps a `ResolveHandle` failure onto an [`IdentityError`]: NOT_FOUND ->
/// `NotFound`, FAILED_PRECONDITION (ambiguous) -> `Ambiguous`, INVALID_ARGUMENT
/// (role/channel mention, `@everyone`, malformed) -> `Invalid`; everything else
/// (hub-api down, circuit open, auth) -> `Unavailable`. No hub-api message text
/// is forwarded to a guest.
fn hub_error_to_identity(err: &hub_client::HubClientError) -> IdentityError {
    use hub_client::HubClientError as E;
    match err {
        E::Grpc(status) => match status.code() {
            tonic::Code::NotFound => IdentityError::NotFound,
            tonic::Code::FailedPrecondition => IdentityError::Ambiguous,
            tonic::Code::InvalidArgument => {
                IdentityError::Invalid("mention is not a resolvable user reference".to_string())
            }
            tonic::Code::Unauthenticated | tonic::Code::PermissionDenied => {
                tracing::error!(
                    code = ?status.code(),
                    "identity: hub-api rejected the ResolveHandle credential; check the \
                     identity:handle:resolve scope grant for this service"
                );
                IdentityError::Unavailable("identity resolver is not authorized".to_string())
            }
            _ => IdentityError::Unavailable("identity resolver unavailable".to_string()),
        },
        _ => IdentityError::Unavailable("identity resolver unavailable".to_string()),
    }
}

impl HandleResolver for HubHandleResolver {
    fn resolve_handle<'a>(
        &'a self,
        tenant_id: i32,
        platform: &'a str,
        reference: &'a str,
    ) -> BoxFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            let response = self
                .0
                .resolve_handle(
                    tenant_id.to_string(),
                    platform.to_string(),
                    reference.to_string(),
                )
                .await
                .map_err(|e| hub_error_to_identity(&e))?;
            Uuid::parse_str(&response.uuid).map_err(|_| {
                IdentityError::Backend("resolver returned a malformed uuid".to_string())
            })
        })
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;
    use crate::license::test_support::FixedGate;
    use std::sync::atomic::{AtomicUsize, Ordering};

    const SCOPE: IdentityScope = IdentityScope {
        tenant_id: 7,
        community_id: 3,
    };

    /// A directory double keyed by `(platform, platform_user_id)` and by uuid,
    /// counting lookups so cache behavior is observable.
    #[derive(Default)]
    struct FakeDirectory {
        by_platform: HashMap<(String, String), Result<Uuid, IdentityErrorKind>>,
        members: Vec<Uuid>,
        lookups: AtomicUsize,
        scopes_seen: Mutex<Vec<IdentityScope>>,
    }

    /// `IdentityError` is not `Clone`; the fake stores the interesting kinds.
    #[derive(Clone, Copy)]
    enum IdentityErrorKind {
        NotLinked,
        Backend,
    }

    impl From<IdentityErrorKind> for IdentityError {
        fn from(k: IdentityErrorKind) -> Self {
            match k {
                IdentityErrorKind::NotLinked => IdentityError::NotLinked,
                IdentityErrorKind::Backend => IdentityError::Backend("boom".to_string()),
            }
        }
    }

    impl MemberDirectory for FakeDirectory {
        fn member_by_platform_id<'a>(
            &'a self,
            scope: IdentityScope,
            platform: &'a str,
            platform_user_id: &'a str,
        ) -> BoxFuture<'a, Result<Uuid, IdentityError>> {
            Box::pin(async move {
                self.lookups.fetch_add(1, Ordering::SeqCst);
                self.scopes_seen.lock().unwrap().push(scope);
                match self
                    .by_platform
                    .get(&(platform.to_string(), platform_user_id.to_string()))
                {
                    Some(Ok(u)) => Ok(*u),
                    Some(Err(k)) => Err((*k).into()),
                    None => Err(IdentityError::NotAMember),
                }
            })
        }

        fn confirm_member<'a>(
            &'a self,
            scope: IdentityScope,
            user: Uuid,
        ) -> BoxFuture<'a, Result<Uuid, IdentityError>> {
            Box::pin(async move {
                self.lookups.fetch_add(1, Ordering::SeqCst);
                self.scopes_seen.lock().unwrap().push(scope);
                if self.members.contains(&user) {
                    Ok(user)
                } else {
                    Err(IdentityError::NotAMember)
                }
            })
        }
    }

    struct FakeHandles(Result<Uuid, fn() -> IdentityError>, AtomicUsize);

    impl HandleResolver for FakeHandles {
        fn resolve_handle<'a>(
            &'a self,
            tenant_id: i32,
            platform: &'a str,
            reference: &'a str,
        ) -> BoxFuture<'a, Result<Uuid, IdentityError>> {
            Box::pin(async move {
                self.1.fetch_add(1, Ordering::SeqCst);
                assert_eq!(tenant_id, SCOPE.tenant_id, "tenant comes from the scope");
                assert_eq!(platform, "twitch");
                assert!(reference.starts_with('@'), "hub-api is sent the typed form");
                match &self.0 {
                    Ok(u) => Ok(*u),
                    Err(f) => Err(f()),
                }
            })
        }
    }

    fn wiring(
        dir: FakeDirectory,
        handles: Option<FakeHandles>,
    ) -> (IdentityWiring, Arc<FakeDirectory>) {
        let dir = Arc::new(dir);
        (
            IdentityWiring {
                directory: Arc::clone(&dir) as Arc<dyn MemberDirectory>,
                handles: handles.map(|h| Arc::new(h) as Arc<dyn HandleResolver>),
                flag: Arc::new(FixedGate(true)),
            },
            dir,
        )
    }

    fn actor_inv(
        platform_user_id: Option<&str>,
        mentions: Vec<MentionBinding>,
    ) -> InvocationIdentity {
        InvocationIdentity::new("twitch", platform_user_id.map(str::to_string), mentions)
    }

    fn binding(key: &str, reference: MentionRef) -> MentionBinding {
        MentionBinding {
            key: key.to_string(),
            reference,
        }
    }

    #[tokio::test]
    async fn the_actor_resolves_to_its_community_user_uuid_under_the_host_scope() {
        let alice = Uuid::new_v4();
        let mut dir = FakeDirectory::default();
        dir.by_platform
            .insert(("twitch".into(), "1001".into()), Ok(alice));
        let (w, dir) = wiring(dir, None);
        let inv = actor_inv(Some("1001"), vec![]);
        assert_eq!(resolve_actor(&w, &inv, SCOPE).await.unwrap(), alice);
        assert_eq!(*dir.scopes_seen.lock().unwrap(), vec![SCOPE]);
    }

    #[tokio::test]
    async fn an_actor_with_no_resolved_identity_is_not_linked_never_a_default() {
        let mut dir = FakeDirectory::default();
        dir.by_platform.insert(
            ("twitch".into(), "1001".into()),
            Err(IdentityErrorKind::NotLinked),
        );
        let (w, _) = wiring(dir, None);
        let inv = actor_inv(Some("1001"), vec![]);
        assert_eq!(
            resolve_actor(&w, &inv, SCOPE).await.unwrap_err(),
            IdentityError::NotLinked
        );
    }

    #[tokio::test]
    async fn an_event_with_no_platform_account_id_is_not_linked_without_touching_the_directory() {
        let (w, dir) = wiring(FakeDirectory::default(), None);
        let inv = actor_inv(None, vec![]);
        assert_eq!(
            resolve_actor(&w, &inv, SCOPE).await.unwrap_err(),
            IdentityError::NotLinked
        );
        assert_eq!(dir.lookups.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn a_non_member_actor_is_not_a_member() {
        let (w, _) = wiring(FakeDirectory::default(), None);
        let inv = actor_inv(Some("999"), vec![]);
        assert_eq!(
            resolve_actor(&w, &inv, SCOPE).await.unwrap_err(),
            IdentityError::NotAMember
        );
    }

    #[tokio::test]
    async fn the_nil_uuid_is_never_returned_as_an_identity() {
        let mut dir = FakeDirectory::default();
        dir.by_platform
            .insert(("twitch".into(), "1001".into()), Ok(Uuid::nil()));
        let (w, _) = wiring(dir, None);
        let inv = actor_inv(Some("1001"), vec![]);
        assert!(matches!(
            resolve_actor(&w, &inv, SCOPE).await.unwrap_err(),
            IdentityError::Backend(_)
        ));
    }

    #[tokio::test]
    async fn a_successful_actor_resolution_is_cached_for_the_invocation_and_a_refusal_is_not() {
        let alice = Uuid::new_v4();
        let mut dir = FakeDirectory::default();
        dir.by_platform
            .insert(("twitch".into(), "1001".into()), Ok(alice));
        dir.by_platform.insert(
            ("twitch".into(), "2002".into()),
            Err(IdentityErrorKind::Backend),
        );
        let (w, dir) = wiring(dir, None);

        let inv = actor_inv(Some("1001"), vec![]);
        resolve_actor(&w, &inv, SCOPE).await.unwrap();
        resolve_actor(&w, &inv, SCOPE).await.unwrap();
        assert_eq!(dir.lookups.load(Ordering::SeqCst), 1, "success is cached");

        let failing = actor_inv(Some("2002"), vec![]);
        resolve_actor(&w, &failing, SCOPE).await.unwrap_err();
        resolve_actor(&w, &failing, SCOPE).await.unwrap_err();
        assert_eq!(
            dir.lookups.load(Ordering::SeqCst),
            3,
            "a refusal is re-evaluated"
        );
    }

    #[tokio::test]
    async fn a_platform_id_mention_resolves_by_exact_platform_id_without_hub_api() {
        let bob = Uuid::new_v4();
        let token = Uuid::new_v4().to_string();
        let mut dir = FakeDirectory::default();
        dir.by_platform
            .insert(("twitch".into(), "555".into()), Ok(bob));
        let (w, _) = wiring(dir, None); // NO handle resolver: not needed
        let inv = actor_inv(
            Some("1001"),
            vec![binding(&token, MentionRef::PlatformId("555".into()))],
        );
        assert_eq!(resolve_mention(&w, &inv, SCOPE, &token).await.unwrap(), bob);
    }

    #[tokio::test]
    async fn a_mention_token_is_accepted_bare_wrapped_padded_or_upper_cased() {
        let bob = Uuid::new_v4();
        let token = Uuid::new_v4().to_string();
        let mut dir = FakeDirectory::default();
        dir.by_platform
            .insert(("twitch".into(), "555".into()), Ok(bob));
        let (w, _) = wiring(dir, None);
        let inv = actor_inv(
            None,
            vec![binding(&token, MentionRef::PlatformId("555".into()))],
        );
        for form in [
            token.clone(),
            format!("{{user:{token}}}"),
            format!("  {token}  "),
            token.to_uppercase(),
        ] {
            assert_eq!(resolve_mention(&w, &inv, SCOPE, &form).await.unwrap(), bob);
        }
    }

    #[tokio::test]
    async fn a_handle_mention_resolves_through_hub_api_then_confirms_community_membership() {
        let carol = Uuid::new_v4();
        let token = Uuid::new_v4().to_string();
        let mut dir = FakeDirectory::default();
        dir.members.push(carol);
        let handles = FakeHandles(Ok(carol), AtomicUsize::new(0));
        let (w, dir) = wiring(dir, Some(handles));
        let inv = actor_inv(
            None,
            vec![binding(&token, MentionRef::Handle("carol".into()))],
        );
        assert_eq!(
            resolve_mention(&w, &inv, SCOPE, &token).await.unwrap(),
            carol
        );
        assert_eq!(*dir.scopes_seen.lock().unwrap(), vec![SCOPE]);
    }

    #[tokio::test]
    async fn a_handle_that_exists_in_the_tenant_but_not_this_community_is_not_a_member() {
        let stranger = Uuid::new_v4();
        let token = Uuid::new_v4().to_string();
        let handles = FakeHandles(Ok(stranger), AtomicUsize::new(0));
        let (w, _) = wiring(FakeDirectory::default(), Some(handles)); // stranger is no member
        let inv = actor_inv(
            None,
            vec![binding(&token, MentionRef::Handle("stranger".into()))],
        );
        assert_eq!(
            resolve_mention(&w, &inv, SCOPE, &token).await.unwrap_err(),
            IdentityError::NotAMember
        );
    }

    #[tokio::test]
    async fn hub_api_not_found_and_ambiguous_surface_as_explicit_errors_never_a_guess() {
        let token = Uuid::new_v4().to_string();
        for (make, expected) in [
            (
                (|| IdentityError::NotFound) as fn() -> IdentityError,
                IdentityError::NotFound,
            ),
            (|| IdentityError::Ambiguous, IdentityError::Ambiguous),
        ] {
            let handles = FakeHandles(Err(make), AtomicUsize::new(0));
            let (w, dir) = wiring(FakeDirectory::default(), Some(handles));
            let inv = actor_inv(
                None,
                vec![binding(&token, MentionRef::Handle("bob".into()))],
            );
            assert_eq!(
                resolve_mention(&w, &inv, SCOPE, &token).await.unwrap_err(),
                expected
            );
            assert_eq!(
                dir.lookups.load(Ordering::SeqCst),
                0,
                "no membership lookup follows a failed handle resolution"
            );
        }
    }

    #[tokio::test]
    async fn a_handle_mention_without_a_hub_resolver_is_unavailable_not_a_guess() {
        let token = Uuid::new_v4().to_string();
        let (w, _) = wiring(FakeDirectory::default(), None);
        let inv = actor_inv(
            None,
            vec![binding(&token, MentionRef::Handle("bob".into()))],
        );
        assert!(matches!(
            resolve_mention(&w, &inv, SCOPE, &token).await.unwrap_err(),
            IdentityError::Unavailable(_)
        ));
    }

    #[tokio::test]
    async fn an_unknown_token_is_not_found_and_never_reaches_the_directory_or_hub_api() {
        let handles = FakeHandles(Ok(Uuid::new_v4()), AtomicUsize::new(0));
        let (w, dir) = wiring(FakeDirectory::default(), Some(handles));
        let inv = actor_inv(
            None,
            vec![binding(
                &Uuid::new_v4().to_string(),
                MentionRef::Handle("bob".into()),
            )],
        );
        // A token that was never in the message: not a lookup oracle.
        for probe in [
            Uuid::new_v4().to_string(),
            "@bob".to_string(),
            "bob".to_string(),
            "<@555>".to_string(),
        ] {
            assert_eq!(
                resolve_mention(&w, &inv, SCOPE, &probe).await.unwrap_err(),
                IdentityError::NotFound,
                "{probe}"
            );
        }
        assert_eq!(dir.lookups.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn a_malformed_token_is_invalid() {
        let (w, _) = wiring(FakeDirectory::default(), None);
        let inv = actor_inv(None, vec![]);
        for bad in [
            String::new(),
            "   ".to_string(),
            "x".repeat(MAX_MENTION_TOKEN_LEN + 1),
            "a\nb".to_string(),
            "{user:}".to_string(),
        ] {
            assert!(
                matches!(
                    resolve_mention(&w, &inv, SCOPE, &bad).await.unwrap_err(),
                    IdentityError::Invalid(_)
                ),
                "{bad:?}"
            );
        }
    }

    #[tokio::test]
    async fn an_untokenized_message_is_keyed_by_the_raw_reference_it_carried_and_nothing_else() {
        let bob = Uuid::new_v4();
        let mut dir = FakeDirectory::default();
        dir.by_platform
            .insert(("discord".into(), "555".into()), Ok(bob));
        let (w, _) = wiring(dir, None);
        let event = PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("alice".to_string()),
            payload: serde_json::json!({
                "text": "!steal <@555> now",
                "author_id": "111",
            })
            .as_object()
            .cloned()
            .unwrap(),
            occurred_at: "2026-10-09T00:00:00.000Z".to_string(),
            source: None,
        };
        let inv = InvocationIdentity::for_untokenized_event(&event);
        // One mention: the `@555` inside `<@555>` is the same mention, not a
        // second `@handle` one (the shared scanner skips it).
        assert_eq!(inv.mention_count(), 1);
        assert_eq!(
            resolve_mention(&w, &inv, SCOPE, "<@555>").await.unwrap(),
            bob
        );
        // A reference the message did NOT carry is not resolvable.
        assert_eq!(
            resolve_mention(&w, &inv, SCOPE, "<@556>")
                .await
                .unwrap_err(),
            IdentityError::NotFound
        );
    }

    #[test]
    fn from_event_takes_the_actor_from_user_id_then_author_id() {
        let mk = |payload: serde_json::Value| PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("whoever".to_string()),
            payload: payload.as_object().cloned().unwrap(),
            occurred_at: "2026-10-09T00:00:00.000Z".to_string(),
            source: None,
        };
        let a = InvocationIdentity::from_event(
            &mk(serde_json::json!({"user_id": "1", "author_id": "2"})),
            vec![],
        );
        assert_eq!(a.actor_platform_user_id.as_deref(), Some("1"));
        let b = InvocationIdentity::from_event(&mk(serde_json::json!({"author_id": "2"})), vec![]);
        assert_eq!(b.actor_platform_user_id.as_deref(), Some("2"));
        // The display handle (`event.actor`) is NEVER treated as an account id.
        let c = InvocationIdentity::from_event(&mk(serde_json::json!({})), vec![]);
        assert_eq!(c.actor_platform_user_id, None);
        // A hostile/oversized/control-bearing id is dropped, not bound.
        let d = InvocationIdentity::from_event(&mk(serde_json::json!({"user_id": "1\n2"})), vec![]);
        assert_eq!(d.actor_platform_user_id, None);
        let e = InvocationIdentity::from_event(
            &mk(serde_json::json!({"user_id": "9".repeat(256)})),
            vec![],
        );
        assert_eq!(e.actor_platform_user_id, None);
    }

    #[test]
    fn the_mention_table_is_bounded() {
        let bindings = (0..MAX_MENTIONS_PER_INVOCATION + 10)
            .map(|i| binding(&format!("tok-{i}"), MentionRef::PlatformId(i.to_string())))
            .collect();
        let event = PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: None,
            payload: serde_json::Map::new(),
            occurred_at: "2026-10-09T00:00:00.000Z".to_string(),
            source: None,
        };
        let inv = InvocationIdentity::from_event(&event, bindings);
        assert_eq!(inv.mention_count(), MAX_MENTIONS_PER_INVOCATION);
    }

    #[test]
    fn raw_references_never_appear_in_debug_output() {
        let raw = "super_secret_handle_42";
        let m = MentionRef::Handle(raw.to_string());
        assert!(!format!("{m:?}").contains(raw));
        let b = binding(raw, m.clone());
        assert!(!format!("{b:?}").contains(raw));
        let inv = actor_inv(Some("123456789"), vec![b]);
        let dbg = format!("{inv:?}");
        assert!(!dbg.contains(raw) && !dbg.contains("123456789"), "{dbg}");
    }

    #[test]
    fn wire_codes_are_the_executor_contract() {
        assert_eq!(IdentityError::NotLinked.wire_code(), "not_linked");
        assert_eq!(IdentityError::NotAMember.wire_code(), "not_a_member");
        assert_eq!(IdentityError::NotFound.wire_code(), "not_found");
        assert_eq!(IdentityError::Ambiguous.wire_code(), "ambiguous");
        assert_eq!(
            IdentityError::Invalid("x".into()).wire_code(),
            "invalid_args"
        );
        assert_eq!(
            IdentityError::Unavailable("x".into()).wire_code(),
            "unavailable"
        );
        assert_eq!(IdentityError::Backend("x".into()).wire_code(), "backend");
    }

    #[test]
    fn hub_status_codes_map_to_explicit_errors() {
        use hub_client::HubClientError as E;
        let grpc = |code: tonic::Code| E::Grpc(tonic::Status::new(code, "detail with @secret"));
        assert_eq!(
            hub_error_to_identity(&grpc(tonic::Code::NotFound)),
            IdentityError::NotFound
        );
        assert_eq!(
            hub_error_to_identity(&grpc(tonic::Code::FailedPrecondition)),
            IdentityError::Ambiguous
        );
        assert!(matches!(
            hub_error_to_identity(&grpc(tonic::Code::InvalidArgument)),
            IdentityError::Invalid(_)
        ));
        for code in [
            tonic::Code::Unavailable,
            tonic::Code::DeadlineExceeded,
            tonic::Code::Internal,
            tonic::Code::Unauthenticated,
            tonic::Code::PermissionDenied,
        ] {
            let err = hub_error_to_identity(&grpc(code));
            assert!(matches!(err, IdentityError::Unavailable(_)), "{code:?}");
            assert!(
                !err.to_string().contains("secret"),
                "hub-api message text must never be forwarded"
            );
        }
        assert!(matches!(
            hub_error_to_identity(&E::CircuitOpen),
            IdentityError::Unavailable(_)
        ));
    }
}
