# slots (Python)

`!slots` -- a weighted-random, per-community slot-machine minigame. Community-scoped fun
content, first-party (`provider: builtin`, `app_id: waddles.core.example.slots`). Net-new
Waddles content -- no inspiration source, no attribution/notice required (contrast
`bundles/python/fish`, which credits superpenguintv (Psychoboy) for its catch-and-track
concept).

Uses the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`,
merged in #618), same pattern as `fish`'s own `app.py`.

**No real-money/currency ties.** The "payout" tracked here is an abstract point value used
only to rank the caller's own best spin -- there is no wallet, balance, transfer, or
redemption path. This is a chat game, not a gambling economy feature.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!slots` | bare (no verb) | **Spin.** Draws three independent weighted-random reel symbols. All three matching is a win, paying the matched symbol's own point value; anything else is a loss. Updates the caller's total spin count, win count, and best-payout record. Subject to the per-caller cooldown. |
| `!slots list` | `list` verb, no args | Reads the caller's own stats: total spins, total wins, best payout (points + symbol). |
| `!slots set cooldown <seconds>` | `set` verb | Broadcaster/moderator only. Sets the community's spin cooldown, `5`-`3600` seconds (default `30`). |

Any other grammar-legal verb (`add`/`sub`/`enable`/`disable`/`remove`/`reset`) or a malformed
`!slots ...` replies with usage text -- never silently dropped.

## Reel table

Six weighted symbols, each reel drawn independently with replacement (`random.choices(...,
k=3)`); a win is all three reels landing on the same symbol, paid at that symbol's own point
value:

| Symbol | Relative weight | Payout (points) |
|---|---|---|
| Cherry | 40 | 5 |
| Lemon | 30 | 8 |
| Orange | 18 | 10 |
| Bell | 8 | 25 |
| Diamond | 3 | 75 |
| Seven | 1 | 250 |

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-caller keys use a SHA-256 hash of the actor as the pseudonym (never the raw
username/actor id -- see `src/app.py::_pseudonym()`), ahead of the PII-tokenization pipeline
(#427/#429) in case `event.actor` is still a raw username.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `slots.lastspin.<pseudonym>` | per-(community, caller) | `cooldown` seconds | Cooldown gate -- presence + elapsed time decide allow/deny. |
| `slots.spins.<pseudonym>` | per-(community, caller) | none | Running total spins (`kv.increment`). |
| `slots.wins.<pseudonym>` | per-(community, caller) | none | Running total wins (`kv.increment`, win spins only). |
| `slots.bestpayout.<pseudonym>` | per-(community, caller) | none | JSON `{symbol, payout}` of the caller's best (highest-payout) spin. |
| `slots.config.cooldown` | per-community | none | Admin-configured spin cooldown in seconds. |

## Deferred to v2 (not stubbed)

**Cross-community leaderboards.** Every key above is scoped to one community
(`community_kv`'s own rule: reputation and user-details are the platform's only two
cross-community exceptions, and this bundle is neither). A leaderboard needs a query that
spans many communities/rows -- `kv` has no scan/list-keys primitive, so this needs a real `db`
capability with an `order_by`/pagination surface. **Deferred until the in-flight #623 `db`
order_by API lands** -- no `!slots leaderboard` command is declared, and nothing here is a
stand-in stub for it.

Also out of scope: any wallet/points-economy integration (betting an actual balance, redeeming
payouts) -- the no-real-money-ties rule above, not partially stubbed.

## Feature flag

Gated behind `waddles.command-slots`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap `!slots` command-name match and
before the real grammar parse.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full game |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime) |

## Build

```bash
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir componentize-py==0.25.1 && \
    componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/slots/src \
      waddle_sdk._component_entry -o /tmp/slots.wasm"
```

## Test

```bash
cd bundles/python/slots
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../../../sdk/waddle-sdk
pip install pytest==8.3.3 mypy==1.14.1 ruff==0.14.1
pytest --cov=src --cov-report=term-missing
mypy --strict src
ruff check .
```

## Activation

**Not yet registered** in `bundles/core-bundles.yaml` or wired into
`bundles/Dockerfile.core-bundles` -- deliberately held back for a batched catalog
registration pass covering multiple new bundles at once (see this bundle's PR description).
The bundle is otherwise complete and buildable; the catalog entry/Dockerfile stage is the only
remaining step.
