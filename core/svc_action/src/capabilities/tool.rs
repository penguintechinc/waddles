//! `tool.invoke` stage-side handler (bundle-MCP-tools P3b) -- the svc_action
//! twin of `core/svc_process/src/capabilities/tool.rs`. A guest's `tool.invoke`
//! arrives as `Db:"tool.invoke"` with `{target, tool, args, idempotency_key}`;
//! the shared policy (`bundle_tool_call::plan_tool_call`: zero-trust
//! validation, self/cycle/depth guard, the P1 matrix, the P2 `tool.call` gate,
//! target resolution) authorizes it, then the nested invoke of the target
//! service bundle's `tool-provider.call` runs on the active executor connection.
//!
//! Unlike svc_process (one capability set per invoke), this stage's handler is
//! connection-lifetime and takes the per-invoke [`InvokeScope`] as a
//! parameter, so the call chain / deadline / trace ride on the scope
//! (`tool_chain`, `deadline_at`, `trace`) and the nested invoke is registered
//! under a fresh TARGET scope: host-asserted tenant/community unchanged, the
//! target's app id + platform-installed version (its OWN grants), the grown
//! chain. The caller guest never names a community, tenant, or actor.
//!
//! Same-stage only: a target not loaded on THIS stage's executor fails loudly
//! as `target_unavailable` -- cross-stage calls need the ack-queue path and are
//! not built yet.

use std::sync::Arc;
use std::time::Instant;

use bundle_active_set::ActiveVersionSnapshot;
use bundle_tool_call::ToolRefusal;
use penguin_bundle_host::wire::{HostCallBody, HostResultError};

use super::{denied, InvokeScope, RelayQueue, StageCapabilities};
use crate::host_api::ConnectionRegistry;
use crate::stage_metrics;
use bundle_host_kv::KvBackend;

/// The process-wide `tool.invoke` wiring: the live active-version snapshot (the
/// sole source of a target's platform-installed digest/version + P1 tool facts)
/// and the connection registry the nested invoke runs on (resolved at call
/// time via `.active()`, never cached -- a reconnect replaces the connection).
#[derive(Clone)]
pub struct ToolWiring {
    pub connections: Arc<ConnectionRegistry>,
    pub snapshot: ActiveVersionSnapshot,
}

impl<Q: RelayQueue, K: KvBackend> StageCapabilities<Q, K> {
    /// Attaches the `tool.invoke` wiring (production: once, at startup).
    #[must_use]
    pub fn with_tool(mut self, tool: ToolWiring) -> Self {
        self.tool = Some(tool);
        self
    }

    /// `tool.invoke` (see the module doc). Returns the target's `{ok}` /
    /// `{tool_error}` envelope; every refusal is a typed host-call error
    /// carrying one of the snake_case codes the executor maps.
    pub(super) async fn handle_tool(
        &self,
        scope: &InvokeScope,
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
        let outcome = self.run_tool_invoke(scope, call, &mut resolved).await;
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
                app_id = %scope.app_id, community_id = scope.community_id,
                outcome = "ok", "tool.invoke served"
            ),
            Err(r) => tracing::info!(
                app_id = %scope.app_id, community_id = scope.community_id,
                outcome = r.code(), "tool.invoke refused"
            ),
        }
        outcome.map_err(|r| denied(r.code(), r.message()))
    }

    async fn run_tool_invoke(
        &self,
        scope: &InvokeScope,
        call: &HostCallBody,
        resolved: &mut Option<(String, String)>,
    ) -> Result<serde_json::Value, ToolRefusal> {
        let Some(wiring) = &self.tool else {
            // Production activations always call `with_tool`; reaching this
            // means a capabilities value was built without tool wiring.
            // Refuse (fail closed) and say so loudly.
            tracing::error!(
                "tool.invoke refused: capabilities were built without tool wiring (with_tool never called)"
            );
            return Err(ToolRefusal::TargetUnavailable(
                "tool.invoke is unavailable for this activation (no tool wiring configured)"
                    .to_string(),
            ));
        };
        // A scope built outside the dispatch path carries no chain: treat it
        // as a top-level call by this app.
        let chain = if scope.tool_chain.is_empty() {
            vec![scope.app_id.clone()]
        } else {
            scope.tool_chain.clone()
        };
        // 1-5: validate, guard, matrix, gate, resolve. No executor slot is
        // touched until this returns.
        let req = bundle_tool_call::parse_request(&call.args)?;
        let plan = bundle_tool_call::plan_tool_call(
            &scope.app_id,
            &chain,
            req,
            &wiring.snapshot,
            &self.gate,
            &scope.gate_scope(),
        )?;
        *resolved = Some((plan.target_app_id.clone(), plan.tool.clone()));
        tracing::debug!(
            app_id = %scope.app_id, target = %plan.target_app_id, tool = %plan.tool,
            community_id = scope.community_id, write = plan.write, depth = plan.next_chain.len() - 1,
            "tool.invoke authorized"
        );

        // 6. Nested invoke on the active executor connection.
        let deadline = bundle_tool_call::nested_deadline(scope.deadline_at)?;
        let conn = wiring.connections.active().ok_or_else(|| {
            ToolRefusal::TargetUnavailable("no executor connection is active".to_string())
        })?;
        let target_scope = InvokeScope {
            tenant: scope.tenant.clone(),
            community: scope.community.clone(),
            app_id: plan.target_app_id.clone(),
            origin_channel_id: scope.origin_channel_id.clone(),
            origin_platform: scope.origin_platform.clone(),
            tenant_id: scope.tenant_id,
            community_id: scope.community_id,
            app_version: plan.target_version,
            tool_chain: plan.next_chain.clone(),
            deadline_at: Some(Instant::now() + deadline),
            trace: scope.trace.clone(),
        };
        let result = crate::dispatch::invoke_tool_call(&conn, &plan, deadline, target_scope).await;
        if let Err(ToolRefusal::TargetUnavailable(_)) = &result {
            tracing::warn!(
                app_id = %scope.app_id, target = %plan.target_app_id, tool = %plan.tool,
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
