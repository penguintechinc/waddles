# Pseudonym merge-on-claim: implementation plan

Status: PLAN ONLY (investigated 2026-10-17 against release/v3.0.X 881ac8dd). Not built: the change
spans SQL, Valkey, Rust data plane and an unresolved bundle-state merge semantic.

## Contract

Ingest resolve-or-create mints a provisional UUID (`ephemeral_pseudonyms.pseudonym`) for a
first-seen platform user. When the person later claims that platform account (OAuth link), the
provisional UUID merges into the canonical `hub_users.uuid`; user-scoped state carries over; an
alias (merged -> canonical) keeps in-flight `{user:<uuid>}` tokens and historical refs resolving.

## What exists today

| Piece | Where | State |
|---|---|---|
| Pseudonym store | `alembic/versions/0045_identity_resolution.py` (`ephemeral_pseudonyms`, unique tenant+platform+pid) | exists |
| Resolver | SQL `resolve_identity_uuid()` (0045, hardened 0048): explicit user_id, else hub link (only in a tenant the hub user belongs to), else get-or-create pseudonym | exists |
| Member binding | trigger `community_members_set_user_uuid` sets `community_members.user_uuid`; fires only on INSERT / UPDATE of user_id, platform, platform_user_id | exists |
| Mint RPC | `proto/.../identity.proto` `MintEphemeralPseudonyms`; `hub_api/grpc_internal/servicers.py` | exists |
| Display names | `identity_resolution_service.resolve_display_names` (+ `_HUB_NAMES_SQL`, `_PSEUDONYM_NAMES_SQL`); Rust consumer `core/egress_detokenizer` | exists, no alias awareness |
| Claim trigger points | `identity_service.identity_link_callback` (authenticated link), `oauth_service.link_callback` / login path; both end with `ban_suppression.reapply_for_hub_user_async` | exists, no merge call |
| Merge / alias / redirect | none (grep: no `merged_into`, `alias_of`, alias table) | MISSING |

Only partial continuity today: `_PSEUDONYM_LOGIN_CANDIDATES_SQL` re-points a pseudonym to the hub
user for handle lookup. `community_members.user_uuid` for already-created rows keeps the
pseudonym after a claim (trigger does not re-fire), so the data plane (`core/svc_process/src/identity.rs`
`ACTOR_SQL`) keeps using P in old rows while new rows or other communities get H: split identity.

## User-scoped state keyed on the UUID

| Store | Table / key | Key shape | Merge difficulty |
|---|---|---|---|
| SQL | `community_members.user_uuid` (+ unique `(community_id,user_uuid)`) | uuid | collision if P-row and H-row coexist in one community |
| SQL | `bundle_reputation_scores` | PK `(community_id,user_uuid)` | sum balances; PK collision |
| SQL | `bundle_reputation_adjustments.target_user_uuid` (VARCHAR36, append-only ledger, community + tenant scope) | uuid text | re-key; rolling 24h cap spans both |
| SQL | `economy_balances` | PK `(tenant,community,user_uuid)`, CHECK balance>=0 | sum balances |
| SQL | `economy_ledger.user_uuid`, `.counterparty_uuid` (append-only, runtime role has no UPDATE) | uuid | re-key as owner role |
| SQL | `caption_events.user_ref` (config 102 / alembic 0060) | uuid | re-key |
| SQL | `ephemeral_pseudonyms` itself | pseudonym PK | becomes alias source |
| Valkey | bundle kv `bundlekv:{tenant}:{community|_tenant}:{app}:data:{guest_key}` (+ `:count`); counters live here | uuid embedded in bundle-composed opaque key | cannot re-key generically; value merge (sum vs last-write) is per bundle |
| Qdrant | mem0 owner key `user_<uuid>` (`mem0_privacy.py`) | uuid | re-key memories |
| Rust | `core/egress_detokenizer`, `core/svc_process` identity/pii_tokenize, `core/hub_client` | uuid tokens | must follow alias |
| Governance | `erasure_registry.py` (USER_UUID keys; coverage test demands a rule per uuid column), `docs/PRIVACY.md` | n/a | new alias table + merge audit need rules |

## Decision: BALLOONS -> plan only

- 7+ SQL tables with differing conflict rules (sum vs re-key vs drop), 2 append-only ledgers.
- Valkey kv/counters: opaque bundle-composed keys, merge semantics undefined without a bundle-declared strategy.
- 3 Rust crates + Qdrant: cross-service rollout ordering.
- Unresolved policy questions below. Half-building (alias table without state carry-over) would
  silently orphan balances or double-count: forbidden by the no-stubs rule.

## Proposed design

1. Alias table (alembic 0062, id <= 32 chars, explicit downgrade): `identity_uuid_aliases(merged_uuid PK, canonical_uuid NOT NULL, tenant_id NOT NULL, merged_at, merge_id)`, `CHECK (merged_uuid <> canonical_uuid)`, no chains (canonical must not be a merged_uuid; flatten on write), REVOKE PUBLIC, reader grants only to roles that resolve. Plus `identity_merges` audit (ids only, no PII).
2. `canonicalize_identity_uuid(uuid)` SQL fn (single hop). `resolve_identity_uuid()` returns canonical for pseudonym results; `resolve_display_names`, `_PSEUDONYM_*_SQL`, handle/mention lookups, `ACTOR_SQL`, and egress detokenizer follow it.
3. `merge_pseudonym_into_user(tenant, pseudonym, hub_user_uuid)`: ONE transaction, advisory lock on both uuids, idempotent (existing alias same target = no-op; different target = hard error), tenant guard identical to 0048 (hub user must be a member of the tenant, else no merge: cross-tenant correlation). Per-table: re-key or sum per table above; delete-then-sum for collisions; append ledger rows re-keyed by owner role and recorded in `identity_merges`.
4. Trigger point: after link insert in `identity_link_callback` and `oauth_service.link_callback`, before `ban_suppression.reapply_for_hub_user_async`; per tenant where the hub user is a member, look up pseudonym by (tenant, platform, platform_user_id). Retry-safe like the existing already-linked branch. Gate: new opt-out kill-switch flag `waddles.disable-pseudonym-merge` (core mechanism, unseen = on).
5. Valkey/Qdrant: post-commit worker (outbox row in the merge txn) renames kv keys via the erasure scan pattern and re-keys mem0; failure leaves the alias in place so reads stay correct once the host kv capability reads through the alias.
6. Erasure: alias rows and `identity_merges` registered in `erasure_registry.py`; erasing H must also erase every merged_uuid (suppression hash continuity: hash key stays on the platform id, so a banned P merged into H still re-matches).

## Phasing (each a separate PR)

| PR | Scope |
|---|---|
| A | alias table + `canonicalize` + resolver/display-name/lookup alias-follow + erasure rules + tests |
| B | merge function for SQL tables + link-callback wiring + flag + real-PG integration tests (idempotency, double-merge, collision, tenant guard) |
| C | Valkey/Qdrant outbox worker + kv merge strategy |
| D | Rust: `ACTOR_SQL`, detokenizer, hub_client alias-follow + parity tests |

## Open decisions for the user

1. kv/counter merge: bundle-declared merge strategy in the manifest (ties to `2026-10-13-bundle-manifest-v-next`), or default sum-for-counters / canonical-wins-for-values?
2. Balances on collision: sum (recommended) vs canonical-wins; economy cap/wager history implications.
3. Claim in a tenant where the hub user is not a member: skip merge (recommended, 0048 rule) or auto-join?
4. Second platform account of one hub user inside one community (`uuid_collision` today): merge both pseudonyms, or keep the unavailable state?
