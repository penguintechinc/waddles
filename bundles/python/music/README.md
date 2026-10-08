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

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-caller identity is a SHA-256 hash of the actor (never the raw username/actor id
-- see `src/app.py::_pseudonym()`), ahead of the PII-tokenization pipeline (#427/#429) in case
`event.actor` is still a raw username.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `music:queue` | per-community | none | JSON array of `{id, requester_pseudonym, text, ts}`, oldest-first -- the whole queue. |
| `music:seq` | per-community | none | Monotonic request-id counter (`community_kv.increment`). |
| `music:config:max-per-user` | per-community | none | Admin-configured per-caller max-pending-requests limit. |

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

**Not yet wired into `bundles/Dockerfile.core-bundles` or `bundles/core-bundles.yaml`** --
catalog registration is a batched step done separately (see this bundle's PR description), same
as `fish`/`shoutout`.

## Test

```bash
cd bundles/python/music
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../../../sdk/waddle-sdk
pip install pytest==8.3.3 mypy==1.14.1 ruff==0.14.1
pytest --cov=src --cov-report=term-missing
mypy --strict src
ruff check .
```

## Activation

Catalog row for `bundles/core-bundles.yaml` (`waddles.core.example.music`) is intentionally **not**
added by this PR -- batched registration later, per task instructions.
