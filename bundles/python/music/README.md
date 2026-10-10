# music (Python)

`!sr`/`!songrequest` + `!music`/`!queue` -- a per-community song-request QUEUE. Community-scoped
fun/utility content, first-party (`provider: builtin`, `app_id: waddles.core.example.music`),
**inspired by** superpenguintv (Psychoboy)'s
[PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) song-request feature -- not a
literal port. No original source code or text is reused; the command grammar, queue data shape,
and all logic are written fresh for this bundle. See `bundle.yaml`'s `author`/`notice` fields for
the credit, and contrast `bundles/csharp/superpenguin-roll`, which *is* a line-for-line port and
carries the full verbatim MIT notice because it reuses original code.

Uses the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`) for the
`!music`/`!queue` verb shapes. `!sr`/`!songrequest` are pure free-text "add" commands and never go
through the grammar -- same documented extension as `shoutout`'s own bare-positional-target case
(the request text may start with any token, including one that looks like a VERB).

**This is a request QUEUE, not a player.** No playback, no external music-service API call (no
`http`/egress capability declared) -- `!music next`/`!music skip` advance the data structure only.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!sr <link-or-text>` / `!songrequest <link-or-text>` | anyone | Adds a request to the caller's community queue. Subject to a per-caller max-pending limit (default `3`, admin-configurable). |
| `!music` / `!queue` | anyone | Shows the first 10 queue entries, marking the caller's own `(yours)`. |
| `!music list` | anyone | Same read as bare `!music`, reached through the `list` verb. |
| `!music remove <id>` | requester, or mod/broadcaster | Removes one request by its numeric id. |
| `!music next` / `!music skip` | mod/broadcaster only | Pops the head of the queue ("now playing" -> the next one up). |
| `!music set max-per-user <n>` | mod/broadcaster only | Sets the per-caller max-pending-requests limit, `1`-`10` (default `3`). |

Any other grammar-legal verb (`add`/`sub`/`enable`/`disable`/`reset`) or a malformed `!music ...`
replies with usage text -- never silently dropped. `next`/`skip` are bare advance keywords outside
the shared `waddle_sdk.command.VERBS` vocabulary, handled ahead of the formal grammar parse (see
`src/app.py`'s module docstring) -- not smuggled in as fake sub-modules.

## Examples

```text
> !sr never gonna give you up
added to the queue at position 1 (#1)
> !music
Queue (1): #1 never gonna give you up (yours)
> !music set max-per-user 5            (mod)
max-per-user set to 5
> !music next                          (mod)
now playing: never gonna give you up (queue is now empty)
> !music next                          (regular viewer)
only moderators/broadcasters can do that
```

## Permissions (V2, `bundle.yaml` / `hub-manifest.yaml`)

| id | Why |
|---|---|
| `storage.kv` | Persists the per-community song-request queue, its id sequence, and the max-per-user limit. |
| `flags.read` | Reads the `waddles.command-music` feature flag that gates the command. |

No `db` (the queue is one per-community JSON array under one kv key), no egress (`egress: []`).

## Moderator gate (fail-closed)

`!music next`/`skip` and `!music set max-per-user` require `is_mod` **or** `is_broadcaster` on the
normalized event (`_caller_role_signal()`), decided in `dispatch` **before any `kv` access**:
neither field present (Discord today) => denied; present but falsy => denied; either true =>
allowed. Denial logs `music.permission_denied` (command + role-signal state only). `!sr`,
`!music`, `!music list` are open to anyone; `!music remove <id>` is the requester **or** a
mod/broadcaster (a non-owner without the signal is refused and the queue is left untouched).

## Platforms

Twitch and Discord `chat.message` events starting with `!sr`, `!songrequest`, `!music` or `!queue`
(`stages.process.consumes`). Discord events carry no mod badge today, so the mod-only commands are
denied there until its normalizer supplies the fields.

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-caller identity is a SHA-256 hash of the actor (never the raw username/actor id
-- see `src/app.py::_pseudonym()`), ahead of the PII-tokenization pipeline (#427/#429) in case
`event.actor` is still a raw username.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `music.queue` | per-community | none | JSON array of `{id, requester_pseudonym, text, ts}`, oldest-first -- the whole queue (max 200 entries, 300 chars per request). |
| `music.seq` | per-community | none | Monotonic request-id counter (`community_kv.increment`) -- ids are never reused after a removal. |
| `music.config.max-per-user` | per-community | none | Admin-configured per-caller max-pending-requests limit (`1`-`10`, default `3`). |

Keys use `.` as the separator, never `:` (gh-631: the host rejects `:`; this bundle originally
built `music:queue`-style keys and was fixed before registration). A test validates every key the
bundle touches against the host charset.

Because only the pseudonym is retained, the queue listing cannot (and does not try to) show *who*
requested each song to the channel -- it marks the caller's own entries `(yours)` by comparing a
freshly computed pseudonym, the same "count, not identity" trade-off `shoutout`'s own `auto`
sub-module documents for its list command.

## Deferred (not stubbed)

**Actual playback / external music-service integration** (resolving a YouTube/Spotify link,
driving a now-playing overlay, auto-advancing on track end). This bundle is a request queue only;
wiring a real player needs an `http`/egress capability this bundle deliberately does not declare.
Not built here, not stubbed -- `!music next`/`!music skip` advance the queue data structure, which
is the whole of this v1's scope.

## Failure behavior (fail-loud, never silent)

| Condition | Behavior |
|---|---|
| `kv` get/set/increment raises | ERROR `music.kv_error` (`op` + WIT error **case name**), chat reply "the song queue is temporarily unavailable, try again shortly.", then `RuntimeError`. Exactly one relay. A failed save leaves the stored queue/config untouched. |
| Corrupt stored queue (not UTF-8, bad JSON, not an array, entry not an object / missing fields / non-integer `id` or `ts`) | ERROR `music.state_corrupt`, chat reply "the song queue is corrupted, please contact support.", then `RuntimeError` -- on **every** queue-reading command; the corrupt bytes are never overwritten or silently reset. |
| Corrupt `max-per-user` config | ERROR `music.max_per_user_config_corrupt`; falls back to the default `3`. |
| Corrupt id sequence | Loud `kv increment` failure; nothing is enqueued. |
| Missing `channel_id`, missing community (no tenant-wide fallback), unknown command | `ValueError`; missing community also logs ERROR `music.missing_community`. |
| `relay.push` fails | Propagates; no success line is logged. |

## Logging / PII

Every log message has a strict field allowlist (command, platform, community id, op/error class,
error type) -- **never** the raw message, request text, removal argument, config argument, or
`event.actor` (regression: gh-674, which fixed `raw=` fields on three lines here; the suite drives
every command and outcome with a sentinel string as actor and argument, and asserts its absence
plus the exact per-message field set). Known residual: the `music.state_corrupt` `reason` for a
corrupt queue *entry* interpolates the underlying parse error, which can echo the corrupt stored
`id`/`ts` value -- bundle-written integers, never chat text.

## Feature flag

Gated behind `waddles.command-music`, defaulted OFF (`critical-rules.md` Feature Flags & License
Tiers) -- checked in `transform()` after the cheap command-head match and before the real grammar
resolution.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full queue |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime) |

## Build

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir --user componentize-py==0.25.1 && \
    PATH=/tmp/.local/bin:\$PATH componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/music/src \
      waddle_sdk._component_entry -o /tmp/music.wasm"
```

## Test

```bash
cd bundles/python/music
python3 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==6.0.0 mypy==1.14.1 ruff==0.14.1
# tests/conftest.py puts this bundle's src/ and the SDK's src/ on sys.path directly.
pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90
```

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.music`, activation target
`global`); dark until `waddles.command-music` is turned on.
