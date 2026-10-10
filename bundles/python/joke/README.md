# joke (Python)

`!joke` -- a random family-friendly joke, plus a per-community custom joke pool that
moderators manage. First-party (`provider: builtin`, `app_id: waddles.core.example.joke`); the
14 built-in jokes are original text written for Waddles, not copied from any third-party source,
so there is no external attribution to carry. kv-only (`waddle_sdk.community_kv`) -- it
deliberately does not depend on the `db` capability.

Built on the shared command grammar (`waddle_sdk.command.parse_command`/`CommandSpec`, first
adopted by `fish`, #618). `!joke` declares no sub-modules; `add`/`remove`/`list` are already
members of the shared `VERBS` vocabulary.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!joke` | anyone | One random joke from the built-in list **plus** the community's custom pool. Avoids immediately repeating the previous joke (best effort: a one-entry pool still replies). |
| `!joke list` | anyone | Lists the community's custom jokes as `#id: text` in numeric id order (built-ins are fixed source and never listed). Empty pool => "No custom jokes have been added yet." |
| `!joke add <text>` | **mod / broadcaster** | Adds a custom joke (trimmed; 1-300 chars) and replies `Added joke #<id> to the pool.` Ids are sequential and **never reused** after a removal. |
| `!joke remove <id>` | **mod / broadcaster** | Removes custom joke `<id>` (canonical decimal id only). Unknown id => "No custom joke #N exists."; non-numeric => "isn't a valid joke id". |

Anything else replies with usage text rather than being dropped: a grammar error, a grammar-legal
verb this bundle does not implement (`!joke enable`, `!joke set ...`), or `!joke list <extra>`.
`add`/`remove` with no argument reply with their own `Usage:` line.

## Moderator gate (fail-closed)

`add`/`remove` require `is_mod` **or** `is_broadcaster` in the normalized event
(`_caller_role_signal()`, same pattern as `fish`/`count`). The decision is made in `dispatch`
**before any `kv` access**:

| Event badge fields | Result |
|---|---|
| neither field present (e.g. Discord's normalizer today) | **denied** -- absence is never an implicit allow |
| present but falsy (`False`/`None`) | denied |
| `is_mod` true, `is_broadcaster` true, or both | allowed |

Denial replies "only moderators/broadcasters can manage the joke pool" and logs
`joke.permission_denied` (command + role-signal state only). Role claims typed into the joke text
grant nothing -- the signal comes only from the event payload. `!joke` / `!joke list` never need a role.

## Examples

```text
> !joke
How does a penguin build its house? Igloos it together.
> !joke add Why did the penguin cross the road? To get to the other tide.   (mod)
Added joke #1 to the pool.
> !joke list
Custom jokes: #1: Why did the penguin cross the road? To get to the other tide.
> !joke remove 1                                                             (mod)
Removed joke #1.
> !joke add sneaky                                                           (regular viewer)
only moderators/broadcasters can manage the joke pool
```

## Permissions (V2, `bundle.yaml` / `hub-manifest.yaml`)

| id | Why |
|---|---|
| `storage.kv` | Persists the community's custom joke registry, its id counter, and the last-served joke pointer. |
| `flags.read` | Reads the `waddles.command-joke` feature flag that gates the command. |

No `db`, no egress (`egress: []`, `data.tables: []`).

## Feature flag

`waddles.command-joke`, default **OFF** (`critical-rules.md` Feature Flags & License Tiers).
`transform()` order: cheap `!joke` head match -> flag check -> grammar parse, so unrelated chat
costs nothing. Flag off => no reply.

## Platforms

Twitch and Discord `chat.message` events whose text starts with `!joke`
(`stages.process.consumes`). Discord events carry no mod/broadcaster badge today, so
`add`/`remove` are denied there until its normalizer supplies the fields.

## State (kv, community-scoped only)

Every key goes through `waddle_sdk.community_kv` (scoped by community id; `dispatch` raises
`ValueError` when the envelope has no community -- no tenant-wide fallback). State is
**per-community**, not per-caller; no user identity is stored anywhere.

| Key | Value | TTL |
|---|---|---|
| `joke.custom.registry` | JSON object `{"<id>": "<text>"}` | none |
| `joke.custom.next_id` | monotonically increasing counter (`kv.increment`) | none |
| `joke.last` | ref of the last joke served (`b<n>` built-in / `c<id>` custom) | none |

Keys use `.` only, never `:` (gh-631: the host rejects `:`); the suite runs against the shared
charset-enforcing `waddle_sdk.testing.FakeKvHost`.

## Failure behavior (fail-loud, never silent)

| Condition | Behavior |
|---|---|
| `kv` get/set/increment raises | ERROR log `joke.kv_error` (`op` + host error text), chat reply "jokes are temporarily unavailable, try again shortly.", then `RuntimeError` -- failed writes leave stored state untouched. |
| Corrupt `joke.custom.registry` (not UTF-8, bad JSON, not an object, non-string values) | Same loud path on **every** command (`tell`/`list`/`add`/`remove`); the corrupt bytes are **never overwritten** or silently reset, so an operator can inspect them. |
| Missing `channel_id`, missing community, unknown command | `ValueError`. |
| `relay.push` fails | Propagates; no success line is logged. |

Known raw-failure edges (raise, never silently succeed, but skip the chat error reply): a
non-UTF-8 `joke.last` value, and a registry whose keys are valid strings but not numeric ids
(`!joke list` sorts by `int(id)`). Both are pinned by tests as "must raise".

## Logging / PII

Each log message has a strict field allowlist (command, platform, joke id, op/error) -- **never**
the raw message, joke text, removal argument, or `event.actor` (regression: gh-674; the suite
drives every verb, the denial path and the failure paths with a sentinel string and asserts both
the sentinel's absence and the exact per-message field set).

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, V2 permissions |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer) |
| `src/app.py` | `transform` / `dispatch` |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime; shared `FakeKvHost`) |

## Test

```bash
cd bundles/python/joke
python3 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==6.0.0 mypy==1.14.1 ruff==0.14.1
# tests/conftest.py puts this bundle's src/ and the SDK's src/ on sys.path directly.
pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90
```

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.joke`, activation target
`global`); dark until `waddles.command-joke` is turned on.
