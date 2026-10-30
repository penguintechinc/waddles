//! Stage-agnostic policy for the bundle-to-bundle `tool.invoke` host call
//! (bundle-MCP-tools P3b, `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-
//! framework.md` SS5.4/SS6).
//!
//! The executor forwards a guest's `tool.invoke` to the stage as the host call
//! `Db:"tool.invoke"` with args `{target, tool, args: <json>, idempotency_key}`.
//! Everything the stage must decide BEFORE touching the executor lives here so
//! `svc_process` and `svc_action` share one authorization path:
//!
//! 1. [`parse_request`] -- zero-trust validation of every caller-supplied value.
//! 2. [`plan_tool_call`] -- self/cycle/depth guard (before any executor slot is
//!    requested), the caller's approved `requested_tools`, the P2 `tool.call`
//!    gate, target resolution against the live active-version snapshot (digest +
//!    version pin), the P1 invocation matrix (app->app forbidden: only a trusted
//!    `service` exporting the tool is a target) and the write-needs-idempotency
//!    rule.
//! 3. [`nested_payload`] / [`nested_deadline`] / [`interpret_reply`] -- the
//!    nested `InvokeBody` contract and the typed mapping of the target's reply.
//!
//! The wire contract is pinned (plan Global Constraints): args/results are
//! JSON; the target's reply is `{"ok": <json>}` or `{"tool_error": <info>}`.
//! Refusal codes are the snake_case set the executor's `tool.invoke` import
//! maps (`bundle_executor::host::stage_tools`).
//!
//! PII-free by construction: nothing here logs or formats `args`.

use std::time::{Duration, Instant};

use bundle_active_set::ActiveVersionSnapshot;
use bundle_capability_gate::resource::{ToolAccess, ToolCallTarget};
use bundle_capability_gate::{CapabilityGate, InvokeScope, PermissionId, ResourceRef};
use penguin_bundle_host::wire::{ErrorCode, Message};

/// Maximum nested tool-call depth below the originating (top-level) invocation:
/// `A -> B -> C` is depth 2; `C -> D` is refused.
pub const MAX_CALL_DEPTH: usize = 2;

/// Upper bound on the canonical-JSON size of a tool call's `args`.
pub const MAX_ARGS_BYTES: usize = 64 * 1024;

/// Host-enforced ceiling on one nested tool call (spec SS5.4 "8s host cap").
pub const TOOL_CALL_CAP: Duration = Duration::from_secs(8);

/// Longest accepted idempotency key.
pub const MAX_IDEMPOTENCY_KEY_LEN: usize = 128;

/// Reserved top-level args field the host uses to hand the target tool the
/// VERIFIED actor + scopes (`{"actor": <uuid|null>, "scopes": [..]}`). The host
/// strips any caller-supplied value and overwrites it, so a guest can never
/// choose it; the SDK facades strip it and expose it read-only.
pub const CTX_ARG: &str = "__ctx";

/// The host-VERIFIED actor of a tool call: the community `user_uuid` the
/// stage resolved from the MAC-verified envelope's platform account, and the
/// scope set derived from that member's community role via [`scopes_for_role`].
/// `Default` (no actor, no scopes) is the fail-closed value for anything not
/// user-originated or not resolvable. NEVER built from guest/caller args.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ActorContext {
    /// Canonical community `user_uuid`, or `None` when not user-originated /
    /// unresolved.
    pub actor: Option<String>,
    /// Sorted, deduplicated host-derived scopes.
    pub scopes: Vec<String>,
}

impl ActorContext {
    /// The fail-closed "nobody" context.
    pub fn none() -> Self {
        Self::default()
    }

    /// A resolved actor with the scopes of `role` (unknown role -> no scopes).
    pub fn for_role(actor: impl Into<String>, role: &str) -> Self {
        Self {
            actor: Some(actor.into()),
            scopes: scopes_for_role(role)
                .iter()
                .map(|s| (*s).to_string())
                .collect(),
        }
    }

    /// `true` only when the verified actor holds `scope` exactly.
    pub fn has_scope(&self, scope: &str) -> bool {
        self.actor.is_some() && self.scopes.iter().any(|s| s == scope)
    }
}

/// The FIXED host-side community role -> scope mapping (cumulative, not
/// guest/config controlled). `community-owner` is a superset of
/// `community-admin`, which is a superset of `moderator`; `vip` and `member`
/// are siblings below it. Any unknown / empty role maps to NO scopes
/// (fail closed). Mirrors `TOOL_REQUIRED_SCOPES` in
/// `hub_api/services/bundle_manifest_v2.py`.
///
/// | role | scopes |
/// |---|---|
/// | `community-owner` | member, vip, moderate, admin, owner |
/// | `community-admin` | member, vip, moderate, admin |
/// | `moderator` | member, moderate |
/// | `vip` | member, vip |
/// | `member` | member |
pub fn scopes_for_role(role: &str) -> &'static [&'static str] {
    match role {
        "community-owner" => &[
            "community:member",
            "community:vip",
            "community:moderate",
            "community:admin",
            "community:owner",
        ],
        "community-admin" => &[
            "community:member",
            "community:vip",
            "community:moderate",
            "community:admin",
        ],
        "moderator" => &["community:member", "community:moderate"],
        "vip" => &["community:member", "community:vip"],
        "member" => &["community:member"],
        _ => &[],
    }
}

/// The `required_scope` the target tool declares, if any -- lets a stage pay
/// for actor resolution only for scope-restricted tools. Advisory only:
/// [`plan_tool_call`] re-checks authoritatively, so a `None` here can never
/// bypass the gate (an unresolved actor simply fails any required scope).
pub fn required_scope_of(
    snapshot: &ActiveVersionSnapshot,
    gate_scope: &InvokeScope,
    target: &str,
    tool: &str,
) -> Option<String> {
    snapshot
        .tool_entry_in_scope(gate_scope.tenant_id(), gate_scope.community_id(), target)
        .and_then(|e| {
            e.tools
                .exported_tool(tool)
                .and_then(|t| t.required_scope.clone())
        })
}

/// A typed refusal -- the stage returns it to the executor as a host-call error
/// whose `code` is [`ToolRefusal::code`] (the executor maps it to the guest's
/// `tool-error`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ToolRefusal {
    /// Malformed / oversized / non-JSON input, or a write without a key.
    InvalidArgs(String),
    /// Self-call, cycle, app->app, or a tool the caller never requested /
    /// the target never exported.
    Forbidden(String),
    /// Nested depth cap exceeded.
    DepthExceeded,
    /// The P2 gate denied the `tool.call` grant.
    NotGranted(String),
    /// Unknown / stale / unresolvable target (or cross-stage, unsupported).
    TargetUnavailable(String),
    /// The host-enforced nested deadline elapsed.
    Timeout,
    /// No executor slot was free for the nested call.
    PoolExhausted,
}

impl ToolRefusal {
    /// The snake_case wire code (see the module doc).
    pub fn code(&self) -> &'static str {
        match self {
            Self::InvalidArgs(_) => "invalid_args",
            Self::Forbidden(_) => "forbidden",
            Self::DepthExceeded => "depth_exceeded",
            Self::NotGranted(_) => "not_granted",
            Self::TargetUnavailable(_) => "target_unavailable",
            Self::Timeout => "timeout",
            Self::PoolExhausted => "pool_exhausted",
        }
    }

    /// A short, fixed, PII-free message for the wire.
    pub fn message(&self) -> String {
        match self {
            Self::InvalidArgs(m)
            | Self::Forbidden(m)
            | Self::NotGranted(m)
            | Self::TargetUnavailable(m) => m.clone(),
            Self::DepthExceeded => format!("tool call depth exceeds {MAX_CALL_DEPTH}"),
            Self::Timeout => "tool call exceeded its deadline".to_string(),
            Self::PoolExhausted => "no executor slot free for the nested tool call".to_string(),
        }
    }
}

/// A validated `tool.invoke` request -- every field passed [`parse_request`].
#[derive(Clone, Debug, PartialEq)]
pub struct ToolRequest {
    pub target: String,
    pub tool: String,
    pub args: serde_json::Value,
    pub idempotency_key: Option<String>,
}

/// `waddles.<seg>.<seg>.<seg>`, each segment `[a-z][a-z0-9-]{0,..}`-ish --
/// mirrors hub-api's `_APP_ID_RE` shape closely enough to reject anything that
/// is not a plausible app id (the authoritative check is the snapshot lookup).
fn valid_app_id(s: &str) -> bool {
    if s.len() > 128 {
        return false;
    }
    let segs: Vec<&str> = s.split('.').collect();
    segs.len() == 4
        && segs[0] == "waddles"
        && segs[1..].iter().all(|seg| {
            !seg.is_empty()
                && seg
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
        })
}

/// `^[a-z][a-z0-9_.]{0,63}$` (hub-api `_TOOL_NAME_RE`).
fn valid_tool_name(s: &str) -> bool {
    let mut bytes = s.bytes();
    match bytes.next() {
        Some(b) if b.is_ascii_lowercase() => {}
        _ => return false,
    }
    s.len() <= 64
        && bytes.all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'.')
}

fn valid_idempotency_key(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= MAX_IDEMPOTENCY_KEY_LEN
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.' | b':'))
}

/// Parses and zero-trust-validates the `Db:"tool.invoke"` host-call args.
/// Every caller-supplied value is checked; any failure is `invalid_args`.
/// Whether a write tool carries the idempotency key is checked later (the
/// tool's access is only known from the target's manifest).
pub fn parse_request(raw: &serde_json::Value) -> Result<ToolRequest, ToolRefusal> {
    let bad = |m: &str| ToolRefusal::InvalidArgs(m.to_string());
    let obj = raw
        .as_object()
        .ok_or_else(|| bad("args must be an object"))?;
    let target = obj
        .get("target")
        .and_then(|v| v.as_str())
        .filter(|s| valid_app_id(s))
        .ok_or_else(|| bad("target must be a valid app id"))?;
    let tool = obj
        .get("tool")
        .and_then(|v| v.as_str())
        .filter(|s| valid_tool_name(s))
        .ok_or_else(|| bad("tool must match [a-z][a-z0-9_.]{0,63}"))?;
    let args = obj.get("args").ok_or_else(|| bad("args is required"))?;
    // Canonical-JSON size, re-encoded: the bound is on what actually gets
    // forwarded, not on whatever the caller's framing was.
    let encoded = serde_json::to_vec(args).map_err(|_| bad("args are not encodable JSON"))?;
    if encoded.len() > MAX_ARGS_BYTES {
        return Err(bad("args exceed the maximum size"));
    }
    let idempotency_key = match obj.get("idempotency_key") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::String(k)) if valid_idempotency_key(k) => Some(k.clone()),
        Some(_) => return Err(bad("idempotency_key is malformed")),
    };
    Ok(ToolRequest {
        target: target.to_string(),
        tool: tool.to_string(),
        args: args.clone(),
        idempotency_key,
    })
}

/// An authorized, target-resolved tool call, ready for the nested invoke.
#[derive(Clone, Debug, PartialEq)]
pub struct ToolPlan {
    pub target_app_id: String,
    pub tool: String,
    pub args: serde_json::Value,
    pub idempotency_key: Option<String>,
    /// The target's platform-installed digest (canonical `sha256:<hex>`).
    pub target_digest: String,
    /// The target's platform-installed `app_versions.id`.
    pub target_version: i64,
    /// `true` for a `write` tool.
    pub write: bool,
    /// The chain the nested invoke carries: the caller's chain plus the target.
    pub next_chain: Vec<String>,
    /// Host-derived caller bundle (never from args): namespaces the forwarded
    /// idempotency key so two principals' identical raw keys cannot collide.
    pub caller_app_id: String,
    /// The host-verified actor/scopes forwarded to the target as [`CTX_ARG`].
    pub actor: ActorContext,
    /// Gate-scope tenant (host-derived) -- idempotency namespace component.
    pub tenant_id: i32,
    /// Gate-scope community (host-derived) -- idempotency namespace component.
    pub community_id: i32,
}

/// Authorizes one validated request and resolves its target. Pure policy -- no
/// executor slot is requested here, which is why the self/cycle/depth guard
/// runs first: a self/cycle call would otherwise wait on the very per-bundle
/// slot semaphore the caller already holds.
///
/// `chain` is the call chain INCLUDING `caller_app_id` as its last element
/// (a top-level invocation's chain is `[caller]`). Order:
///
/// 1. self-call / cycle (`forbidden`) and depth (`depth_exceeded`);
/// 2. the caller's own approved `requested_tools` must list `(target, tool)`
///    (`forbidden`);
/// 3. the P2 gate (`not_granted`) -- the grant is bound to this exact
///    `(target, tool)` by the gate's `ToolScoped` arm;
/// 4. the target must be active in this scope (`target_unavailable`), a trusted
///    `service` exporting the tool (`forbidden` -- this is the app->app ban,
///    enforced here independently of any grant) whose access the caller's
///    request covers (`forbidden`);
/// 5. a write tool requires the caller's idempotency key (`invalid_args`);
/// 6. a tool declaring `required_scope` requires the HOST-VERIFIED `actor` to
///    hold it (`forbidden`; no actor / unresolved / under-scoped all refuse).
pub fn plan_tool_call(
    caller_app_id: &str,
    chain: &[String],
    req: ToolRequest,
    snapshot: &ActiveVersionSnapshot,
    gate: &CapabilityGate,
    gate_scope: &InvokeScope,
    actor: &ActorContext,
) -> Result<ToolPlan, ToolRefusal> {
    // 1. Guards, before anything else.
    if req.target == caller_app_id {
        return Err(ToolRefusal::Forbidden(
            "a bundle may not invoke its own tool (self-call)".to_string(),
        ));
    }
    if chain.contains(&req.target) {
        return Err(ToolRefusal::Forbidden(
            "tool call cycle: the target is already in the call chain".to_string(),
        ));
    }
    // `chain.len()` includes the caller, so hops already taken = len - 1 and
    // this call would be hop len.
    if chain.len() > MAX_CALL_DEPTH {
        return Err(ToolRefusal::DepthExceeded);
    }

    // 2. The caller's own approved manifest must request this exact tool.
    // Scoped to the GATE's own `(tenant, community)` -- never a flattened,
    // cross-tenant lookup. No row for this scope => no caller facts => forbidden.
    let (tenant_id, community_id) = (gate_scope.tenant_id(), gate_scope.community_id());
    let caller_entry = snapshot.tool_entry_in_scope(tenant_id, community_id, caller_app_id);
    let requested_write = caller_entry
        .as_ref()
        .and_then(|e| {
            e.tools
                .requested
                .iter()
                .find(|r| r.target == req.target && r.tool == req.tool)
        })
        .map(|r| r.write)
        .ok_or_else(|| {
            ToolRefusal::Forbidden("the caller's manifest does not request this tool".to_string())
        })?;

    // 3. The P2 gate. Denial detail is the gate's own fixed reason string.
    let access = if requested_write {
        ToolAccess::Write
    } else {
        ToolAccess::Read
    };
    gate.authorize(
        gate_scope,
        PermissionId::ToolCall {
            target: req.target.clone(),
            tool: req.tool.clone(),
        },
        ResourceRef::ToolScoped(ToolCallTarget {
            target_app_id: req.target.clone(),
            tool: req.tool.clone(),
            access,
        }),
    )
    .map_err(|d| ToolRefusal::NotGranted(d.to_string()))?;

    // 4. Resolve + matrix.
    let target_entry = snapshot
        .tool_entry_in_scope(tenant_id, community_id, &req.target)
        .ok_or_else(|| {
            ToolRefusal::TargetUnavailable("the target is not active in this scope".to_string())
        })?;
    let exported = target_entry.tools.exported_tool(&req.tool).ok_or_else(|| {
        ToolRefusal::Forbidden(
            "the target is not a service exporting this tool (app-to-app calls are forbidden)"
                .to_string(),
        )
    })?;
    if exported.write && !requested_write {
        return Err(ToolRefusal::Forbidden(
            "the caller requested read access to a write tool".to_string(),
        ));
    }

    // 5. Writes are retry-safe only with a caller-supplied key.
    if exported.write && req.idempotency_key.is_none() {
        return Err(ToolRefusal::InvalidArgs(
            "a write tool requires an idempotency key".to_string(),
        ));
    }

    // 6. Host-side scope enforcement against the VERIFIED actor -- before any
    // dispatch, with no reliance on guest code. Fail closed.
    if let Some(required) = &exported.required_scope {
        if !actor.has_scope(required) {
            return Err(ToolRefusal::Forbidden(
                "the verified actor lacks the scope this tool requires".to_string(),
            ));
        }
    }

    let mut next_chain = chain.to_vec();
    next_chain.push(req.target.clone());
    Ok(ToolPlan {
        target_app_id: req.target,
        tool: req.tool,
        args: req.args,
        idempotency_key: req.idempotency_key,
        target_digest: target_entry.digest,
        target_version: target_entry.version_id,
        write: exported.write,
        next_chain,
        caller_app_id: caller_app_id.to_string(),
        actor: actor.clone(),
        tenant_id,
        community_id,
    })
}

/// Reserved top-level args field carrying the caller's idempotency key to the
/// target guest (`tool-provider.call` has no key param). Callers may not set it.
pub const IDEMPOTENCY_ARG: &str = "__idempotency_key";

/// Principal-scoped, deterministic idempotency token:
/// `hex(sha256(tenant 0x1f community 0x1f caller_app_id 0x1f raw_key))`.
/// Same principal + raw key => same token (retry dedupes); any differing
/// principal component => a different token. Fixed 64-char hex output.
pub fn scoped_idempotency_key(
    tenant_id: i32,
    community_id: i32,
    caller_app_id: &str,
    raw_key: &str,
) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(tenant_id.to_string().as_bytes());
    h.update([0x1f]);
    h.update(community_id.to_string().as_bytes());
    h.update([0x1f]);
    h.update(caller_app_id.as_bytes());
    h.update([0x1f]);
    h.update(raw_key.as_bytes());
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

/// The `InvokeBody.payload` for the nested `ExportKind::ToolCall` invoke.
///
/// When an idempotency key is present it is merged into the forwarded args
/// under [`IDEMPOTENCY_ARG`], and the merged args are re-encoded and re-checked
/// against [`MAX_ARGS_BYTES`] so the bound holds for what the target receives.
/// A caller-supplied reserved field (spoofing) or a key with non-object args is
/// `invalid_args`.
pub fn nested_payload(plan: &ToolPlan) -> Result<serde_json::Value, ToolRefusal> {
    let bad = |m: &str| ToolRefusal::InvalidArgs(m.to_string());
    if plan
        .args
        .as_object()
        .is_some_and(|o| o.contains_key(IDEMPOTENCY_ARG))
    {
        return Err(bad("args may not set the reserved idempotency field"));
    }
    let mut args = plan.args.clone();
    // Host-asserted actor context. A caller-supplied `__ctx` is IGNORED:
    // always stripped, then (only when the host verified an actor) replaced
    // with the verified value. No actor => the field is simply absent, which
    // the SDKs read as "no actor, no scopes" (so non-user-originated calls to
    // pre-`__ctx` targets are byte-identical to before).
    if let Some(obj) = args.as_object_mut() {
        obj.remove(CTX_ARG);
        if plan.actor.actor.is_some() {
            obj.insert(
                CTX_ARG.to_string(),
                serde_json::json!({"actor": plan.actor.actor, "scopes": plan.actor.scopes}),
            );
        }
    } else if plan.actor.actor.is_some() {
        return Err(bad(
            "args must be an object when the call has a verified actor",
        ));
    }
    let forwarded_key = plan.idempotency_key.as_deref().map(|raw| {
        scoped_idempotency_key(plan.tenant_id, plan.community_id, &plan.caller_app_id, raw)
    });
    if let Some(key) = &forwarded_key {
        let obj = args
            .as_object_mut()
            .ok_or_else(|| bad("args must be an object when an idempotency key is supplied"))?;
        obj.insert(
            IDEMPOTENCY_ARG.to_string(),
            serde_json::Value::String(key.clone()),
        );
    }
    if args.is_object() {
        let encoded = serde_json::to_vec(&args).map_err(|_| bad("args are not encodable JSON"))?;
        if encoded.len() > MAX_ARGS_BYTES {
            return Err(bad("args exceed the maximum size"));
        }
    }
    Ok(serde_json::json!({
        "tool": plan.tool,
        "args": args,
        "call_chain": plan.next_chain,
        "idempotency_key": forwarded_key,
    }))
}

/// The nested call's host deadline: `min(remaining outer deadline, 8s)`. A
/// spent outer budget is `timeout` (never a zero-length nested call).
pub fn nested_deadline(outer_deadline: Option<Instant>) -> Result<Duration, ToolRefusal> {
    let remaining = match outer_deadline {
        Some(at) => at.saturating_duration_since(Instant::now()),
        None => TOOL_CALL_CAP,
    };
    let d = remaining.min(TOOL_CALL_CAP);
    if d.is_zero() {
        return Err(ToolRefusal::Timeout);
    }
    Ok(d)
}

/// Maps the target's executor reply onto the `{ok}`/`{tool_error}` envelope
/// the caller's executor decodes, or a typed refusal. A malformed envelope is
/// `target_unavailable` (fail closed; never a fabricated success).
pub fn interpret_reply(reply: Message) -> Result<serde_json::Value, ToolRefusal> {
    match reply {
        Message::Result(body) => {
            let valid = body.payload.as_object().is_some_and(|o| {
                o.len() == 1 && (o.contains_key("ok") || o.contains_key("tool_error"))
            });
            if valid {
                Ok(body.payload)
            } else {
                Err(ToolRefusal::TargetUnavailable(
                    "the target returned a malformed tool envelope".to_string(),
                ))
            }
        }
        Message::Error(e) => Err(match (e.code, e.detail.as_deref()) {
            (ErrorCode::ExecutorDeadline, Some("pool_exhausted")) => ToolRefusal::PoolExhausted,
            (ErrorCode::ExecutorDeadline, _) => ToolRefusal::Timeout,
            (ErrorCode::UnknownBundle | ErrorCode::DigestMismatch, _) => {
                ToolRefusal::TargetUnavailable("the target bundle is not loaded".to_string())
            }
            _ => ToolRefusal::TargetUnavailable("the target tool call failed".to_string()),
        }),
        _ => Err(ToolRefusal::TargetUnavailable(
            "unexpected reply frame from the executor".to_string(),
        )),
    }
}

#[cfg(test)]
mod tests;
