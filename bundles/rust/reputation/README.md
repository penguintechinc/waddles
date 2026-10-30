# waddles.core.service.reputation

Always-on `kind: service` bundle (Rust, `world stage-tools`) that will own community
reputation behind tool calls (bundle-MCP-tools P5). It exports `tool-provider` and holds the
`reputation` host capability (`reputation.read`, `reputation.community.write`,
`reputation.community.admin`) plus `storage.kv` for delta-policy/tier config.

Tools (all args validated, unknown fields rejected, unknown tool = typed `invalid-args`):

| Tool | Access | What |
|---|---|---|
| `get_score {user}` | read | score + tier |
| `get_many {users<=100}` | read | batch scores + tiers (draw filter) |
| `adjust {user, event}` | write | `event` in win/loss/loyalty; the community delta POLICY picks the points (caller never names a number); host-keyed `adjust-many`, needs the caller's idempotency key |
| `override {actor, user, absolute_score, reason?}` | write, dangerous | admin-only: `actor` must be in the community `admins` kv list, else `forbidden` before any host call; host also requires `reputation.community.admin` |
| `leaderboard {limit?<=100}` | read | ranked entries |
| `rep {user}` | read | `!rep` UX: score, tier, progress to next tier, text |

Config (bundle kv, per community, absent = defaults, present-but-invalid = loud error):
`policy` `{"win":1,"loss":-1,"loyalty":1}` (|delta| <= 5, win/loyalty > 0, loss < 0);
`tiers` `[{"name","min_score"}]` (1..=16, ascending, first reaches -1000000);
`admins` `["<actor uuid>", ...]` (<= 100).
Logs carry tool name + outcome only.

Build: `cargo component build --release --target wasm32-wasip2 --locked`
(the core-bundle image does this in `bundles/Dockerfile.core-bundles`).
