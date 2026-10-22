# queue (Python)

`!queue` -- a per-community **viewer queue**: viewers `join`/`leave` a line, anyone can see their
place or the front of the line, and a moderator advances or clears it. First-party
(`provider: builtin`, `app_id: waddles.core.example.queue`, `author: PenguinTech/waddles`,
Apache-2.0), **original Waddles content** -- the viewer-queue idea is a common stream-tool concept,
but no third-party source, code or text is used, so no attribution applies. kv-only
(`storage.kv`); no `db`, no egress.

> **Command collision with `music`.** `bundles/python/music` already answers `!queue` (its alias
> for `!music`, the song-request queue). Activating **both** bundles in one community makes every
> `!queue ...` line reach both. Activate one per community, drop `music`'s `!queue` alias, or
> rename this command (`_HEAD`/`SPEC` in `src/app.py` plus the two `command_prefix` filters).

## Commands

The front of the queue is position 1.

| Command | Who | Behavior |
|---|---|---|
| `!queue join` | anyone | Adds the caller to the back. A second `join` while queued replies with the existing position and changes nothing. |
| `!queue leave` | anyone | Removes the caller. Not queued -> a friendly "you're not in the queue", nothing changes. |
| `!queue` | anyone | The caller's own place: `#2 of 5`, or how to join. Pure read. |
| `!queue list [n]` | anyone | The front `n` entries (default 10, max 15) and a `(+K more)` tail. Pure read. |
| `!queue next` | **mod / broadcaster** | Pops the front and announces who is up and how many still wait. Empty -> "The queue is empty." |
| `!queue clear` (alias `reset`) | **mod / broadcaster** | Empties the queue and says how many were removed. |

`join`/`leave`/`next`/`clear` are not in the shared `waddle_sdk.command.VERBS` vocabulary, so they
are special-cased ahead of `parse_command()` (same documented extension as `raffle`/`music`); bare,
`list` and `reset` go through the real grammar. Any other verb, a malformed `list` count, or
trailing arguments on a domain verb replies with usage text -- never silently dropped. Matching is
case-insensitive and whitespace-tolerant; the exact first token must be `!queue` (`!queuex` is not).

```text
alice> !queue join
bot>   alice joined the queue at position 1.
bob>   !queue join
bot>   bob joined the queue at position 2.
bob>   !queue join
bot>   bob, you're already in the queue at position 2.
bob>   !queue
bot>   bob, you're #2 of 2 in the queue.
viewer> !queue list
bot>   Queue (2): 1. alice 2. bob
mod>   !queue next
bot>   Next up: alice. 1 still waiting.
viewer> !queue next
bot>   Only the broadcaster or a moderator can use !queue next.
```

(Names above are what the host's egress detokenizer renders for the `{user:<id>}` placeholders the
bundle emits.)

## Identity and PII

A queue entry has to be re-displayable to *other* viewers, so a one-way hash alone is not enough.
Following `lastseen`, identity is the pipeline's opaque `{user:<id>}` token in `event.actor`: the
bare id is stored (`u:<id>`) and replies **emit** `{user:<id>}` so the host renders the display
name. Until ingest tokenization is universal `event.actor` may still be a raw platform handle; then:

| `event.actor` | Stored entry | Shown as |
|---|---|---|
| `{user:<id>}` (well-formed, id <= 128 chars) | `u:<id>` | the host-rendered display name |
| any other non-empty string (raw handle) | `h:<24 hex of SHA-256(community|actor)>` | `viewer-<6 hex>` (stable per viewer, so `!queue` still finds your place) |
| missing / blank | nothing | "I couldn't identify you ..." -- never a guessed entry |

No raw handle ever reaches a kv key, a kv value, a log line or a reply (regression:
`test_a_raw_handle_never_reaches_kv_logs_or_replies`). Logs carry only `command`, counts and
exception class names.

## State (`kv`, community-scoped)

All keys go through `waddle_sdk.community_kv` (`c.<community_id>.` prefix; `None` is the host's
tenant-wide sentinel `0`, an empty string fails loud). Keys are `.`-separated, never `:` (gh-631).

| Key | Value | TTL |
|---|---|---|
| `queue.entries` | JSON array of entries, front first (<= 100) | 7 days, refreshed on every write (an abandoned queue expires) |
| `queue.lock` | `kv.increment` counter used as a mutex | 10 s |

### Concurrency

A read-modify-write of the single JSON list is serialized by the `queue.lock` mutex
(`kv.increment` is the only compare-and-swap the host offers: it returns `1` to exactly one
caller). A caller that cannot take it after three immediate attempts (no sleep exists under WASI)
gets "The queue is busy right now, please try again in a moment." instead of a silently lost
update; the TTL bounds a crashed holder. Reads take no lock. Every command costs <= 5 kv
operations (host cap: 64).

## Failure semantics (fail loud)

| Condition | Behavior |
|---|---|
| `kv` backend error | ERROR `queue.kv_error` (`op` + exception class), chat reply "temporarily unavailable", `RuntimeError("queue kv <op> failed: <Type>")`; the lock is still released. |
| Corrupt stored list (non-UTF-8 / non-JSON / not an array / malformed entry / over the cap) | ERROR `queue.state_corrupt`, chat reply "corrupted", `RuntimeError` -- the blob is **never** overwritten or reset. |
| Lock contended | WARN `queue.lock_busy`, "busy, try again" reply, no state change. |
| Empty-string `community` | ERROR `queue.missing_community`, chat reply, `ValueError`. |
| Missing `channel_id` / unknown command | `ValueError`. |

## Output safety

Every reply is built from validated tokens, hash labels, integers and constant text. The one place
user-typed text can be echoed is the usage reply to a malformed command (grammar errors quote the
offending token); that text goes through `waddle_sdk.safe_text.sanitize_user_text` (no
`allow_tokens`), and the usage text leads the reply so a bot line never starts with `!`, `/` or `.`.

## Permissions (V2, `bundle.yaml` / `hub-manifest.yaml`)

| id | Why |
|---|---|
| `storage.kv` | The per-community queue list and its short-lived mutation lock. |
| `chat.send:discord` / `chat.send:twitch` | Replies via the relay host import. |

No `flags.read`, no `db`, no egress. Mod gate: `next`/`clear` require a real `is_mod`/`is_broadcaster`
`True` (`is True`, never truthiness -- the string `"false"` is denied); **absent** badge fields
(e.g. the Discord normalizer today) are **denied** (fail closed) with zero kv access.

## Gating

Activation-gated only (installed -> enabled -> activated), **not** feature-flag gated:
`transform()` carries no flag check. See `docs/reference/command-bundles.md`.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!queue"]`; replies go
back to the event's own origin platform + channel.

## Not built (documented extension points)

Multiple/named queues, `skip`/`move` verbs, subscriber-priority ordering, DMing the next viewer
(the reply names them through their mention token only), cross-community queues.

## Test

```bash
cd bundles/python/queue
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```
