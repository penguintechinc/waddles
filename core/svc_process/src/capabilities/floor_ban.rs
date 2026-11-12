//! Automated hard ban: the host-asserted `system:reputation_floor` actor for the platform
//! `<platform>.moderation.ban` tool (the reputation floor enforcement path).
//!
//! Before this gate a floor ban from the reputation service failed `forbidden`: the ban tool
//! declares `required_scope = community:moderate` and an automated call has no human actor. The
//! host now attaches an AUTOMATED actor, but only when ALL of these hold (each is host-derived,
//! none is guest input, every failure falls through to the ordinary human-actor path, which
//! refuses a caller without `community:moderate`):
//!
//! * the call is `<platform>.moderation.ban` on `waddles.core.service.<platform>` with a floor
//!   reason (`community_hard_floor` / `tenant_floor`);
//! * the calling app is the first-party reputation SERVICE, reached as exactly the chain
//!   `[logic, service]`, where `logic` is the tenant's active reputation-logic app
//!   (`reputation_active_logic_app`; NULL / error / more than one = not eligible);
//! * a reputation apply already succeeded in THIS invocation (`InvocationIdentity` flag set by
//!   the host `reputation.apply*` handler; fresh on every tool hop, so the actor is never
//!   inherited across a hop);
//! * the `waddles.reputation-auto-ban` flag is ON and the per-scope switch is enabled (the SQL
//!   re-verification `reputation_verify_floor_breach` is false when the switch is off or the
//!   score is not below the floor).
//!
//! An eligible call then, per target community: claims an idempotency/rate-limit slot
//! (`reputation_floor_ban_begin`), re-verifies the breach in SQL, runs the ban through the normal
//! authorization (the caller's per-community grant still applies) with the automated actor, and
//! records the outcome (`reputation_floor_ban_record`): the audit trail and the durable retry
//! record. The tenant floor fans out across every community of the tenant (host-enumerated) with
//! bounded concurrency; failures are retried from the durable record
//! (`reputation_floor_ban_due`), never silently dropped.

use std::collections::BTreeSet;

use bundle_host_kv::KvBackend;
use bundle_host_reputation::{
    floor_sql_key, FloorBanBegin, FloorBanOutcome, FloorScope, ReputationScope, FLOOR_ACTOR,
};
use bundle_tool_call::{ActorContext, ToolRefusal, ToolRequest};
use futures_util::stream::{self, StreamExt};
use uuid::Uuid;

use super::{
    gameserver_net, StageCapabilities, ToolContext, REPUTATION_MODERATE_SCOPE,
    REPUTATION_SERVICE_APP_ID,
};

/// Communities banned concurrently by one tenant-floor fan-out.
const FLOOR_BAN_CONCURRENCY: usize = 4;
/// Durable retries picked up (and leased) by one floor-ban call.
const RETRY_SWEEP_LIMIT: u32 = 5;
/// Prefix of the platform service bundle's app id.
const SERVICE_PREFIX: &str = "waddles.core.service.";

/// What one community's floor ban came to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum FloorResult {
    /// Banned (or an idempotent replay of an already-recorded ban).
    Done,
    /// Re-verification said the floor is not breached (or the switch is off).
    Refused,
    /// The per-community floor-ban throttle was hit (recorded, not retried automatically).
    RateLimited,
    /// The ban failed with this stable code; a durable retry is recorded.
    Failed(String),
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

/// The tool request for one community's ban, keyed by the SQL-form idempotency key.
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
        if !self.floor_ban_eligible(ctx).await {
            return None;
        }
        Some(self.run_floor_ban(ctx, req, &platform, floor).await)
    }

    /// The host-side eligibility test (module doc); fails closed on every unknown.
    async fn floor_ban_eligible(&self, ctx: &ToolContext) -> bool {
        if self.app_id != REPUTATION_SERVICE_APP_ID || self.community_id == 0 {
            return false;
        }
        let [origin, caller] = ctx.chain.as_slice() else {
            return false;
        };
        if *caller != self.app_id || *origin == self.app_id {
            return false;
        }
        let applied = self
            .invocation_identity
            .as_deref()
            .is_some_and(|i| i.reputation_applied());
        if !applied {
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
        let user = parse_floor_args(req)?;
        let raw_key = req.idempotency_key.as_deref().ok_or_else(|| {
            ToolRefusal::InvalidArgs("a floor ban requires an idempotency key".to_string())
        })?;
        let key = floor_sql_key(raw_key)
            .map_err(|_| ToolRefusal::InvalidArgs("idempotency key is not usable".to_string()))?;
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
        // Durable retries of earlier failures, bounded and best-effort (never fails this call).
        self.retry_due_floor_bans(ctx).await;
        summarise(floor, &results)
    }

    /// One community's ban: claim the slot, re-verify, act, record.
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
        let store = &wiring.store;
        let origin_scope = ReputationScope {
            tenant_id: self.tenant_id,
            community_id: origin,
            app_id: self.app_id.clone(),
        };
        match store
            .floor_ban_begin(&origin_scope, target, user, floor, key, platform)
            .await
        {
            Ok(FloorBanBegin::Proceed) => {}
            Ok(FloorBanBegin::AlreadyDone) => return FloorResult::Done,
            Ok(FloorBanBegin::RateLimited) => {
                tracing::warn!(
                    community_id = target,
                    scope = floor.reason(),
                    "floor ban rate limited"
                );
                return FloorResult::RateLimited;
            }
            Err(e) => return FloorResult::Failed(e.wire_code().to_string()),
        }
        let target_scope = ReputationScope {
            tenant_id: self.tenant_id,
            community_id: target,
            app_id: self.app_id.clone(),
        };
        let (outcome, code, result) =
            match store.verify_floor_breach(&target_scope, user, floor).await {
                Ok(true) => match self.execute_floor_ban(ctx, req, target).await {
                    Ok(_) => (FloorBanOutcome::Done, "", FloorResult::Done),
                    Err(r) => (
                        FloorBanOutcome::Failed,
                        r.code(),
                        FloorResult::Failed(r.code().to_string()),
                    ),
                },
                Ok(false) => (
                    FloorBanOutcome::Refused,
                    "floor_not_breached",
                    FloorResult::Refused,
                ),
                Err(e) => (
                    FloorBanOutcome::Failed,
                    e.wire_code(),
                    FloorResult::Failed(e.wire_code().to_string()),
                ),
            };
        if let Err(e) = store
            .floor_ban_record(&origin_scope, target, user, floor, key, outcome, code)
            .await
        {
            // The audit/retry record is part of the contract: surface it, never swallow it.
            tracing::error!(
                community_id = target,
                outcome = e.wire_code(),
                "floor ban outcome could not be recorded"
            );
            return FloorResult::Failed("audit_failed".to_string());
        }
        tracing::info!(
            community_id = target, origin_community_id = origin, scope = floor.reason(),
            actor = FLOOR_ACTOR, outcome = ?outcome, "floor ban audited"
        );
        result
    }

    /// Runs the ban in `target` through the NORMAL authorization (the caller's per-community
    /// request/grant, the P1 matrix, the write key) with the automated actor, then the nested
    /// invoke under capabilities re-scoped to that community.
    async fn execute_floor_ban(
        &self,
        ctx: &ToolContext,
        req: &ToolRequest,
        target: i32,
    ) -> Result<serde_json::Value, ToolRefusal> {
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
        let plan = authorized.into_plan(&floor_actor())?;
        scoped.run_nested(ctx, &plan).await
    }

    /// Picks up due durable retries (failed attempts past their backoff, crashed pending ones)
    /// of this tenant and re-runs each; the outcome is recorded by `floor_ban_one`.
    async fn retry_due_floor_bans(&self, ctx: &ToolContext) {
        let Some(wiring) = &self.reputation else {
            return;
        };
        let due = match wiring
            .store
            .floor_ban_due(&self.reputation_scope(), RETRY_SWEEP_LIMIT)
            .await
        {
            Ok(due) => due,
            Err(e) => {
                tracing::warn!(
                    outcome = e.wire_code(),
                    "floor ban retry sweep could not read the retry record"
                );
                return;
            }
        };
        for d in due {
            if !valid_platform(&d.platform) {
                tracing::error!(
                    community_id = d.community_id,
                    "floor ban retry row has an invalid platform"
                );
                continue;
            }
            let req = ban_request(&d.platform, d.scope, d.user, &d.key);
            let result = self
                .floor_ban_one(
                    ctx,
                    &d.platform,
                    d.scope,
                    d.user,
                    &d.key,
                    &req,
                    d.community_id,
                    d.origin_community_id,
                )
                .await;
            tracing::info!(
                community_id = d.community_id, scope = d.scope.reason(), result = ?result,
                "floor ban durable retry ran"
            );
        }
    }

    /// A copy of these capabilities scoped to `community_id` of the same tenant (the tenant-floor
    /// fan-out target): same app, wirings and invocation identity, community-specific scope so
    /// grants, snapshot lookups and idempotency namespaces are the TARGET community's own.
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
            gs_sessions: std::sync::Arc::new(gameserver_net::SessionTable::default()),
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

/// Folds the per-community results into the tool result: all banned (or replayed) = ok; any
/// failure/throttle = a typed, retriable refusal that names the counts only; nothing banned
/// because the floor is not breached = forbidden.
pub(super) fn summarise(
    floor: FloorScope,
    results: &[FloorResult],
) -> Result<serde_json::Value, ToolRefusal> {
    let total = results.len();
    let done = results.iter().filter(|r| **r == FloorResult::Done).count();
    let refused = results
        .iter()
        .filter(|r| **r == FloorResult::Refused)
        .count();
    let failed = total - done - refused;
    if failed > 0 {
        let first = results
            .iter()
            .find_map(|r| match r {
                FloorResult::Failed(c) => Some(c.as_str()),
                FloorResult::RateLimited => Some("rate_limited"),
                _ => None,
            })
            .unwrap_or("unknown");
        return Err(ToolRefusal::TargetUnavailable(format!(
            "floor ban incomplete ({first}): {failed} of {total} communities failed; \
             durable retry recorded"
        )));
    }
    if done == 0 {
        return Err(ToolRefusal::Forbidden(format!(
            "floor_not_breached: {} is not currently breached",
            floor.reason()
        )));
    }
    Ok(serde_json::json!({ "ok": { "communities": total, "banned": done } }))
}
