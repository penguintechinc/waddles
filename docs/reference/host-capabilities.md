# Bundle Host Capabilities

Per-capability reference: WIT surface, permission id, risk tier, limits, fail-closed behavior.
Enforcement model and call path: `docs/architecture/bundle-host-capability-model.md`.
Egress allowlist and `net.http.*` manifest syntax: `docs/reference/bundle-network-permissions.md`.

## Sources

| Item | Path |
|---|---|
| WIT contract (source of truth) | `wit/waddle-bundle/stage.wit` |
| Permission catalog, tiers, quotas | `core/bundle_capability_gate/src/permission.rs` |
| Gate (`authorize`) | `core/bundle_capability_gate/src/gate.rs` |
| Denial vocabulary | `core/bundle_capability_gate/src/denied.rs` |
| Stage wiring | `core/svc_process/src/capabilities.rs`, `core/svc_action/src/capabilities.rs` |

Verified against `release/v3.0.X` (`1e6b0056`) plus in-flight PRs #741, #749, #751.

## Status Legend

| Label | Meaning |
|---|---|
| MERGED | On `release/v3.0.X` |
| IN-FLIGHT #N | Open PR, not on release; contract may change in review |
| CATALOG-ONLY | Permission id defined in `permission.rs`; no WIT import and no host handler |

## Capability Summary

| Capability | Permission id(s) | Tier | WIT import | Status |
|---|---|---|---|---|
| Context, clock, log | `platform.context`, `platform.clock`, `platform.log` | Normal | `context`, `clock`, `log` | MERGED |
| Bundle KV | `storage.kv` | Normal | `kv` | MERGED |
| Bundle table | `storage.tables` | Normal | `db` | MERGED |
| Outbound HTTP | `net.http.fqdn:<host>`, `net.http.public-ip:<ip>`, `net.http.private-ip:<ip\|cidr>` | Normal / Dangerous / Dangerous | `http` | MERGED; `?` query-param secret ref IN-FLIGHT #749 |
| Chat relay | `chat.send:<platform>` | Normal | `relay` (action-stage only) | MERGED (`twitch`, `discord`) |
| Flags | `flags.read` | Normal | `%flags` | MERGED (`enabled`); `tier` not wired |
| Reputation | `reputation.read`, `reputation.community.write`, `reputation.tenant.write` | Normal / Dangerous / Dangerous | `reputation` (stage-next) | IN-FLIGHT #741; `reputation.tenant.write` CATALOG-ONLY |
| Economy | `economy.read`, `economy.wager`, `economy.transfer` | Normal / Dangerous / Dangerous | `economy` (stage-next) | IN-FLIGHT #751 (stacked on #741) |
| Overlay widgets | `overlay.media` | Dangerous | `overlay` (stage-next) | WIT MERGED; no host handler |
| Streaming hooks | `streaming.lifecycle.subscribe` | Normal | `streaming-lifecycle` export (stage-next) | WIT MERGED; host registration deferred |
| Moderation | `moderation.<platform>` | Dangerous | none | CATALOG-ONLY |
| AI generation | `ai.generate` | Dangerous | none | CATALOG-ONLY |
| Object storage | `storage.objects` | Normal | none | CATALOG-ONLY |
| User profile read | `users.profile.read` | Normal | none | CATALOG-ONLY |
| Telemetry | `telemetry.logs`, `telemetry.metrics` | Normal | none | CATALOG-ONLY |
| Raw PII in interaction inputs | `interaction.pii.receive` | Dangerous | none | CATALOG-ONLY (default NO) |
| Scheduled triggers | `platform.scheduled` | Normal | none | CATALOG-ONLY |

## Platform (Always Granted)

| Item | Value |
|---|---|
| Permission | `platform.context`, `platform.clock`, `platform.log` — Normal, unlimited |
| Grant | Unioned into every grant set by `AlwaysGrantedLoader` (`core/svc_process/src/grant_gate.rs`); no grant row needed |
| WIT | `get-context: func() -> bundle-context`; `now-millis: func() -> u64`; `now-rfc3339: func() -> string`; `monotonic-nanos: func() -> u64`; `write: func(lvl: level, message: string, fields-json: string)` |
| Log limits | Message ≤4096 chars; control characters stripped; PII-sanitized before emit (`svc_process/src/capabilities.rs`, `sanitize_bundle_log_message`) |
| Fail-closed | Not applicable — grant is unconditional |

## KV (`storage.kv`)

| Item | Value |
|---|---|
| Permission | `storage.kv` — Normal — resource `KvState` (app-scoped) |
| WIT | `get(key) -> result<option<list<u8>>, error>`; `set(key, value, ttl-seconds: u32) -> result<_, error>`; `delete(key) -> result<_, error>`; `increment(key, delta: s64, ttl-seconds: u32) -> result<s64, error>` |
| Error | `too-large(u64)`, `backend(string)` |
| Limits | 64 KiB/value (`MAX_VALUE_BYTES`); TTL ≤30 d (`MAX_TTL_SECONDS`); 10k keys/app (`MAX_KEYS_PER_APP`); 64 ops/invoke (`MAX_OPS_PER_INVOKE`) — `core/bundle_host_kv/src/limits.rs` |
| Isolation | Valkey key prefix built only from `(tenant, community, app_id)`; guest key rejects `:` and glob metacharacters (`validate_guest_key`, `core/bundle_host_kv/src/scope.rs`) |
| Fail-closed | No grant row → `not_granted` |

Note: `stage.wit` marks `kv` "always granted". Gate wins: `storage.kv` requires an explicit grant (`permission.rs`, `StorageKv` notes). WIT comment is stale.

## Table (`storage.tables`)

| Item | Value |
|---|---|
| Permission | `storage.tables` — Normal — resource `Table` (app-scoped) |
| Grant precondition | Manifest `data.tables` non-empty (`stage.wit` `interface db` doc) |
| WIT | `insert(column-values: list<column-value>) -> result<row, error>`; `get(row-id: string) -> result<row, error>`; `query(limit: u32, offset: u32, order-by: option<order-by>) -> result<list<row>, error>`; `update(row-id, expected-version: u64, column-values) -> result<row, error>`; `delete(row-id, expected-version: u64) -> result<_, error>` |
| Error | `denied`, `invalid-column`, `invalid-value`, `not-found`, `conflict`, `quota-exceeded`, `timeout`, `backend` |
| Column types | `uuid`, `int4`, `int8`, `bool`, `text` (≤8192 B), `timestamptz`, `jsonb` (≤16 KiB) — `core/bundle_host_db/src/schema.rs` |
| `user_ref` columns | Must be a UUID string; drive erasure-cascade eligibility (`schema.rs`) |
| Platform columns | `row-id`, `version`, `created-at`, `updated-at` — never guest-writable |
| Scope | `(tenant, community, app_id)` derived server-side; guest supplies no schema, table, or column name, and no tenant or community |
| DB role | `waddles_bundle_runtime` (DML only); DDL owned by hub-api migrator |
| Limits | `query` limit clamped to 200 (`MAX_QUERY_LIMIT`); 100k rows/app (`MAX_ROWS_PER_APP`); 2 s statement timeout; 5 s call deadline — `core/bundle_host_db/src/limits.rs` |
| Feature flag | `waddles.bundle-db-capability` (`license.rs`, `BUNDLE_DB_CAPABILITY_FLAG`); OFF → `feature_disabled` |
| Fail-closed | Gate first (`not_granted`); unknown `app_id` in schema cache → denied, never "any table" (`schema.rs`) |

Note: the `bundle_host_db/src/lib.rs` header describes an older insert/get/update/delete slice with raw `execute` in `stage.wit`. Both are stale. Current WIT and `svc_process` handlers are authoritative.

## Outbound HTTP (`net.http`)

| Item | Value |
|---|---|
| Permission | `net.http.fqdn:<host>` — Normal (preferred); `net.http.public-ip:<ip>` — Dangerous; `net.http.private-ip:<ip\|cidr>` — Dangerous |
| Grant precondition | Manifest `egress` non-empty (`stage.wit` `interface http` doc) |
| WIT | `send: func(req: request) -> result<response, error>`; `request` = `method`, `url`, `headers`, `body: option<list<u8>>`, `secret-refs: list<tuple<string, string>>` |
| Error | `denied(string)`, `timeout`, `too-large(u64)`, `rate-limited(u32)`, `transport(string)` |
| Rate / size | 10 rps, 1 MB response (catalog default); `egress_rps` per-app override (`core/bundle_host_http/src/egress.rs`) |
| Private IP | Dangerous; deny-by-default at instance policy (`instance_policy.rs`); grants may not be coarser than /16 (v4) or /64 (v6) (`permission.rs`) |
| Always denied | Loopback, link-local, cloud metadata (incl. IMDSv2 IPv6) regardless of family or policy (`permission.rs`) |
| Pipeline | Gate → `EgressGuard`: declared-host match, SSRF checks, DNS pinning, redirect re-check, token bucket, response cap |
| Feature flag | Action-stage: `waddles.core.bundle-egress` via `flag_or_closed` (`core/svc_action/src/lib.rs`) |

### Secret references

| Mode | Slot name | Status | Behavior |
|---|---|---|---|
| Header | `Authorization` (plain name) | MERGED | Resolved value sent as that request header |
| Query param | `?key` | IN-FLIGHT #749 | Appends `key=VALUE`; first hop only, to granted FQDN; dropped on redirect; scrubbed from errors and responses; unresolvable → denied, never sent unauthenticated |

Resolution (MERGED, `egress.rs` ~L441–460): guest names a symbolic ref → must exist in the activation's `granted_secret_refs` map → only the mapped env-var name is read via `CredentialBroker`. Unknown ref → `secret_not_granted`. Secret value never enters the component.

## Chat Relay (`chat.send:<platform>`)

| Item | Value |
|---|---|
| Permission | `chat.send:<platform>` — Normal (catalog `ChatSend`) |
| Scope | Action-stage only; process-stage never granted, denied unconditionally (`stage.wit` `interface relay` doc) |
| WIT | `push: func(provider: string, message-json: string) -> result<_, error>`; error `denied(string)`, `backend(string)` |
| Provider allowlist | `twitch`, `discord` (`RELAY_PROVIDERS`, `core/svc_action/src/capabilities.rs`); unknown → `unknown_provider` before gate |
| Parser allowlist | `twitch`, `kick`, `youtube`, `discord`, `slack` (`COMPILED_IN_PLATFORMS`) — broader than relay allowlist |
| Outbound text | `{user:<uuid>}` placeholders → display names via `ResolveDisplayNames`, flag-gated; raw placeholder if off (`capabilities.rs`) |
| Rate | Existing relay limits (`UsageBatcher`) |

No `dm.*` permission family exists in source.

## Flags (`flags.read`)

| Item | Value |
|---|---|
| Permission | `flags.read` — Normal — unlimited |
| WIT | `%flags.enabled: func(key: string, default-value: bool) -> bool`; `%flags.tier: func() -> string` |
| Wired | `enabled` only; `tier` → `not_implemented` (`handle_flags`, both stages) |
| Resolution | PostHog flag + license entitlement; cached; outage → caller's `default-value` (`stage.wit` `%flags` doc) |

## Reputation (`reputation`) — IN-FLIGHT #741

| Item | Value |
|---|---|
| Permissions | `reputation.read` (`get`) — Normal, unlimited; `reputation.community.write` (`adjust`) — Dangerous |
| WIT (stage-next) | `get(user: string) -> result<s64, error>`; `adjust(user: string, delta: s32, reason: string) -> result<s64, error>` |
| Error | `denied(string)`, `not-a-member`, `daily-cap-exceeded`, `invalid(string)`, `unavailable(string)`, `backend(string)` |
| Caps (catalog) | Per call \|delta\| ≤5; per user rolling-24h ≤5; per community rolling-24h ≤50 |
| `reputation.tenant.write` | Per scope rolling-24h ≤200; CATALOG-ONLY in #741 (no WIT op maps to it) |
| Target | `user` must parse as UUID; community/tenant/app from scope only |
| Membership | `SnapshotMembership` pre-filter at call time; re-checked inside write transaction (`membership.rs`) |
| Reason | `[a-z0-9._:-]`, ≤100 chars, machine code, never PII |
| Ledger | One `bundle_reputation_adjustments` row per applied adjust, same transaction |
| Wire | Carried as `capability=db`, `op="reputation.get"` / `"reputation.adjust"`; gate authorizes, not wire kind |
| Feature flag | `waddles.bundle-reputation-capability`; OFF → `feature_disabled` |

## Economy (`economy`) — IN-FLIGHT #751 (stacked on #741)

| Item | Value |
|---|---|
| Permissions | `economy.read` — Normal, 20 calls/s; `economy.wager` — Dangerous; `economy.transfer` — Dangerous |
| WIT | `balance(user) -> result<s64, error>`; `wager(user, stake: u64, payout: u64) -> result<s64, error>`; `transfer(from-user, to-user, amount: u64) -> result<_, error>`; `max-bet(user) -> result<s64, error>`; `leaderboard(limit: u32) -> result<list<entry>, error>`; `entry` = `user: string`, `balance: s64` |
| Error | `denied(string)`, `insufficient-funds(u64)`, `over-cap(u64)`, `not-a-member`, `invalid(string)`, `unavailable(string)`, `backend(string)` |
| Caps: wager | Per call 1000; per user daily 10k; per community daily 250k (`EconomyAmount`) |
| Caps: transfer | Per call 1000; per sender daily 5k; per community daily 100k |
| Caps: stake/payout | Stake ≤ grant `max_bet` clamped to ceiling; payout ≤100× stake |
| Atomicity | `wager` = one guarded `UPDATE … WHERE balance >= stake`; `CHECK (balance >= 0)`; ledger row via data-modifying CTE; `transfer` locks both rows in uuid order |
| Membership | Gate pre-filter + re-check in store (`not-a-member`) |
| Feature flag | `waddles.bundle-economy-capability` |

## Overlay (`overlay.media`) — WIT MERGED, no handler

| Item | Value |
|---|---|
| Permission | `overlay.media` — Dangerous — 1 call / 10 s — resource `Overlay` |
| WIT (stage-next) | `register-widget(widget: widget-descriptor) -> result<transport-result, transport-error>`; `push(widget-id: string, surface: surface, payload-json: string) -> result<transport-result, transport-error>` |
| Scope | Community and token resolved server-side; bundle never sees either |
| Status | No `overlay` arm in `svc_process` or `svc_action` handlers; executor binds `world stage` only (`core/bundle_executor/src/engine.rs:24`) |

## Streaming Lifecycle (`streaming.lifecycle.subscribe`) — WIT MERGED

| Item | Value |
|---|---|
| Permission | `streaming.lifecycle.subscribe` — Normal — no call-rate quota (host-pushed) |
| WIT (stage-next export) | `on-start`, `on-stop`, `on-segment`, `on-recording-ready` → `result<option<platform-event>, unsupported-stage>` |
| Status | Export defined; host registration per grant deferred (`permission.rs`) |

## CATALOG-ONLY Permissions

| Permission | Tier | Catalog quota | Notes |
|---|---|---|---|
| `moderation.<platform>` | Dangerous | Relay authz + `UsageBatcher` | No WIT import |
| `ai.generate` | Dangerous | 10 / 60 s | Enterprise gate at marketplace time, not in gate |
| `storage.objects` | Normal | 1k objects / 500 MB per (tenant, app) | No WIT import |
| `users.profile.read` | Normal | Unlimited | Reputation-scoped membership check; non-identifying attributes only |
| `telemetry.logs`, `telemetry.metrics` | Normal | Declared instruments only | Mechanics owned by a separate doc |
| `interaction.pii.receive` | Dangerous | Descriptive | Default NO; host PII filter is separate work |
| `platform.scheduled` | Normal | 60 s floor | hub-api CRUD only; no WIT import |

## Denial Vocabulary

### Gate (`Denied`, `denied.rs`)

| Reason | Trigger | Status |
|---|---|---|
| `instance_denied` | Instance policy deny (checked before grant lookup) | Active |
| `not_granted` | No grant snapshot or no grant entry (fail-closed) | Active |
| `resource_scope_mismatch` | Resource kind does not match family | Active |
| `user_not_in_scope` | Reputation/profile target not a member | Active |
| `delta_out_of_bounds` | Reputation delta outside declared or per-call bound | Active |
| `quota_exceeded` | Daily aggregate or quota ceiling | Active |
| `rate_limited` | Calls-per-window breach | Active |
| `unsupported_platform` | Platform not in compiled-in set | Parser only (`PermissionId::parse`) |
| `contains_pii` | Reserved for capability-specific checks | Reserved |

### Stage and transport codes

| Code | Source |
|---|---|
| `unknown_invoke` | `call_id` with no in-flight invoke (`svc_action/src/capabilities.rs`) |
| `unknown_provider` | Relay provider outside allowlist |
| `invalid_args` | Malformed host-call args |
| `secret_not_granted` | Secret ref not in activation grant map (`egress.rs`) |
| `feature_disabled` | Capability flag OFF |
| `not_implemented` | Op or capability not wired in this build |

## See Also

- `docs/architecture/bundle-host-capability-model.md` — enforcement pipeline, scope, identity, signing
- `docs/reference/bundle-network-permissions.md` — `net.http.*` manifest syntax
- `docs/APP_BUNDLE_AUTHORING.md` — bundle authoring
