# fish (Python)

`!fish` -- a weighted-random, per-community fishing minigame. Community-scoped fun content,
first-party (`provider: builtin`, `app_id: waddles.core.example.fish`), **inspired by**
superpenguintv (Psychoboy)'s [PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)
fishing feature -- not a literal port. No original source code or text is reused; the catch
table, weights, flavor text, cooldown mechanism, and all game logic are written fresh for this
bundle. See `bundle.yaml`'s `author`/`notice` fields for the credit, and contrast
`bundles/csharp/superpenguin-roll`, which *is* a line-for-line port and carries the full
verbatim MIT notice because it reuses original code.

First bundle in this repo to use the shared command-grammar parser
(`waddle_sdk.command.parse_command`/`CommandSpec`, merged in #618) instead of hand-rolled
`text.split()`/regex matching -- `lurk`/`count`/`roll` all predate it.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!fish` | bare (no verb) | **Cast.** Rolls one weighted-rarity catch, updates the caller's total catch count and biggest-catch record, replies with the result. Subject to the per-caller cooldown. |
| `!fish list` | `list` verb, no args | Reads the caller's own stats: total catches, biggest catch (rarity, name, weight). |
| `!fish set cooldown <seconds>` | `set` verb | Broadcaster/moderator only. Sets the community's cast cooldown, `5`-`3600` seconds (default `60`). |

Any other grammar-legal verb (`add`/`sub`/`enable`/`disable`/`remove`/`reset`) or a malformed
`!fish ...` replies with usage text -- never silently dropped.

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-caller keys use a SHA-256 hash of the actor as the pseudonym (never the raw
username/actor id -- see `src/app.py::_pseudonym()`), ahead of the PII-tokenization pipeline
(#427/#429) in case `event.actor` is still a raw username.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `fish:lastcast:<pseudonym>` | per-(community, caller) | `cooldown` seconds | Cooldown gate -- presence + elapsed time decide allow/deny. |
| `fish:count:<pseudonym>` | per-(community, caller) | none | Running total catches (`kv.increment`). |
| `fish:biggest:<pseudonym>` | per-(community, caller) | none | JSON `{name, rarity, weight_lbs}` of the caller's heaviest catch. |
| `fish:config:cooldown` | per-community | none | Admin-configured cast cooldown in seconds. |

## Deferred to v2 (not stubbed)

**Cross-community leaderboards.** Every key above is scoped to one community
(`community_kv`'s own rule: reputation and user-details are the platform's only two
cross-community exceptions, and this bundle is neither). A leaderboard needs a query that
spans many communities/rows -- `kv` has no scan/list-keys primitive, so this needs a real `db`
capability with an `order_by`/pagination surface. **Deferred until the in-flight #623 `db`
order_by API lands** -- no `!fish leaderboard` command is declared, and nothing here is a
stand-in stub for it.

Also out of scope for this kv-only v1 (see the much larger C#/DB-backed port spec,
`docs/superpowers/specs/2026-09-28-superpenguin-fish-game-port.md`): rod/line "snap" equipment
loss, a shop/economy, and tournaments.

## Feature flag

Gated behind `waddles.command-fish`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap `!fish` command-name match and
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
      componentize -p sdk/waddle-sdk/src -p bundles/python/fish/src \
      waddle_sdk._component_entry -o /tmp/fish.wasm"
```

**Not yet wired into `bundles/Dockerfile.core-bundles`** -- that file is being genericized in
a parallel PR (`chore/genericize-core-bundles-dockerfile`); this bundle's CI wasm build is
blocked on that PR landing. The command above is the same `componentize-py` invocation that
Dockerfile uses for the `python-batch1-builder` stage, run standalone to confirm this bundle
compiles today.

## Test

```bash
cd bundles/python/fish
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../../../sdk/waddle-sdk
pip install pytest==8.3.3 mypy==1.14.1 ruff==0.14.1
pytest --cov=src --cov-report=term-missing
mypy --strict src
ruff check .
```

## Activation

Catalog row added to `bundles/core-bundles.yaml` (`waddles.core.example.fish`) -- see that
file's own comment next to the entry for the Dockerfile dependency above. Until the
genericization PR lands and a wasm is built, the core-bundle-seeder will not find
`fish.wasm`/`fish.manifest.yaml` in a built image; this is expected and tracked, not a bug in
the catalog entry itself.
