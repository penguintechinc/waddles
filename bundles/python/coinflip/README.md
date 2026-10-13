# coinflip (Python)

`!flip` (alias `!coinflip`) -- a 50/50 coinflip minigame. Community-scoped fun content,
first-party (`provider: builtin`, `app_id: waddles.core.example.coinflip`). Net-new Waddles
content -- no inspiration source, no attribution/notice required (contrast `bundles/python/fish`,
which credits superpenguintv (Psychoboy) for its catch-and-track concept).

Uses the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`,
merged in #618), same pattern as `fish`/`slots`'s own `app.py`. Both chat aliases route through
one `CommandSpec(name="flip")` -- `transform()` normalizes `!coinflip ...` to `!flip ...` before
parsing.

**No betting/currency ties.** This bundle tracks only a flip count and a correct-call count --
there is no wallet, balance, points value, payout, transfer, or redemption path. This is a
standalone chat game, not a gambling economy feature.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!flip` / `!coinflip` | bare (no verb) | **Flip.** Draws one 50/50 `heads`/`tails` result and replies with a flavor line. No call was made, so there is no win/lose outcome -- only the caller's flip count is updated. |
| `!flip heads` / `!flip tails` | call (declared `sub_module`) | **Call.** Same 50/50 draw; the caller's guess is checked against the result. A match increments the caller's correct-call count; a miss does not. Either way the flip count is updated. |
| `!flip list` | `list` verb, no args | Reads the caller's own stats: total flips, correct calls. |
| `!flip set cooldown <seconds>` | `set` verb | Broadcaster/moderator only. Sets the community's flip cooldown, `0`-`3600` seconds (default `10`). `0` disables the cooldown entirely. |

Any other grammar-legal verb (`add`/`sub`/`enable`/`disable`/`remove`/`reset`), a `heads`/`tails`
call combined with another verb (e.g. `!flip heads list`), or a malformed `!flip ...` replies
with usage text -- never silently dropped.

```text
!flip                    -> 🪙 viewer-1 flips a coin -- tumbling end over end... HEADS! (flip #1)
!flip tails              -> 🪙 viewer-1 calls tails... HEADS! Not this time. (flip #2)
!flip list               -> Flips: 2. Correct calls: 0.
!coinflip set cooldown 0 (mod) -> coinflip cooldown set to 0s
!flip set cooldown 5  (viewer) -> only moderators/broadcasters can configure !flip
```

Platforms: Twitch + Discord (`chat.message`). The mod/broadcaster gate **fails closed** -- with
no `is_mod`/`is_broadcaster` on the event (Discord today) `set cooldown` is denied.

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-coinflip` |
| `storage.kv` | persists per-community coin-flip tallies and state |

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-caller keys use a SHA-256 hash of the actor as the pseudonym (never the raw
username/actor id -- see `src/app.py::_pseudonym()`), ahead of the PII-tokenization pipeline
(#427/#429) in case `event.actor` is still a raw username.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `coinflip.lastflip.<pseudonym>` | per-(community, caller) | `cooldown` seconds (`0` = never expires) | Cooldown gate -- presence + elapsed time decide allow/deny. |
| `coinflip.flips.<pseudonym>` | per-(community, caller) | none | Running total flips, bare + called (`kv.increment`). |
| `coinflip.wins.<pseudonym>` | per-(community, caller) | none | Running total correct calls (`kv.increment`, called flips only). |
| `coinflip.config.cooldown` | per-community | none | Admin-configured flip cooldown in seconds. |

Colon-free keys (gh-631) -- the `kv` host rejects `:`; `.` only.

## Deferred to v2 (not stubbed)

**Cross-community leaderboards.** Every key above is scoped to one community (`community_kv`'s
own rule: reputation and user-details are the platform's only two cross-community exceptions,
and this bundle is neither). A leaderboard needs a query that spans many communities/rows --
`kv` has no scan/list-keys primitive, so this needs a real `db` capability with an
`order_by`/pagination surface. **Deferred until the in-flight #623 `db` order_by API lands** --
no `!flip leaderboard` command is declared, and nothing here is a stand-in stub for it.

Also out of scope: any wallet/points-economy integration (betting an actual balance, redeeming a
payout) -- the no-betting-ties rule above, not partially stubbed.

## Feature flag

Gated behind `waddles.command-coinflip`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap `!flip`/`!coinflip` command-name match
and before the real grammar parse.

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
      componentize -p sdk/waddle-sdk/src -p bundles/python/coinflip/src \
      waddle_sdk._component_entry -o /tmp/coinflip.wasm"
```

## Behavior notes

- **Fail loud.** A `kv` backend error logs `coinflip.kv_error` at ERROR (error class only),
  replies "temporarily unavailable", then raises. Corrupt counters/cooldown state self-heal
  (read as 0 / default) but always log ERROR (`coinflip.*_corrupt`).
- **PII-free logs.** Logs never include the actor, its pseudonym, or typed text (gh-674: the
  invalid-cooldown log once echoed the raw typed value).

## Test

```bash
cd bundles/python/coinflip
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`tests/conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build or SDK
install needed.

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.coinflip`).
