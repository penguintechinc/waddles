# Reputation service bundle (`waddles.core.service.reputation`)

Always-on `kind: service` Rust/WASM bundle (bundle-MCP-tools P5, `bundles/rust/reputation`). It owns reputation
POLICY (the `!rep` UX, win/loss/loyalty deltas, tiers, admin override) and calls the existing `reputation` host
capability for every atomic score mutation. Other bundles reach it only through `tool.invoke`
(see `docs/bundle-tool-invoke.md`).

## Layering

```
app bundle (giveaway/raffle/economy)
   | tool.invoke  (requested_tools + P2 grant + app->service matrix)
   v
reputation service bundle  -- policy, tiers, validation (kv: policy, tiers)
   | reputation host cap   (reputation.read / .community.write / .community.admin)
   v
bundle_host_reputation     -- advisory lock, FOR UPDATE, 24h caps, clamp, ledger
```

The host cap is the single authority for atomicity, the per-user/per-scope rolling-24h caps, the +/-1,000,000
clamp and the audit ledger; the bundle cannot bypass any of them (caps come from the permission catalog, never
from the bundle). Host-cap op reference: `docs/reference/host-capabilities.md` (Reputation).

## Tools

| Tool | Access | Notes |
|---|---|---|
| `get_score {user}` | read | score + tier |
| `get_many {users<=100}` | read | one host round-trip; the giveaway/raffle draw-time rep filter uses this; a non-member fails the whole call |
| `leaderboard {limit?<=100}` | read | ranked, ties by user id |
| `rep {user}` | read | `!rep` text, tier progress |
| `adjust {user, event}` | write | `event` in win/loss/loyalty; the community policy picks the delta; needs an idempotency key; capped by the host |
| `override {user, absolute_score, reason?}` | write, dangerous | `required_scope: community:admin` -- the HOST verifies the actor's role and refuses before dispatch; the tool re-checks `ToolContext.actor_scopes`; `actor` is never an argument (unknown field rejected) |

Draw filter (economy spec `2026-10-29-waddlebucks-economy.md`): the draw reads candidate scores with `get_many`,
drops everyone below the community line, and an admin removes a fraudster by `override`-ing them below the line.

## Config (bundle kv, per community; keys contain no `:`)

`policy` `{"win":1,"loss":-1,"loyalty":1}` (|delta| <= 5); `tiers` `[{"name","min_score"}]` (1..=16, ascending).
Absent = defaults; present-but-invalid = loud `invalid-args`, never silently defaulted.

## Scope and limits

* Community-scoped only (`(community_id, user)`); the tenant-wide FICO score is a separate legacy system, a
  tenant-wide reputation bundle is a flagged follow-up (needs new tenant-scope host ops).
* Logs carry tool name + outcome only (no user input, no UUIDs of members).
* Tests: unit (`bundles/rust/reputation/src/logic.rs`), host cap (`core/bundle_host_reputation`, Postgres),
  stage handler (`core/svc_process`), and the real-path e2e `core/tool_invoke_e2e/tests/reputation_service_e2e.rs`
  (real wasm + executor + `tool.invoke` + gate: admin/non-admin override, 24h cap, community isolation, telemetry).
* The e2e fixture `core/tool_invoke_e2e/tests/fixtures/reputation_service.wasm` must be rebuilt when the bundle
  changes (see the README beside it).
* Migration: `alembic/versions/0072_reputation_host_ops.py` (score clamp CHECK + ledger columns).

## Legacy v2 defaults (reference for rebuild)

Captured from the deleted `core/reputation_module` (`services/weight_manager.py`, `services/policy_enforcer.py`,
`config.py`) before removal; see git history of `release/v3.0.X` prior to PR #1233.

* Score scale: FICO-style integer, `min_score` 300, `max_score` 850, `starting_score` 600.
* Activity weights (per event; whole integers): chat_message 1, command_usage 1, giveaway_entry -1, follow 1,
  subscription 5 (tier2 10, tier3 20), gift_subscription 3, donation_per_dollar 1, cheer_per_100bits 1, raid 2, boost 5.
* Moderation deltas: warn -25, timeout -50, kick -75, ban -200.
* Tiers: exceptional 800-850, very_good 740-799, good 670-739, fair 580-669, poor 300-579.
* Policy: `auto_ban_enabled` false by default, `auto_ban_threshold` 450 (score <= threshold triggers auto-ban,
  recorded as an `auto_ban` row in `reputation_events`, member status set to banned); score <= 450 and above the
  threshold produces a low-score notification only (no action).
* Premium-community weight overrides were stored per community (`CommunityWeights.is_premium`).
