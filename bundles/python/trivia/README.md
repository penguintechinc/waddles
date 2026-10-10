# trivia (Python)

`!trivia` -- a per-community trivia Q&A game. `kv`-only, first-party (`provider: builtin`,
`app_id: waddles.core.example.trivia`, `author: PenguinTech/waddles`, Apache-2.0), **original
Waddles content** (not a port; no third-party attribution applies). The questions are a small,
fixed, auditable embedded bank (`QUESTION_BANK`, 8 entries) -- no external data source or
scheduler.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!trivia start` | anyone | Poses one random question and makes it the community's single active round. If a round is already active it **re-shows** that question instead of overwriting it. |
| `!trivia answer <text>` | anyone | Compares `<text>` (lowercased, whitespace-collapsed) to the active answer. First correct answer wins: the round ends, the winner's score increments. A wrong answer replies `Not quite -- try again!` and the round continues. |
| `!trivia score` / bare `!trivia` | anyone | The caller's own score plus the community's top 5 scorers. Pure read. |

Matching is exact after normalization: `PARIS`, `  paris ` and `Paris` match; `Paris!`, `the
paris` and `par is` do not. No active round -> `No trivia question is active right now -- start
one with !trivia start!` (state untouched). `answer` with no text, a grammar-legal-but-meaningless
verb (`!trivia list`) or any unknown token replies with the usage line -- never silently dropped.
(`answer <text>` takes a free-text tail the shared grammar can't express, so `transform` falls
back to this bundle's own `answer` vocabulary on a grammar-shape failure -- the same fallback `rps`
documents.)

```text
viewer> !trivia start
bot>    📝 Trivia time! What planet is known as the Red Planet? (answer with !trivia answer <your answer>)
viewer> !trivia answer mars
bot>    🎉 Correct, viewer! The answer was Mars. Your score: 1
viewer> !trivia score
bot>    viewer, your score: 1. Top scorers: user-1a2b3c4d (1)
```

## State (`kv`, community-scoped)

All keys go through `waddle_sdk.community_kv` (`c.<community_id>.` prefix) -- never global.

| Key | TTL | Purpose |
|---|---|---|
| `trivia.active` | **1 h** | JSON `{question, answer_raw, answer_norm}` -- the one active round; auto-expires so an abandoned round can't block a community. |
| `trivia.score.<pseudonym>` | durable | All-time score (`increment`). |
| `trivia.score.registry` | durable | Sorted JSON array of every scorer's pseudonym (drives the leaderboard). |

`<pseudonym>` is the SHA-256 of `event.actor` -- the raw actor never reaches a key, value or log.
The caller is addressed by their own live name in their own reply only; **other players are shown
only as `user-<8 hex>` handles**. Keys are `.`-separated, never `:` (gh-631).

### Failure semantics

| Condition | Behavior |
|---|---|
| `kv` backend error | ERROR `trivia.kv_error` (`op` + exception type only), chat reply "trivia is temporarily unavailable", then `RuntimeError("trivia kv <op> failed: <Type>")` -- fail loud. |
| Corrupt active round / score registry / a score value | ERROR `trivia.state_corrupt` with `context` (`active`/`score_registry`/`own_score`/`other_score`) + community, then treated as absent/`0` -- logged loudly, never silent. |
| No `community` on the envelope | ERROR `trivia.missing_community` + `ValueError` (no tenant-wide fallback). |
| Missing `channel_id` / unknown action | `ValueError`. |

**Known limitation:** `kv` has no compare-and-swap, so two correct answers landing in the same
instant can both read the active round before either deletes it; both are credited.

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `storage.kv` | Persists per-community trivia scores and active-round state. |
| `flags.read` | Gates the command behind its `waddles.command-trivia` feature flag. |

No `db`, no egress, no moderator-gated verb (anyone can start, answer and read scores).

## Feature flag

`waddles.command-trivia`, **default OFF**, checked after the cheap command match and before the
grammar parse, requested with `default=False` (flag outage / missing `wit_world` -> off). While off
nothing is parsed, stored or logged.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!trivia"]`; replies
go back to the event's own origin platform + channel.

## Logging / PII

Logs carry only `action`, `op`, `context`, `community`, the new score count and exception type
names -- never a guess, the answer text, grammar text or the raw actor. Regression:
`tests/test_backfill.py::test_no_log_line_in_any_flow_contains_the_guess_or_the_raw_actor`.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest + hub-api install-pipeline manifest |
| `src/app.py` | `transform` (recognize/classify) / `dispatch` (all `kv` I/O + relay) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | Host-native pytest suite (shared charset-enforcing `kv` fake) |

## Test

```bash
cd bundles/python/trivia
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```
