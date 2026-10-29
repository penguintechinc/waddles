# Bundle Tools / Internal-MCP Framework — Design

**Date:** 2026-10-29
**Status:** APPROVED (owner 2026-10-29, post builder-check) — corrections below applied; proceeding to implementation plan.
**Owner:** justin@penguintech.io
**Interim notes:** `~/.claude/projects/-home-penguin-code-waddles/memory/bundle-mcp-tools-framework.md`
**Consumed by:** the waddlebucks economy ([[giveaway-raffle-dungeon-economy]]), reputation, and all future service bundles. **Prerequisite** — spec'd before the economy service bundle.
**Builds on:** `docs/superpowers/specs/2026-10-13-bundle-manifest-v-next.md` (planned, unbuilt — `kind: app|service`, `requires`).

---

## 1. Intent & Why

Today every app bundle that touches shared state (economy, reputation) must independently hold the raw host grants and re-implement the access/adjustment logic — because **bundles cannot call each other** (confirmed: `bundles/python/chat/src/app.py:74`). That scatters money/rep-safety-critical logic across many bundles and multiplies the audit surface. This framework lets a **service bundle own that logic once and expose it as MCP-like "tools,"** which app bundles (and other service bundles) invoke. App bundles become thin: they compute *what* should change; the service does *how* it's applied, safely.

## 2. Goals / Non-Goals / Anti-Goals

**Goals**
- A first-class **`app` vs `service`** bundle type (extending `bundle-manifest-v-next`).
- **Service bundles declare exported tools** (MCP-like: name, typed in/out, read|write, risk); app and service bundles **request specific tools** (per-tool).
- **Host-mediated invocation**: caller `await`s a typed result; the host nested-invokes the target service bundle under *its own* grants and returns the result synchronously, inside an **~8s host-enforced timeout**.
- **Invocation matrix:** app→service and service→service allowed; **app→app forbidden**.
- **Zero-trust app bundles** as the security spine; the **service bundle + host gateway are the sole authorization boundary**.
- **Per-tool, version+SHA-2-pinned grants**, approved by the **global admin at install**, with tenant/community notices.

**Non-Goals**
- App→app invocation (ever).
- Synchronous cross-stage calls / long-running tool calls (bounded by the ≤10s WASM budget; 8s soft cap).
- Replacing host capabilities — tools are a *bundle-to-bundle* layer on top of the existing host-call mechanism.
- A public/third-party tool marketplace (internal bundles only in this iteration).

**Anti-Goals (failure even if it "works")**
- A service that trusts app-supplied amounts/targets/identity → privilege-escalation engine (a malicious giveaway bundle asking economy to mint it a million).
- Grants that auto-carry across versions, or a bundle that can mutate its own manifest/permissions at runtime.
- A nested invoke that exhausts or deadlocks the wasmtime Store pool, or lets caller+target blow the 10s budget and trap.
- A new wire `CapabilityKind` forced into external penguin-libs when an in-repo op-prefix suffices.

## 3. Constraints / Security Spine

- **Zero-trust app bundles** — design-harden assuming any app bundle is compromised/malicious. The app only *proposes* a delta; the service treats every input (amounts, target users, pot/context ids, tool args) as hostile and enforces its own authorization + bounds. All caps/quotas/actor-binding/supply-cap are host/service-enforced, never app-asserted.
- **Service/host is the trust boundary** — the target service bundle runs under *its own* grants and re-validates the caller's request; the app never holds the privileged grant it is effectively triggering (confused-deputy resolution).
- **Per-tool, version + SHA-2 pinned grants** — a grant binds to (app_id, version, SHA-2 `artifact_digest`); no auto-carry across versions; a prior version's artifact cannot change without invalidating approval (`critical-rules.md` Dependency Pinning).
- **Global-admin approval** for grants at install (`bundle_approvals.py` `require_scope("platform:admin")`); **re-approval on any update or permission change**; manifests immutable at runtime (no self-modification).
- **Host-enforced ~8s timeout** (2s buffer under the ≤10s WASM hard limit), handled host-side with a typed timeout error — never relying on the WASM trap.
- Reuse the existing closed capability catalog, grant gate (`bundle_capability_gate`), and 3-tier lifecycle (global install → tenant availability → community activate) rather than inventing parallel machinery.

## 4. Current State (recon 2026-10-29)

| Piece | Status | Where |
|---|---|---|
| WASM execution, synchronous nested host-calls mid-`transform`, ≤10s budget | exists | `wit/waddle-bundle/stage.wit` (worlds `stage`/`stage-next`/`stage-v2`); `bundle_executor/src/host/imports.rs` (`host-call`→blocking reply) |
| Capability gate + closed catalog + grant snapshot authorize | exists | `bundle_capability_gate/src/{permission.rs,gate.rs:79}` |
| Manifest v2 (no app/service `kind`, no bundle deps, no RPC) | exists | `hub_api/services/bundle_manifest_v2.py:154,424` |
| **`kind: app\|service` + app→service `requires` (dep/activation guard, NOT a call mechanism)** | **planned, unbuilt** | `docs/superpowers/specs/2026-10-13-bundle-manifest-v-next.md` §5,§7 |
| Host dispatch point for capability ops (op-prefix on `db` for reputation/economy/identity/ai) | exists | `svc_process/src/capabilities.rs:2015 dispatch()` |
| 3-tier approval lifecycle; grants approved by `platform:admin` | exists | `bundle_approval_service.py`; `blueprints/v1/bundle_approvals.py:135` |
| Await-a-result-from-elsewhere precedent (Discord ack handshake) | exists (fallback pattern) | `svc_action/src/capabilities.rs:1347` |
| Bundle→bundle tool RPC / exported-tool manifest / MCP-like schema | **none** | — |

**Hard constraints:** (1) `CapabilityKind` is a **closed enum in external penguin-libs** (mirrored `permission.rs:137`) → ride an op-prefix on an existing kind (e.g. `tool.invoke` on `db`) or wait for the planned `CapabilityKind::Transport`. (2) **Store-pool reentrancy** — a nested invoke must not exhaust/deadlock the wasmtime Store pool; caller+target share one budget. (3) **`InvokeScope.app_version` is a `0` placeholder** (`capabilities.rs:104`) — real version(+SHA-2) pinning must be wired. (4) **Actor-binding ambiguity** — economy/identity ops are actor-bound; whose actor flows into the target must be explicit.

## 5. The Design

### 5.1 Bundle types
Two kinds: **`app`** (per-community, install→avail→activate) and **`service`** (**always-on, platform-core**). `service` is ONE unified category covering **both platform connectors (Twitch, YouTube, Discord) AND logic services (economy, reputation)** — all always-on, all **tool-capable (export AND request tools, dual-role)**. This *extends* v-next (whose `service` was connector-only and forbade `requires`): relax the no-`requires` rule so services can request each other's tools — no third kind. A tool call does NOT depend on per-community activation of the target service; it resolves to the platform-installed service version. Enforce the matrix at manifest validation + grant time: **app→app forbidden; app→service and service→service allowed.**

**Payoff:** connectors-as-tool-servers makes every platform capability composable through this one mechanism — e.g. the Twitch service bundle exposes `get_chatters`/`get_followers`/`get_subscriptions`/`send_chat` tools, so lurker-economy ([[giveaway-raffle-dungeon-economy]]) gets its active-viewer/membership data by *requesting those tools* rather than building a bespoke Twitch poller. The economy service exposes `get_balance`/`adjust_balance`/`transfer`/`pay_pot`; app bundles compute deltas and call tools.

### 5.2 Tool declaration (service bundles)
A **service** bundle's manifest gains an **exported-tools** block: each tool = `{ name, input schema, output schema, access: read|write, risk: normal|dangerous }`. Tools are the only surface callers can invoke; the catalog/admin UI reads this for approval + notices.

### 5.3 Tool request (app AND service bundles)
A caller's manifest gains a **requested-tools** block: a per-tool list `{ target_app_id, tool, access }`. Service bundles may have BOTH blocks (dual role). Each request → a closed-catalog `PermissionId` ~ `tool.call:<target_app_id>:<tool>` + a `ResourceRef` encoding caller→target and read/rw, declared in the caller's `permissions:` block (`bundle_manifest_v2.py:424`).

### 5.4 Invocation
Caller guest calls a new host import (e.g. `tool.invoke(target, tool, args) -> result`) — a `tool.`-prefixed op on an existing wire kind (mirrors the reputation/economy op-prefix pattern at `capabilities.rs:2054`). Available on every stage a caller can run (`svc_process`, `svc_action`, and the gateway), so each `capabilities.rs` dispatcher implements it.

**Service bundles are STAGE-PLACED by function** — connector services (Twitch/YouTube/Discord) on the **gateway** (the stateful platform edge, per `edge-reshape-svc-gateway-dispatcher`), economy/reputation logic services on **svc_process**, dispatch services on **svc_action**. So invocation has two paths the host picks between after resolving the target's stage:
- **Same-stage** (caller+target co-located) → **synchronous host nested-invoke** (below).
- **Cross-stage** (e.g. a process app bundle calling a Twitch tool on the gateway) → **async queue + ack-handshake** (the existing Discord `DISCORD_OP_ACK` pattern, `svc_action/capabilities.rs:1347`): enqueue the call, await the target stage's posted result. The caller still `await`s a typed result; the async model the owner chose fits both paths.

Same-stage host handler in `dispatch()`:
1. `gate.authorize` the `tool.call:<target>:<tool>` grant (fail-closed).
2. Resolve the target's current approved (version, SHA-2) artifact.
3. **Nested-invoke** the target service bundle's component under a **fresh `InvokeScope`** (target's own app_id/grants), passing the caller's identity/context as *untrusted metadata*.
4. Enforce the **~8s host timeout** across the chain; on timeout return a typed `ToolTimeout` error to the caller (no WASM trap).
5. Return the target's typed result synchronously to the caller's `await`.

### 5.5 Approval & lifecycle
- **One combined consent screen** (like a Discord-integration / Android app-install permissions screen): the API returns all requested permissions + tools as a **JSON blurb**, rendered pretty in CLI/webui; the **global/platform admin reviews and approves ALL at once** (reuses the existing whole-version `permissionHash` approval — no per-tool approval rows). **Enforcement is per-tool at call time**: the gateway/service checks each tool endpoint against the approved set on every call.
- **Re-approval on every update / permission change** (no auto-carry — already how versioned approval works).
- **Tenant admin** sees an informational **notice** of the app's service-tool usage when adding it to tenant marketplace availability; **community admin** sees the **notice** again at activate — informed choice, not a re-approval. (These notice hooks are net-new in the tenant-availability/activation blueprints.)
- Grants are **pinned to the immutable `version_id`**; the artifact **digest is verified at load** (`verify_digest`) — that load-time verification IS the SHA-2 content-integrity guarantee (no separate stored SHA needed). Manifests immutable post-approval.

### 5.6 Security enforcement (zero-trust)
The target service bundle validates every caller-supplied value server-side and applies its own authorization/bounds (e.g. economy enforces supply-cap, caps, and that the pot/context belongs to the caller's community). The host gateway enforces the grant, the matrix, the timeout, and scope identity regardless of what the bundle asserts.

## 6. The Three Hard Problems (chosen approaches — corrected post builder-check)

1. **Two `CapabilityKind` enums, don't conflate them.** The *closed* one is the WIT/penguin-libs **wire** kind; the **gate's** `CapabilityKind` (`permission.rs:137`) is in-repo audit metadata and freely extensible. So `tool.invoke` rides a **`tool.` op-prefix on an existing wire kind** (exactly how `reputation.`/`economy.`/`identity.`/`ai.` ride `Db` at `capabilities.rs:2054`), plus a new in-repo gate `PermissionFamily`/`PermissionId`. **`CapabilityKind::Transport` is a connector-egress gate, NOT a tool-RPC kind** — not an alternative.
2. **Not a Store-pool problem — every call gets a fresh Store.** The real limit is the **per-bundle-digest slot semaphore (default 4) + global call limiter** (`invoke.rs:470-500`); the caller holds its slot while awaiting the target, so a cycle (A→B→A, A→A) or a hot caller saturating the target's 4 slots stalls → `ToolPoolExhausted`. Mitigation: **max nesting depth 2** (app→service→service), **cycle detection via the call-chain app_id list**, fail-fast on over-depth/exhaustion, and a **host-computed remaining-budget** passed as the target's `limits.timeout_ms` (each nested call gets its own epoch deadline — the 8s is enforced by the host across the chain, not a single shared WASM timer). Consider a per-service slot raise for hot services like economy.
3. **Actor-binding / confused-deputy → host-asserted scope.** The host passes the caller's **tenant/community `InvokeScope` UNCHANGED** (the app can NEVER name a community/tenant) plus the caller's **actor UUID as untrusted context**; the target runs under *its own* grants and decides whether/how to use the actor. Privileged ops (mint/payout) run as the *service's* authority, not the caller's actor — the app cannot borrow the service's privilege beyond the specific tool granted.
4. **Write idempotency.** Tool **writes require a caller-supplied idempotency key** and must be **retry-safe** — on an 8s host timeout the target may already have committed, so the caller can safely retry with the same key (reuses the economy ledger's existing idempotency mechanism).

## 7. Phased Delivery

```
P1  Manifest: kind app|service + exported-tools + requested-tools blocks (schema v3, extend v-next) + validation (matrix, app→app forbidden)
P2  Grant catalog: tool.call:<target>:<tool> PermissionId + ResourceRef + version/SHA-2 grant-pinning (fix InvokeScope.app_version)
P3  Host invocation: tool.invoke op, nested-invoke with fresh InvokeScope, 8s host timeout, depth limit
P4  Approval/notices: global-admin grant approval on install+update; tenant/community notices
P5  SDK: client (await tool.invoke) + service-side tool-export/dispatch helpers; one reference service bundle + one app caller as the e2e test
```

## 8. Open Questions — RESOLVED (owner 2026-10-29)

1. **Approval UX** → one combined consent screen (JSON → pretty), approve-all-at-once; per-tool enforcement at call time (§5.5).
2. **Service lifecycle** → service bundles are always-on platform-core, not per-community activated (§5.1).
3. **Stages** → both `svc_process` and `svc_action` (§5.4).
4. **Actor/scope binding** → host-asserted tenant/community, caller actor as untrusted context (§6.3).
5. **Write idempotency** → caller-supplied key, retry-safe (§6.4).
6. **Kind reconciliation with v-next** → distinct always-on tool-service kind, dual-role (§5.1) — builder reconciles the exact `kind` taxonomy.

**Minor (author defaults):** tool args = small JSON-Schema subset validated host-side; nesting depth 2 + cycle detection; static manifest tool references (no runtime discovery); fold into v-next as schema_version 3; error taxonomy `ToolTimeout`/`ToolDepthExceeded`/`ToolNotGranted`/`ToolPoolExhausted`; delimiter-safe `tool.call` permission-id encoding (app_ids contain dots).

## 9. What's Left to the Builder

- Exact manifest schema keys + validator rules; the `PermissionId`/`ResourceRef` additions (Rust + Python catalog mirror).
- The host nested-invoke implementation (Store management, depth, deadline) and the `tool.invoke` wire op.
- Version+SHA-2 grant-pinning wiring (replace the `app_version=0` placeholder).
- SDK client + service-export helpers; a reference service bundle + app caller as the real-path e2e.
- Tests: zero-trust (malicious app proposing bad amounts/targets rejected), matrix enforcement (app→app denied), timeout path, re-approval-on-update, version/SHA-2 pin mismatch rejection, nesting-depth limit.

---

*Interim capture during brainstorm. On approval → implementation plan (superpowers:writing-plans). The economy's treasury host op (1a) is independent and can proceed in parallel; the economy service bundle consumes this framework.*
