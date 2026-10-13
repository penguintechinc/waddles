# hangman (Python)

`!hangman` -- a per-community hangman word-guessing game. One active game per community; any
chatter can `guess`. First-party, net-new Waddles content (`provider: builtin`,
`app_id: waddles.core.example.hangman`) -- not a port of any external project, so there is no
third-party attribution to carry.

Structurally a sibling of `bundles/python/first`: `transform` only recognizes the command and
forwards a normalized action, `dispatch` does every `kv` read/write and relays the reply.

## Commands

| Command | Behavior |
|---|---|
| `!hangman start` | Picks a word from the embedded `WORD_BANK` (8 fixed words, `random.choice`) and opens the community's game: 6 lives, all letters masked. **If a game is already active it is never overwritten** -- the current masked state is re-shown instead, so a careless double `start` cannot discard an in-progress game. |
| `!hangman guess <letter>` | One a-z letter (case-insensitive). Correct: revealed everywhere it appears; revealing the last letter **wins** and clears the game. Wrong: costs a life; the 6th wrong guess **loses**, reveals the word and clears the game. An already-guessed letter is a no-op reply (never double-penalized). |
| `!hangman reveal` / bare `!hangman` | Pure read: masked word, sorted guessed letters, lives left. Never ends or mutates the game. |

Everything else replies with usage text -- never a silent drop: `!hangman guess` (no letter),
`!hangman bogus`, a grammar-legal-but-meaningless verb (`!hangman list`), or an option. A
`guess` argument that is not exactly one ASCII letter replies `Usage: !hangman guess <a single
letter, a-z>` and touches no state. `guess`/`reveal` with no active game reply "No hangman game
is active right now".

`start`/`reveal` are declared sub-modules of the shared grammar
(`waddle_sdk.command.CommandSpec`/`parse_command`); `guess <letter>` takes a free-text tail the
grammar's sub-module shape cannot express, so -- the same documented fallback `rps` uses -- a
grammar-shape failure is retried as a `guess` invocation (`src/app.py::_classify`).

## Examples

```text
> !hangman start
Hangman started! _ _ _ _ _ _ (6 letters, 6 lives) -- guess with !hangman guess <letter>
> !hangman guess a      # the word is "waddle"
Nice! _ a _ _ _ _ (6 lives left)
> !hangman guess z
Nope, no 'z'. _ a _ _ _ _ (5 lives left)
> !hangman reveal
_ a _ _ _ _ -- guessed: a, z (5 lives left)
```

## Permissions (V2, `bundle.yaml` / `hub-manifest.yaml`)

| id | Why |
|---|---|
| `storage.kv` | Persists the in-progress per-community game (the word, guessed letters, wrong count). |
| `flags.read` | Reads the `waddles.command-hangman` feature flag that gates the command. |

No `db`, no egress (`egress: []`), no other capability.

## Feature flag

`waddles.command-hangman`, default **OFF** (`critical-rules.md` Feature Flags & License Tiers).
Checked in `transform()` after the cheap `!hangman` head match, so unrelated chat costs no
flag/`kv` lookups. Flag off => no reply at all.

## Platforms

Twitch and Discord `chat.message` events whose text starts with `!hangman`
(`stages.process.consumes`). Replies relay back to the originating platform/channel.

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by the envelope's community --
`dispatch` raises `ValueError` if the envelope has **no community** (there is no tenant-wide
fallback -- a game can only live inside one community).

| Key | TTL | Value |
|---|---|---|
| `hangman.active` | 1 h (auto-expires an abandoned game) | JSON `{"word", "guessed": [...], "wrong": n}` |

Keys use `.` as the separator, never `:` -- the host rejects `:` (gh-631); the tests pin this
against the shared charset-enforcing `waddle_sdk.testing.FakeKvHost`.

No player identity is stored: the record holds only the word, guessed letters and a counter.

## Failure behavior (fail-loud, never silent)

| Condition | Behavior |
|---|---|
| `kv` get/set/delete raises (backend / too-large) | Chat reply "hangman is temporarily unavailable, try again shortly.", ERROR log `hangman.kv_error` carrying only `op` + the WIT error **case name**, then `RuntimeError` re-raised so the pipeline sees the failure. A failed write leaves the stored game untouched. |
| Corrupt stored game (not UTF-8, bad JSON, wrong shape) | ERROR log `hangman.state_corrupt` (community id only); reads as "no active game" so the next `start` replaces it -- self-healing, never a crash. |
| Missing `channel_id`, missing community, unknown action | `ValueError` from `dispatch`. |

## Logging / PII

Log lines carry only the resolved `action`, `platform`, `op`/error-case name, community id and
word length -- **never** the raw message, the guess argument, `event.actor`, or any exception
text (regression: gh-674; `tests/test_app.py` drives every verb and every failure path with a
sentinel string and asserts it never appears in any log message or field).

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, V2 permissions |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer) |
| `src/app.py` | `transform` / `dispatch` -- the whole game |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime; shared `FakeKvHost`) |

## Test

```bash
cd bundles/python/hangman
python3 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==6.0.0 mypy==1.14.1 ruff==0.14.1
# tests/conftest.py puts this bundle's src/ and the SDK's src/ on sys.path directly.
pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90
MYPYPATH=../../../sdk/waddle-sdk/src mypy --strict src
ruff check .
```

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.hangman`, activation target
`global`); the command stays dark until `waddles.command-hangman` is turned on.
