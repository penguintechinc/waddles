//! Automated hard ban: the host-asserted `system:reputation_floor` actor for the platform
//! `<platform>.moderation.ban` tool (the reputation floor enforcement path), delivered through the
//! EXISTING outbound moderation path rather than a cross-stage tool call.
//!
//! # Why an outbox
//!
//! The platform service bundle (`waddles.core.service.discord`, ...) is a connector-tools bundle
//! that does not run on this (process) stage, and cross-stage tool calls are unsupported, so a
//! nested `tool.invoke` can never reach Discord. Instead this stage RECORDS the verified intent in
//! the floor-ban outbox (`reputation_floor_ban_enqueue`), and the ACTION stage
//! (`svc_action::floor_ban_dispatch`) leases each queued row, emits the signed outbound `mod.ban`
//! command that `svc_ingest`'s `sender_exec` delivers to the platform, and marks the row done only
//! on the sender's CONFIRMED result. The outbox row id is the correlation id end to end.
//!
//! # Eligibility
//!
//! The call is handled here only when ALL of these hold (each is host-derived, none is guest
//! input; every failure falls through to the ordinary human-actor path, which refuses a caller
//! without `community:moderate`):
//!
//! * the call is `<platform>.moderation.ban` on `waddles.core.service.<platform>` with a floor
//!   reason (`community_hard_floor` / `tenant_floor`) for a platform the action stage can deliver;
//! * the calling app is the first-party reputation SERVICE, reached as exactly the chain
//!   `[logic, service]`, where `logic` is the tenant's active reputation-logic app;
//! * a reputation apply of THIS user in THIS community really applied (`applied == true`) in THIS
//!   invocation ([`InvocationIdentity::reputation_applied_for`]): a ban can only follow a real
//!   score change of the banned user, never a replay and never a different user;
//! * the `waddles.reputation-auto-ban` flag is ON.
//!
//! The breach is re-verified in SQL inside the enqueue transaction (the per-scope switch off or a
//! score not below the floor is a recorded refusal). The idempotency key is derived HERE from the
//! host-derived event id, the user and the scope -- never from a guest value -- and the hourly
//! quota is counted under a per-community advisory lock in the same transaction.
//!
//! [`InvocationIdentity::reputation_applied_for`]: crate::identity::InvocationIdentity

use std::collections::BTreeSet;

use bundle_host_kv::KvBackend;
use bundle_host_reputation::{FloorEnqueue, FloorScope, ReputationScope, FLOOR_ACTOR};
use bundle_tool_call::{ActorContext, ToolRefusal, ToolRequest};
use futures_util::stream::{self, StreamExt};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use super::{StageCapabilities, ToolContext, REPUTATION_MODERATE_SCOPE, REPUTATION_SERVICE_APP_ID};

/// Communities enqueued concurrently by one tenant-floor fan-out.
const FLOOR_BAN_CONCURRENCY: usize = 4;
/// Prefix of the platform service bundle's app id.
const SERVICE_PREFIX: &str = "waddles.core.service.";
/// Platforms whose floor ban the ACTION stage can deliver (`svc_action::floor_ban_dispatch`
/// serves the same set). Anything else is refused loudly up front, never queued to die.
pub(super) const FLOOR_BAN_PLATFORMS: &[&str] = &["discord"];

/// What one community's floor-ban enqueue came to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum FloorResult {
    /// Queued for delivery (or an idempotent replay of an already-queued/delivered slot).
    Queued,
    /// Re-verification said the floor is not breached (or the switch is off).
    Refused,
    /// The per-community floor-ban quota was hit; recorded and retried after the window.
    RateLimited,
    /// The enqueue failed with this stable code; nothing was recorded.
    Failed(String),
}

impl FloorResult {
    /// The low-cardinality metric label of this result.
    fn label(&self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Refused => "refused",
            Self::RateLimited => "rate_limited",
            Self::Failed(_) => "error",
        }
    }
}

/// The automated actor: the fixed system identity holding ONLY the moderate scope.
pub(super) fn floor_actor() -> ActorContext {
    ActorContext {
        actor: Some(FLOOR_ACTOR.to_string()),
        scopes: vec![REPUTATION_MODERATE_SCOPE.to_string()],
    }
}

fn valid_platform(p: &str) -> bool {
    let mut bytes = p.bytes();
    matches!(bytes.next(), Some(b) if b.is_ascii_lowercase())
        && (2..=32).contains(&p.len())
        && bytes.all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
}

/// Derives the HOST-side idempotency key of a floor ban from the invocation's host-derived event
/// id, the user and the scope -- never a guest-chosen value. The key is in the SQL alphabet
/// (`^[a-z0-9._:-]{1,128}$`): `fb:` + a 40-hex SHA-256 prefix, domain-separated.
pub(super) fn floor_ban_key(event_id: &str, user: Uuid, scope: FloorScope) -> String {
    let mut hash = Sha256::new();
    for part in ["floor-ban-v1", event_id, &user.to_string(), scope.reason()] {
        hash.update(part.as_bytes());
        hash.update([0x1f]);
    }
    let digest = hash.finalize();
    let mut key = String::with_capacity(43);
    key.push_str("fb:");
    for byte in digest.iter().take(20) {
        key.push_str(&format!("{byte:02x}"));
    }
    key
}

/// Recognises a floor-ban CLAIM: `<platform>.moderation.ban` on the matching core service with a
/// floor `reason`. Anything else (including a ban with another reason) is not a claim and takes
/// the ordinary path. A claim is not yet an authorization.
pub(super) fn detect_claim(req: &ToolRequest) -> Option<(String, FloorScope)> {
    let platform = req.tool.strip_suffix(".moderation.ban")?;
    if !valid_platform(platform) || req.target != format!("{SERVICE_PREFIX}{platform}") {
        return None;
    }
    let reason = req.args.get("reason")?.as_str()?;
    Some((platform.to_string(), FloorScope::from_reason(reason)?))
}

/// Strict argument shape of an eligible claim: exactly `{user: UUID, reason}`.
fn parse_floor_args(req: &ToolRequest) -> Result<Uuid, ToolRefusal> {
    let bad = |m: &str| ToolRefusal::InvalidArgs(m.to_string());
    let obj = req
        .args
        .as_object()
        .ok_or_else(|| bad("args must be an object"))?;
    let allowed: BTreeSet<&str> = ["user", "reason"].into_iter().collect();
    if obj.keys().any(|k| !allowed.contains(k.as_str())) {
        return Err(bad("a floor ban takes only user and reason"));
    }
    obj.get("user")
        .and_then(|v| v.as_str())
        .and_then(|s| Uuid::parse_str(s).ok())
        .ok_or_else(|| bad("user must be a UUID string"))
}

/// The tool request authorised per community (grant check), keyed by the host-derived key.
fn ban_request(platform: &str, scope: FloorScope, user: Uuid, key: &str) -> ToolRequest {
    ToolRequest {
        target: format!("{SERVICE_PREFIX}{platform}"),
        tool: format!("{platform}.moderation.ban"),
        args: serde_json::json!({ "user": user.to_string(), "reason": scope.reason() }),
        idempotency_key: Some(key.to_string()),
    }
}

impl<K: KvBackend + 'static> StageCapabilities<K> {
    /// `Some(result)` when `req` is an ELIGIBLE automated floor ban (see the module doc);
    /// `None` hands the call to the ordinary human-actor path unchanged.
    pub(super) async fn try_floor_ban(
        &self,
        ctx: &ToolContext,
        req: &ToolRequest,
    ) -> Option<Result<serde_json::Value, ToolRefusal>> {
        let (platform, floor) = detect_claim(req)?;
        if !self.floor_ban_chain_eligible(ctx).await {
            return None;
        }
        let user = match parse_floor_args(req) {
            Ok(user) => user,
            Err(refusal) => return Some(Err(refusal)),
        };
        // The banned user's score must have really changed HERE, in this invocation.
        let applied = self
            .invocation_identity
            .as_deref()
            .is_some_and(|i| i.reputation_applied_for(user, self.community_id));
        if !applied {
            return None;
        }
        Some(self.run_floor_ban(ctx, req, &platform, floor, user).await)
    }

    /// The host-side chain/app eligibility test (module doc); fails closed on every unknown.
    async fn floor_ban_chain_eligible(&self, ctx: &ToolContext) -> bool {
        if self.app_id != REPUTATION_SERVICE_APP_ID || self.community_id == 0 {
            return false;
        }
        let [origin, caller] = ctx.chain.as_slice() else {
            return false;
        };
        if *caller != self.app_id || *origin == self.app_id {
            return false;
        }
        let Some(wiring) = &self.reputation else {
            return false;
        };
        match wiring
            .store
            .active_logic_app(&self.reputation_scope())
            .await
        {
            Ok(Some(active)) => active == *origin,
            Ok(None) => false,
            Err(e) => {
                tracing::warn!(
                    community_id = self.community_id,
                    outcome = e.wire_code(),
                    "floor ban not eligible: active reputation-logic app unresolved"
                );
                false
            }
        }
    }

    async fn run_floor_ban(
        &self,
        ctx: &ToolContext,
        req: &ToolRequest,
        platform: &str,
        floor: FloorScope,
        user: Uuid,
    ) -> Result<serde_json::Value, ToolRefusal> {
        let forbidden = |m: &str| ToolRefusal::Forbidden(m.to_string());
        let Some(wiring) = &self.reputation else {
            return Err(forbidden("floor_ban_unavailable: reputation is not wired"));
        };
        let flag_on = match &wiring.auto_ban_flag {
            Some(f) => f.enabled().await,
            None => false,
        };
        if !flag_on {
            tracing::info!(
                community_id = self.community_id,
                scope = floor.reason(),
                "floor ban refused: waddles.reputation-auto-ban is OFF"
            );
            return Err(forbidden(
                "floor_ban_disabled: waddles.reputation-auto-ban is OFF",
            ));
        }
        if !FLOOR_BAN_PLATFORMS.contains(&platform) {
            tracing::warn!(
                community_id = self.community_id,
                platform,
                "floor ban refused: the action stage cannot deliver this platform's ban"
            );
            return Err(forbidden(
                "floor_ban_unsupported_platform: no outbound ban path for this platform",
            ));
        }
        // The retry token is only a presence check (a write tool requires one): the stored key is
        // derived from the host-derived event id, never from this guest-chosen value.
        if req.idempotency_key.as_deref().is_none_or(str::is_empty) {
            return Err(ToolRefusal::InvalidArgs(
                "a floor ban requires an idempotency key".to_string(),
            ));
        }
        let event_id = self
            .invocation_identity
            .as_deref()
            .and_then(|i| i.event_id())
            .ok_or_else(|| forbidden("floor_ban_unavailable: the invocation has no event id"))?;
        let key = floor_ban_key(event_id, user, floor);
        let origin = self.community_id;
        let targets: Vec<i32> = match floor {
            FloorScope::CommunityHard => vec![origin],
            FloorScope::Tenant => wiring
                .store
                .tenant_communities(&self.reputation_scope())
                .await
                .map_err(|e| {
                    tracing::error!(
                        outcome = e.wire_code(),
                        "tenant community enumeration failed"
                    );
                    ToolRefusal::TargetUnavailable(
                        "tenant communities could not be enumerated; nothing was banned"
                            .to_string(),
                    )
                })?,
        };
        let request = ban_request(platform, floor, user, &key);
        let results: Vec<FloorResult> = stream::iter(targets)
            .map(|target| {
                self.floor_ban_one(ctx, platform, floor, user, &key, &request, target, origin)
            })
            .buffer_unordered(FLOOR_BAN_CONCURRENCY)
            .collect()
            .await;
        for r in &results {
            self.stage_metrics
                .record_floor_ban_enqueue(floor.reason(), r.label());
        }
        summarise(floor, &results)
    }

    /// One community's enqueue: authorise the caller's per-community grant, then record the
    /// verified, quota-checked outbox row. Delivery is the action stage's job.
    #[allow(clippy::too_many_arguments)]
    async fn floor_ban_one(
        &self,
        ctx: &ToolContext,
        platform: &str,
        floor: FloorScope,
        user: Uuid,
        key: &str,
        req: &ToolRequest,
        target: i32,
        origin: i32,
    ) -> FloorResult {
        let Some(wiring) = &self.reputation else {
            return FloorResult::Failed("unavailable".to_string());
        };
        if let Err(r) = self.authorize_floor_ban(ctx, req, target) {
            tracing::info!(
                community_id = target,
                scope = floor.reason(),
                outcome = r.code(),
                "floor ban not enqueued: authorization refused"
            );
            return FloorResult::Failed(r.code().to_string());
        }
        let origin_scope = ReputationScope {
            tenant_id: self.tenant_id,
            community_id: origin,
            app_id: self.app_id.clone(),
        };
        match wiring
            .store
            .floor_ban_enqueue(&origin_scope, target, user, floor, key, platform)
            .await
        {
            Ok(FloorEnqueue::Queued(id)) => {
                tracing::info!(
                    community_id = target,
                    origin_community_id = origin,
                    scope = floor.reason(),
                    actor = FLOOR_ACTOR,
                    floor_ban_id = id,
                    "floor ban queued for the action stage"
                );
                FloorResult::Queued
            }
            Ok(FloorEnqueue::AlreadyKnown(_)) => FloorResult::Queued,
            Ok(FloorEnqueue::RateLimited(id)) => {
                tracing::error!(
                    community_id = target,
                    scope = floor.reason(),
                    floor_ban_id = id,
                    "ALERT floor ban rate limited: recorded, retried after the quota window"
                );
                FloorResult::RateLimited
            }
            Ok(FloorEnqueue::Refused(_)) => FloorResult::Refused,
            Err(e) => {
                tracing::error!(
                    community_id = target,
                    outcome = e.wire_code(),
                    "floor ban could not be enqueued"
                );
                FloorResult::Failed(e.wire_code().to_string())
            }
        }
    }

    /// Runs the planner's authorization for the ban in `target` (the caller's per-community
    /// request/grant, the P1 matrix, the write key, and the tool's `required_scope` against the
    /// automated actor) without executing anything: delivery goes through the outbox.
    fn authorize_floor_ban(
        &self,
        ctx: &ToolContext,
        req: &ToolRequest,
        target: i32,
    ) -> Result<(), ToolRefusal> {
        let scoped = self.rescoped(target);
        let authorized = bundle_tool_call::authorize_tool_call(
            &scoped.app_id,
            &ctx.chain,
            req.clone(),
            &ctx.snapshot,
            &scoped.gate,
            &scoped.gate_scope(),
        )?;
        if ctx.read_only && authorized.is_write() {
            return Err(ToolRefusal::Forbidden(
                "a tool invoked as read may not invoke a write tool".to_string(),
            ));
        }
        authorized.into_plan(&floor_actor()).map(|_| ())
    }

    /// A copy of these capabilities scoped to `community_id` of the same tenant (the tenant-floor
    /// fan-out target): same app, wirings and invocation identity, community-specific scope so
    /// grants and snapshot lookups are the TARGET community's own.
    fn rescoped(&self, community_id: i32) -> Self {
        Self {
            tenant: self.tenant.clone(),
            community: Some(community_id.to_string()),
            app_id: self.app_id.clone(),
            tenant_id: self.tenant_id,
            community_id,
            app_version: self.app_version,
            kv: self.kv.clone(),
            egress: std::sync::Arc::clone(&self.egress),
            gate: std::sync::Arc::clone(&self.gate),
            db: self.db.clone(),
            reputation: self.reputation.clone(),
            economy: self.economy.clone(),
            platform_table: self.platform_table.clone(),
            object_storage: self.object_storage.clone(),
            gameserver_net: self.gameserver_net.clone(),
            gs_sessions: std::sync::Arc::new(super::gameserver_net::SessionTable::default()),
            identity: self.identity.clone(),
            ai: self.ai.clone(),
            ai_deadline: self.ai_deadline,
            media: self.media.clone(),
            media_deadline: self.media_deadline,
            invocation_identity: self.invocation_identity.clone(),
            verified_scope: Some((self.tenant_id, community_id)),
            stage_metrics: std::sync::Arc::clone(&self.stage_metrics),
            tool: self.tool.clone(),
            tool_caller: self.tool_caller.clone(),
        }
    }
}

/// Folds the per-community results into the tool result: all queued (or replayed) = ok; any
/// failure/throttle = a typed refusal that names the counts only (a rate-limited ban is recorded
/// and retried after the quota window by the action stage; a failed enqueue recorded nothing and
/// is retried by the redelivered event); nothing queued because the floor is not breached =
/// forbidden.
pub(super) fn summarise(
    floor: FloorScope,
    results: &[FloorResult],
) -> Result<serde_json::Value, ToolRefusal> {
    let total = results.len();
    let queued = results
        .iter()
        .filter(|r| **r == FloorResult::Queued)
        .count();
    let refused = results
        .iter()
        .filter(|r| **r == FloorResult::Refused)
        .count();
    let limited = results
        .iter()
        .filter(|r| **r == FloorResult::RateLimited)
        .count();
    let failed = total - queued - refused - limited;
    if failed > 0 || limited > 0 {
        let first = results
            .iter()
            .find_map(|r| match r {
                FloorResult::Failed(c) => Some(c.as_str()),
                FloorResult::RateLimited => Some("rate_limited"),
                _ => None,
            })
            .unwrap_or("unknown");
        return Err(ToolRefusal::TargetUnavailable(format!(
            "floor ban incomplete ({first}): {limited} rate-limited (recorded, retried after the \
             quota window), {failed} failed (not recorded) of {total} communities"
        )));
    }
    if queued == 0 {
        return Err(ToolRefusal::Forbidden(format!(
            "floor_not_breached: {} is not currently breached",
            floor.reason()
        )));
    }
    Ok(serde_json::json!({ "ok": { "communities": total, "queued": queued } }))
}
