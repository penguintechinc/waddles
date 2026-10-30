# waddles.core.service.reputation

Always-on `kind: service` bundle (Rust, `world stage-tools`) that will own community
reputation behind tool calls (bundle-MCP-tools P5). It exports `tool-provider` and holds the
`reputation` host capability (`reputation.read`, `reputation.community.write`,
`reputation.community.admin`) plus `storage.kv` for delta-policy/tier config.

Status: skeleton + registration (Task 2). The tool table is empty; an unknown tool returns a
typed `invalid-args` error. Tools (`get_score`, `get_many`, `adjust`, `override`,
`leaderboard`, `rep`) land in Task 3.

Build: `cargo component build --release --target wasm32-wasip2 --locked`
(the core-bundle image does this in `bundles/Dockerfile.core-bundles`).
