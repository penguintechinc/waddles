# Community economy service

`waddles.core.service.economy` is an always-on trusted service bundle. It exports
`get_balance`, `points_view`, `adjust_balance`, `transfer`, `give`, `points_set`,
`pay_pot`, `exchange`, and `points_leaders` through `tool.invoke`.

Every caller requires an approved grant for the specific target/tool. Mint and
pot tools are dangerous app capabilities: grant them only to approved apps whose
policy is trusted to award points. They are not public chat endpoints.
`points_set` additionally requires host-verified `community:admin` scope; caller
arguments cannot attest an actor or role. Host capabilities separately enforce
membership, actor-bound spends, per-community daily limits, supply ceilings and
append-only idempotency ledgers. Each mutation calls one atomic host operation.
Admin adjustments deliberately bypass daily earn windows while retaining the
community supply ceiling.

Community/app-scoped KV key `policy` may contain JSON
`{"alias":"waddlebucks","rate_num":500,"rate_den":1000}`. An absent key uses
these defaults. Invalid or unavailable configuration fails closed. Exchange is
an idempotent claim on externally verified points: it does not debit a remote
Twitch account. Only a trusted caller that verified the external redemption may
hold the exchange tool grant. Rates cannot be supplied by callers.

Build: `cargo component build --release --target wasm32-wasip2 --locked`.
Unit tests: `cargo test --locked`; host-store integration tests:
`cargo test --locked --manifest-path ../../../core/bundle_host_economy/Cargo.toml`.
The trusted core seeder installs the service from the catalog. Tool host metrics
and logs cover nested dispatch; the guest additionally logs bounded outcome codes.

The legacy loyalty `!points` bundle has not yet been migrated. Its pseudonym
balances require an explicit identity mapping before any one-way backfill to
UUID-keyed economy balances. Do not automatically backfill live balances.
