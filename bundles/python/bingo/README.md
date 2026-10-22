# bingo (Python)

`!bingo` -- a per-community **chat bingo**: classic 5x5 B-I-N-G-O cards (numbers 1-75), a mod
calls numbers, the first viewer to complete a row, column or diagonal wins the round.
First-party (`provider: builtin`, `app_id: waddles.core.example.bingo`), kv-only, WASI component.
Original Waddles content.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped -- one round record + two atomic counters; **no cards are stored** |
| Catalog | NOT yet in `bundles/core-bundles.yaml` (added at registration time) |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!bingo start` | mod/broadcaster | Begins a NEW round (new seed). Refused while a round is still running. |
| `!bingo call` | mod/broadcaster | Calls the next number (`G-52 (call #7 of 75)`). After 75, refuses and says so. |
| `!bingo` / `!bingo card` | anyone | Your own card, one line, called numbers in `[brackets]`, centre `[FREE]`. |
| `!bingo bingo` / `!bingo claim` | anyone | Claims a line. False claim -> "not yet" (free to retry); first TRUE claim wins and ends the round. |
| `!bingo status` / `!bingo list` | anyone | Round state, `N/75` called, the latest 8 calls (or the winner). |
| `!bingo end` | mod/broadcaster | Ends the running round with no winner. |
| anything else | anyone | Usage reply -- never silently dropped. |

The mod gate is strict-boolean and **fail-closed**: only a real `is_mod is True` /
`is_broadcaster is True` admits; absent badges (Discord's normalizer today), strings and ints
deny, before any `kv` access. Domain verbs are special-cased ahead of
`waddle_sdk.command.parse_command` (not in the shared `VERBS`), exactly like `raffle`; the bare
form and `list` go through the real grammar. Every verb takes no arguments: a claim never carries
card data from chat -- it is verified server-side.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | the round record, the call counter, the winner claim and the winner record |
| `chat.send:discord` / `chat.send:twitch` | replies through the relay host import |

No `db`, no `http`/egress, no `flags.read`, no AI.

## How it stays stateless-ish

A round holds ONE random `seed`. Everything else is recomputed on demand from pure SHA-256
rankings:

- **card** = `f(seed, pseudonym)`: column `c` (B/I/N/G/O) takes the 5 numbers of `15c+1..15c+15`
  ranked lowest by `sha256(seed, pseudonym, n)`; the centre is free.
- **call order** = `g(seed)`: 1..75 ranked by `sha256(seed, n)`; the called set is its first
  `calls` entries.

So nobody's card is persisted, a viewer's card never changes within a round, and the seed (which
never leaves kv) means nobody can precompute another viewer's card or the call order.

| Key | TTL | Value |
|---|---|---|
| `bingo.rid` | 30 d | round counter (`increment`) |
| `bingo.cur` | 3 d | JSON `{rid, seed, ended}` (written by `start`/`end` only) |
| `bingo.calls.<rid>` | 3 d | numbers called so far (`increment`) |
| `bingo.won.<rid>` | 3 d | winner claim (`increment`): the caller who sees `1` wins |
| `bingo.winner.<rid>` | 3 d | JSON `{t: token \| null, p: 8 hex}` for `status` after the win |

Colon-free keys, prefixed `c.<community>.` by `community_kv`; an abandoned round expires after 3
days. `community=None` is the tenant-wide sentinel (scope `0`); an empty id fails loud. Each
command costs at most ~5 kv operations (host budget: 64).

**Concurrency.** `kv.increment` is the only compare-and-swap, so two simultaneous valid claims
cannot both win. If the winner record fails to write, the claim is released so the next valid
claim wins; `!bingo end` always works to unstick a round (also when the winner record is unreadable).

## Safety

- **PII-free.** The pseudonym `sha256(platform, author_id or actor)` is used in memory to derive a
  card; persisted: 8 hex of it plus the host-minted `{user:<token>}` mention placeholder of the
  winner (re-validated against the token grammar before it is ever emitted). Logs carry the op and
  counts only.
- **Echoed text is sanitized.** The only user-derived text echoed is the claimant's label: the
  host placeholder kept live via `allow_tokens`, else `waddle_sdk.safe_text.sanitize_user_text`
  output (raw names, forged `{user:..}`, `@everyone`, control characters).
- **A token is only ever the host-minted one (security review).** The winner label is built as
  `{user:<token>}`, so the token comes from `event.actor` only when the WHOLE actor is one
  placeholder (`fullmatch`, ASCII token grammar, <= 256 chars; padded, embedded, doubled, escaped
  or look-alike actors are raw names) and is validated again when read back. Tests check this
  against the SDK's port of the egress detokenizer: in every reply the live placeholders are exactly
  the expected token.
- **No reply can lead with a chat command.** Replies that begin with the viewer's label pass
  through `guard_leading_command` (a raw handle starting `/`, `.` or `!` gets a speech-bubble
  prefix instead of running as the bot's own command).
- **Fail loud.** `kv` errors and corrupt records are logged (`op` + exception class) and answered
  in chat; never a silent default. A mod's `start` recovers a corrupt round record.
- **Randomness.** The round seed is 128 bits from `random.SystemRandom()` (OS entropy), never the
  module-level `random` functions (componentize-py freezes their state at build time) nor a hash
  of public inputs such as the start time (which would let a viewer reconstruct the call order).

## Not built

Multiple winners / blackout / custom patterns, prizes or economy payouts, card persistence, a
timer that calls numbers on its own. An unrelated first-to-type number guess already exists as
`guess`.

## Test

```bash
uv venv -p 3.13 .venv   # at the repo/worktree root, never under bundles/
uv pip install --python .venv/bin/python3 -e sdk/waddle-sdk pytest pytest-cov mypy ruff
cd bundles/python/bingo
../../../.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
The suite uses `waddle_sdk.testing.FakeKvHost` (real key validation + atomic `increment`) behind a
failure-injection wrapper -- there is no structured-`db` table, so no `wit_fake_db`.

This bundle imports `waddle_sdk.safe_text`, which lives on `feature/sdk-output-sanitizer`
(merged into this branch); the sanitizer PR must land before this one.
