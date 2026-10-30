//! `tool.invoke` stage-side handler (bundle-MCP-tools P3b): a guest's
//! `tool.invoke` arrives as the host call `Db:"tool.invoke"` with
//! `{target, tool, args, idempotency_key}`; this module authorizes it
//! (`bundle_tool_call::plan_tool_call`: zero-trust validation, self/cycle/depth
//! guard, the P1 matrix, the P2 `tool.call` gate, target resolution) and runs
//! the nested invoke of the target service bundle's `tool-provider.call`.
//!
//! The policy is shared with `core/svc_action` through `bundle-tool-call`; this
//! file is only the svc_process-specific glue (the per-invocation capability
//! set, the connection registry, and building the TARGET's own capabilities).
//!
//! Scope handling: the nested invoke's capability set is derived from the
//! caller's by [`StageCapabilities::for_tool_target`] -- the host-asserted
//! tenant/community are carried UNCHANGED, the app/version become the TARGET's
//! (so the target authorizes under its OWN grants), and the call chain grows by
//! one. The caller guest never names a community, tenant, or actor.
//!
//! Same-stage only: the target must be loaded on THIS stage's executor. A
//! target that is not (another stage's bundle) fails loudly as
//! `target_unavailable` -- cross-stage calls need the ack-queue path and are
//! not built yet.

use std::sync::Arc;
use std::time::Instant;

use bundle_active_set::ActiveVersionSnapshot;
use bundle_host_kv::KvBackend;
use bundle_tool_call::ToolRefusal;
use penguin_bundle_host::wire::{HostCallBody, HostResultError, TraceContext};

use super::{denied, CapabilityHandler, StageCapabilities};
use crate::host_api::ConnectionRegistry;
use crate::stage_metrics;

/// The per-invocation `tool.invoke` wiring: what resolves targets, which
/// executor connection runs the nested call, and where this invocation sits in
/// the call chain / deadline budget. Built by `crate::spine` for a top-level
/// invocation (`chain = [app_id]`) and derived per hop by
/// [`StageCapabilities::for_tool_target`].
#[derive(Clone)]
pub struct ToolContext {
    /// Resolved at call time (`.active()`), never cached: a reconnect replaces
    /// the connection.
    pub connections: Arc<ConnectionRegistry>,
    /// The live active-version snapshot -- the sole source of a target's
    /// platform-installed digest/version and the P1 tool facts.
    pub snapshot: ActiveVersionSnapshot,
    /// The call chain INCLUDING this invocation's own app as the last element.
    pub chain: Vec<String>,
    /// When this invocation's executor budget runs out; bounds every nested
    /// call (`None` = unbounded outer, the nested call is still capped at 8s).
    pub deadline: Option<Instant>,
    /// The caller's trace context, forwarded onto the nested invoke.
    pub trace: Option<TraceContext>,
}

impl<K: KvBackend> StageCapabilities<K> {
    /// Attaches the `tool.invoke` wiring for this invocation.
    #[must_use]
    pub fn with_tool(mut self, tool: ToolContext) -> Self {
        self.tool = Some(tool);
        self
    }
}

impl<K: KvBackend + 'static> StageCapabilities<K> {
    /// Derives the capability set the nested TARGET invoke runs under: the same
    /// process-wide wirings and host-asserted tenant/community/verified scope,
    /// but the TARGET's app id + platform-installed version (its own grants) and
    /// the grown call chain / shortened deadline. The caller's identity facts
    /// ride along unchanged as host-derived context (the target decides whether
    /// to use them; they are never app-suppliable).
    fn for_tool_target(
        &self,
        target_app_id: String,
        target_version: i64,
        ctx: ToolContext,
    ) -> Self {
        Self {
            tenant: self.tenant.clone(),
            community: self.community.clone(),
            app_id: target_app_id,
            tenant_id: self.tenant_id,
            community_id: self.community_id,
            app_version: target_version,
            kv: self.kv.clone(),
            egress: Arc::clone(&self.egress),
            gate: Arc::clone(&self.gate),
            db: self.db.clone(),
            reputation: self.reputation.clone(),
            economy: self.economy.clone(),
            identity: self.identity.clone(),
            ai: self.ai.clone(),
            ai_deadline: ctx.deadline.or(self.ai_deadline),
            invocation_identity: self.invocation_identity.clone(),
            verified_scope: self.verified_scope,
            stage_metrics: Arc::clone(&self.stage_metrics),
            tool: Some(ctx),
        }
    }

    /// `tool.invoke` (see the module doc). Returns the target's `{ok}` /
    /// `{tool_error}` envelope on success; every refusal is a typed host-call
    /// error carrying one of the snake_case codes the executor maps.
    pub(super) async fn handle_tool(
        &self,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostResultError> {
        if call.op != "tool.invoke" {
            return Err(denied(
                "unknown_op",
                format!("tool op {:?} not supported", call.op),
            ));
        }
        let started = Instant::now();
        // Set by `run_tool_invoke` once the target resolved: only then are the
        // names host-verified and safe as metric labels.
        let mut resolved: Option<(String, String)> = None;
        let outcome = self.run_tool_invoke(call, &mut resolved).await;
        let (target, tool) = resolved.unwrap_or_else(|| {
            (
                stage_metrics::TOOL_UNRESOLVED.to_string(),
                stage_metrics::TOOL_UNRESOLVED.to_string(),
            )
        });
        self.stage_metrics.record_tool_call(
            &target,
            &tool,
            outcome.as_ref().map_or_else(ToolRefusal::code, |_| "ok"),
            started.elapsed(),
        );
        // PII-free: ids, tool name and outcome only -- never args.
        match &outcome {
            Ok(_) => tracing::debug!(
                app_id = %self.app_id, community_id = self.community_id,
                outcome = "ok", "tool.invoke served"
            ),
            Err(r) => tracing::info!(
                app_id = %self.app_id, community_id = self.community_id,
                outcome = r.code(), "tool.invoke refused"
            ),
        }
        outcome.map_err(|r| denied(r.code(), r.message()))
    }

    async fn run_tool_invoke(
        &self,
        call: &HostCallBody,
        resolved: &mut Option<(String, String)>,
    ) -> Result<serde_json::Value, ToolRefusal> {
        let Some(ctx) = &self.tool else {
            return Err(ToolRefusal::TargetUnavailable(
                "tool.invoke is not wired for this activation".to_string(),
            ));
        };
        // 1-5: validate, guard, matrix, gate, resolve. No executor slot is
        // touched until this returns.
        let req = bundle_tool_call::parse_request(&call.args)?;
        let plan = bundle_tool_call::plan_tool_call(
            &self.app_id,
            &ctx.chain,
            req,
            &ctx.snapshot,
            &self.gate,
            &self.gate_scope(),
        )?;
        *resolved = Some((plan.target_app_id.clone(), plan.tool.clone()));
        tracing::debug!(
            app_id = %self.app_id, target = %plan.target_app_id, tool = %plan.tool,
            community_id = self.community_id, write = plan.write, depth = plan.next_chain.len() - 1,
            "tool.invoke authorized"
        );

        // 6. Nested invoke on the active executor connection.
        let deadline = bundle_tool_call::nested_deadline(ctx.deadline)?;
        let conn = ctx.connections.active().ok_or_else(|| {
            ToolRefusal::TargetUnavailable("no executor connection is active".to_string())
        })?;
        let nested_ctx = ToolContext {
            chain: plan.next_chain.clone(),
            deadline: Some(Instant::now() + deadline),
            ..ctx.clone()
        };
        let target_caps: Arc<dyn CapabilityHandler> = Arc::new(self.for_tool_target(
            plan.target_app_id.clone(),
            plan.target_version,
            nested_ctx,
        ));
        let result =
            crate::spine::invoke_tool_call(&conn, &plan, deadline, ctx.trace.clone(), target_caps)
                .await;
        if let Err(ToolRefusal::TargetUnavailable(_)) = &result {
            tracing::warn!(
                app_id = %self.app_id, target = %plan.target_app_id, tool = %plan.tool,
                "tool target unavailable on this stage's executor (cross-stage tool \
                 calls are not yet supported)"
            );
        }
        result
    }
}

#[cfg(test)]
#[path = "tool_tests.rs"]
mod tool_tests;
