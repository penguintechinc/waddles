# Bundle Host Capability Model

How app bundles reach host capabilities: sandbox, gate, scope, grants, identity, signing.
Per-capability WIT, permission ids, and limits: `docs/reference/host-capabilities.md`.

Verified against `release/v3.0.X` (`1e6b0056`) plus in-flight PRs #741, #749, #751 (2026-10-09).

## Principles

| Rule | Enforced by |
|---|---|
| Bundles hold no ambient authority — only WIT imports linked in their world | `core/bundle_executor/src/engine.rs` (`bindgen!`) |
| Every host import calls the gate first | `CapabilityGate::authorize` (`core/bundle_capability_gate/src/gate.rs`) |
| Fail-closed — no grant, miss, or error = deny | `gate.rs` (`not_granted`), grant loader (`svc_process/src/grant_gate.rs`) |
| Guest never supplies tenant, community, app_id, schema, or table | `InvokeScope` host-built only (`scope.rs`); WIT `db` doc |
| No raw SQL or raw secret crosses into the guest | WIT `db` (typed ops), WIT `http` (`secret-refs`) |
| Users referenced by UUID; raw PII stays in hub-api | `svc_process/src/pii_tokenize.rs`; see Identity |
| No kill-switch for the gate | `gate.rs` / `lib.rs` module doc |

## Call Path

```
bundle (wasm component, world stage | stage-next)
  │ WIT import            stage.wit
  ▼
bundle_executor           host-call { capability, op, args, call_id }
  │                       penguin_bundle_host::wire::HostCallBody
  ▼
svc_process | svc_action  per-invoke CapabilityHandler::handle
  ├─ 1. CapabilityGate::authorize(scope, PermissionId, ResourceRef)   <- gate first
  ├─ 2. flag / wiring check   -> feature_disabled | not_implemented
  ├─ 3. capability impl      bundle_host_kv | bundle_host_db | bundle_host_http
  │                          | relay (Valkey/Discord) | reputation | economy
  └─ host-result  |  { code, message }
```

- Gate runs before flag and wiring checks, so ungranted calls report `not_granted`, never `feature_disabled` (`svc_process/src/capabilities.rs`, `handle_db`).
- `http` adds a second boundary after the gate: `EgressGuard` (SSRF, allowlist, DNS pinning). Gate = permission; guard = network policy. Neither substitutes for the other.

## World Binding

| World | Imports | Binding status |
|---|---|---|
| `stage` (1.0.0) | `context`, `http`, `kv`, `db`, `relay`, `%flags`, `log`, `clock` | MERGED — bound by executor (`engine.rs:24`) |
| `stage-next` | `stage` + `overlay`, `reputation`, `economy` | `overlay` WIT MERGED, unbound; `reputation` IN-FLIGHT #741 (`engine.rs:49` on branch); `economy` IN-FLIGHT #751 |
| `stage-next` export | `streaming-lifecycle` | WIT MERGED; no host registration |

- `stage-next` is additive; `stage` 1.0.0 is unchanged (`stage.wit` header).
- #741 links unhandled `overlay` calls to a non-retryable `not_implemented` transport error (fail loud, never fabricated success).

## Gate Pipeline

`authorize(scope, permission, resource)` — `core/bundle_capability_gate/src/gate.rs:68-230`.

| Step | Check | Denial |
|---|---|---|
| 1 | Instance policy = Deny for family (checked before grant lookup) | `instance_denied` |
| 2 | Grant snapshot and entry present for `(tenant, community, app_id, app_version)` | `not_granted` |
| 3 | Resource kind matches family's expected resource | `resource_scope_mismatch` |
| 4 | Reputation-scoped: target is member of community or tenant, at call time | `user_not_in_scope` |
| 5 | Reputation delta: within declared bounds and per-call cap | `delta_out_of_bounds` |
| 6 | Quotas: per-user and per-scope daily aggregates (delta); calls-per-window (other) | `quota_exceeded`, `rate_limited` |
| 7 | Audit record; return `AuthorizedCall { permission, resource, params }` | — |

- `params` carries the community's declared values (e.g. `max_bet`), clamped to catalog ceilings.
- Catalog and tiers: `core/bundle_capability_gate/src/permission.rs` (`catalog_entry`). Risk: Normal (shown on consent, never blocks approval) or Dangerous (explicit reviewer/admin ack on first sight).

## Scope

| Property | Mechanism |
|---|---|
| Fields | `(tenant, community, app_id, app_version, tier)` (`scope.rs`) |
| Construction | `HostInvokeScopeBuilder::build` only; all fields private |
| Proof | Compile-fail test: no struct literal outside crate (`tests/compile-fail/invoke_scope_no_struct_literal.rs`) |
| Per invoke | `svc_action` maps `call_id` → scope; unknown `call_id` → `unknown_invoke` (`svc_action/src/capabilities.rs` module doc) |
| Source | Server-resolved invocation envelope — never guest args |

## Grants, Membership, Quotas

| Input | Production source (release) | Refresh | Miss / failure |
|---|---|---|---|
| Grants | `community_permission_grants` ⋈ `app_permission_requests` ⋈ `app_versions` (`PgGrantLoader`) | Valkey push-invalidate + poll | Deny (`not_granted`) |
| Platform always-granted | `AlwaysGrantedLoader`: `platform.context`, `platform.clock`, `platform.log` | — | Never missing |
| Membership | `InMemoryMembership::new()` in `build_production_gate` (`grant_gate.rs`) — **empty** | — | Every reputation-scoped call → `user_not_in_scope` |
| Membership (#741) | `SnapshotMembership`, fed from `community_members` | Loader on grant-cache cadence | Empty snapshot = deny; writes re-check live table |
| Instance policy | `InMemoryInstancePolicySnapshot::new()` — defaults (`net.http.private-ip` = Deny, others Allow) | — | — |
| Quotas | `InMemoryQuotaLedger` — per process | Resets on restart | Not shared across replicas |
| Durable daily caps | Reputation/economy store, inside write transaction, from audit ledger | Per write | IN-FLIGHT #741 / #751 |

- Release has no reputation/economy handler, so the empty membership snapshot affects no live capability today.
- Per-process quota is a known limit: each replica keeps its own counters, so the effective cap scales with replica count until durable caps land (#741, #751).

## Signed Bundles (#431, MERGED)

| Item | Value |
|---|---|
| Signer | hub-api, at global-tier approval (`hub_api/services/bundle_signing_service.py`) |
| Algorithm | Ed25519 over length-prefixed, domain-separated payload |
| Verifier | `core/bundle_executor/src/signing.rs` — every load, before instantiation |
| Failure | Missing or invalid signature → `SignatureInvalid`, never instantiated, never a warning |
| Key config | `BUNDLE_SIGNING_PUBLIC_KEYS` (`core/bundle_executor/src/config.rs`); unset → fails closed at startup |
| Carrier | `.json` sidecar beside compiled component in bucket |
| Schema | `alembic/versions/0040_bundle_artifact_signature.py` |

The payload encoding must match `build_signing_payload` byte-for-byte on both sides.

## Identity and PII Boundary

### Flow

```
platform event (svc_ingest)
  │ raw actor, @mention, user_id, author_id
  ▼
svc_process pii_tokenize   ── MintEphemeralPseudonyms ──►  hub-api IdentityService
  │                                                         (PII boundary; per-tenant secret)
  │ actor      := {user:<pseudonym-uuid>}
  │ mentions   := {user:<pseudonym-uuid>}
  ▼
bundle transform() / action dispatch   — UUIDs only, never raw handle or display name
  │ UUIDs in kv, tables (user_ref), reputation/economy targets
  ▼
svc_action outbound text    ── ResolveDisplayNames ──►  hub-api   (flag-gated)
  ▼
platform relay
```

### Rules

| Rule | Source |
|---|---|
| Tokenization runs after hop verification, before any guest invoke | `pii_tokenize.rs` module doc; `spine.rs` |
| Actor and mentions replaced by `{user:<uuid>}` placeholder | `pii_tokenize.rs` (`format_user_token`, actor substitution) |
| Pseudonyms minted by hub-api with per-tenant secret — no local UUIDv5 | `pii_tokenize.rs` module doc |
| Mint failure → event dead-lettered; no fallback pseudonym | `pii_tokenize.rs` (fail-closed) |
| Reputation/economy target must parse as UUID (else `invalid`) | `reputation`/`economy` WIT docs |
| Display names resolved only at action stage, only via hub-api | `svc_action/src/capabilities.rs`; `core/egress_detokenizer/src/lib.rs` |
| Raw PII in interaction inputs is filtered unless `interaction.pii.receive` granted | `permission.rs` (`InteractionPiiReceive`, default NO) — enforcement separate work |

### Identity PR Status

| PR | State | Scope |
|---|---|---|
| #429 | CLOSED, unmerged | Original tokenization PR; superseded |
| #746 | MERGED | `MintEphemeralPseudonyms`; `community_members.user_uuid` |
| #748 | MERGED | Handle/mention → UUID; `ResolveDisplayNames` |
| #434 | MERGED | `hub_users.uuid` column |
| #464 | MERGED | `waddles_connector_pii_reader` read-only role |
| #756 | Merged into stacked base, NOT on release | Forged-UUID, tenant-isolation, erasure hardening |
| #757 | OPEN, targets release | Same hardening, release-targeted |
| #755 | OPEN | Tenant-scope connector PII reader |
| #440 | OPEN | Encrypt identity fields on ingest → process stream |

## Capability Rollout Status

| Item | State | PR |
|---|---|---|
| Gate wired into `svc_process` and `svc_action` | MERGED | #433 |
| Platform artifact signing | MERGED | #431 |
| Typed `db` columns (`bundle_host_db`) | MERGED | #753 |
| Scope columns bound as integers | IN-FLIGHT | #760 |
| Egress wire alignment (`http.send` body, headers) | IN-FLIGHT | #759 |
| Egress `?key` query-param secret refs | IN-FLIGHT | #749 |
| Reputation (`stage-next`, `SnapshotMembership`) | IN-FLIGHT | #741 |
| Economy (stacked on #741) | IN-FLIGHT | #751 |
| Bundle-http CI workflow | IN-FLIGHT | #761 |

## Known Gaps

| Gap | Impact | Tracking |
|---|---|---|
| Production membership snapshot empty | Blocks reputation/profile reads until wired | #741 |
| Quota ledger per-process | Per-replica limits, not global | #741, #751 (durable caps) |
| `overlay` / `streaming-lifecycle` have no host handler | WIT surface exists, calls fail loud | Overlay: #741 doc cites #716/#457; streaming: #456 |
| `flags.tier` not wired | Returns `not_implemented` | `TODO(M4+)` in `svc_process/src/capabilities.rs` |
| Catalog-only permissions (moderation, AI, objects, users, telemetry, PII, scheduled) | No WIT import or handler | None found in open PR list |

## Stale Comments (Gate Wins)

| Location | Claim | Actual |
|---|---|---|
| `wit/waddle-bundle/stage.wit` `interface kv` | "always granted" | Requires explicit `storage.kv` grant |
| `wit/waddle-bundle/stage.wit` `%flags` | "always granted" | Requires explicit `flags.read` grant |
| `core/bundle_host_db/src/lib.rs` header | `query` not implemented; raw `execute` WIT | `query` wired in `svc_process`; typed WIT |

## See Also

- `docs/reference/host-capabilities.md` — per-capability reference
- `docs/reference/bundle-network-permissions.md` — `net.http.*` manifest syntax
- `docs/APP_BUNDLE_AUTHORING.md` — bundle authoring
