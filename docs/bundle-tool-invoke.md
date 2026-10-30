# Bundle-to-bundle tool invocation (`tool.invoke`)

A running bundle can call a tool exported by another bundle. The stage (svc-process / svc-action) is the sole
authority: it authorizes the call, runs the target under the target's OWN grants, and returns a typed result.
Design: `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-framework.md`; wire/WIT: `wit/waddle-bundle/tool.wit`
(`world stage-tools`).

## Exposing tools (service bundle)

1. Manifest: `kind: service`, with `exported_tools` (name, JSON input/output schema, `access` read|write, `risk`).
   Only a service may export; an app that lists exports is still refused as a target.
2. Build on `world stage-tools` and export `tool-provider`. With the Rust SDK, register handlers in a `ToolRegistry`
   (`waddle_sdk::tool::register_tool`) and have the generated `Guest::call` delegate to `waddle_sdk::tool::dispatch`
   (Python: `@tool("name")` + `dispatch`). A write tool reads the caller's idempotency key from `ToolContext`.
3. Reference: `core/bundle_executor/tests/fixtures/tool-provider-fixture-src`.

## Requesting and invoking (app bundle)

1. Manifest: `requested_tools: [{target, tool, access}]`. Each becomes a versioned `tool.call:<target>:<tool>`
   permission an admin approves (P2 grant path); a manifest change re-approves.
2. Call `waddle_sdk::tool::invoke(target, tool, &args, idempotency_key)` (args/results are canonical JSON). The Rust
   SDK needs a `ToolTransport` adapter over the bundle's generated `tool::invoke` import
   (`install_transport`) because `stage-tools` bindings live in the bundle crate.
3. Writes MUST pass an idempotency key. Errors are the typed `ToolError`: `NotGranted`, `Forbidden`, `Timeout`,
   `DepthExceeded`, `PoolExhausted`, `TargetUnavailable`, `InvalidArgs`.
4. Reference: `core/bundle_executor/tests/fixtures/tool-caller-fixture-src`.

## Security model

| Control | Enforced by |
|---|---|
| Zero-trust inputs: target id / tool name / args size (64 KiB) / key shape validated | `bundle_tool_call::parse_request` |
| App never names tenant/community/actor — host-asserted scope passes through unchanged | stage `InvokeScope` |
| Self-call and cycles refused BEFORE any executor slot is taken (no semaphore deadlock) | `plan_tool_call` guard |
| Depth cap 2 (`caller -> A -> B` ok, `B -> C` refused) | `MAX_CALL_DEPTH` |
| Caller's manifest must request the exact tool; P2 gate must grant `tool.call:<target>:<tool>`, pinned to the caller's version | `plan_tool_call` + `CapabilityGate` |
| app -> app forbidden even with a grant: only a `service` exporting the tool is a target | P1 matrix in `plan_tool_call` |
| Target resolved per `(tenant, community)` to its platform-installed digest + version; unknown/stale/tampered -> `target_unavailable`; same app id in another tenant never resolves | active-version snapshot, executor digest check |
| Target runs under its OWN grants (its app + installed version), never the caller's | `for_tool_target` |
| Deadline = `min(remaining outer budget, 8 s)`, enforced host-side as `Timeout` (not only the WASM trap) | `invoke_tool_call` |
| Idempotency key namespaced by (tenant, community, caller app) before it reaches the target | `scoped_idempotency_key` |
| PII-free: logs/metrics carry ids, tool name, outcome — never args; guest-supplied names are never metric labels (`unresolved`) | `handle_tool`, `record_tool_call` |

Same-stage only: the target must be loaded on the invoking stage's executor; a cross-stage target fails loudly as
`target_unavailable`.

## Telemetry

`waddles_tool_call_total` + `waddles_tool_call_latency_seconds` (labels `stage, target, tool, outcome`) on every
completion — see `docs/observability/data-plane-metrics.md`.

## Tests

`core/tool_invoke_e2e` drives REAL componentized bundles through the REAL executor and `handle_tool`
(`cargo test` there): happy path, app->app, grant, self-call/cycle, depth, digest/version pin, invalid args, slow
provider, cross-tenant, plus the telemetry assertion. Fixtures rebuild per `core/bundle_executor/tests/fixtures/README.md`.
