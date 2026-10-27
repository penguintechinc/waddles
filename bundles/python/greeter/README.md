# greeter (Python)

Opt-in keyword/event auto-responder: greets people, acknowledges the bot's name, and answers
thanks -- plus a mod-only `!greeter` command to switch each responder on/off. Standalone-bundle
reimplementation of the free-text half of `core/svc_process/builtin_handlers/bot_process.py`
(`_handle_keyword`). First-party (`provider: builtin`, `app_id: waddles.core.example.greeter`),
kv-only, WASI component.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (responder toggles + per-user cooldowns) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Behavior at a glance

- **Defaults OFF.** Every responder is a sub-module that is OFF per community until a mod
  enables it (`SubModuleGate`); there is no feature flag. A community that
  never enabled anything gets **zero** auto-replies.
- **No self-reply, no loops.** Never answers the bot's own messages or another bot (see below).
- **Per-user cooldown.** After replying to a user in a category, that user is not answered again
  in that category until the cooldown elapses.
- **Precise matching.** Token-boundary regexes, case-insensitive: "ship" never matches "hi",
  "party" never matches "ty". Non-matching text returns `None` (no reply).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!greeter` | anyone | Usage line plus current on/off state of all three responders. |
| `!greeter list` | anyone | `greeter responders: greet=on, mention=off, thanks=off`. |
| `!greeter <sub>` | anyone | `greeter greet is on.` / `is off.` |
| `!greeter enable <sub>` | mod/broadcaster | Turns a responder on for this community. |
| `!greeter disable <sub>` | mod/broadcaster | Turns it off again (back to default). |

`enable`/`disable` take exactly one sub-module; anything else (unknown sub-module, missing/extra
argument, unknown option) replies with the parser's usage error -- never silently dropped. The
mod gate **fails closed**: it needs a real `bool` `is_mod`/`is_broadcaster`; a missing or
non-boolean badge (Discord today, `"true"`, `1`) is denied.

## Sub-modules (responders)

All default OFF per community. Precedence when one message matches several: `greet` > `mention`
> `thanks`; the first **enabled** match answers, and one message gets at most one reply.

| Sub-module | Matches | Reply | Cooldown (per user) |
|---|---|---|---|
| `greet` | `hi`/`hello`/`hey`/`yo`/`howdy` at the **start** of the message | `Hey @<user>! 👋` | 1 h |
| `mention` | `@waddle`/`@waddles`/`@waddlebot`, or bare `waddles`/`waddlebot` | friendly acknowledgement | 60 s |
| `thanks` | `thanks`/`thank you`/`thankyou`/`thx`/`ty` anywhere | `You're welcome! 🐧` | 60 s |

If the first enabled match is inside its cooldown the message gets **no** reply (it does not
fall through to a lower-precedence category).

## Consume filter (manifest)

`greeter` must see ordinary chat lines, not only `!greeter`, so `consumes` declares
`twitch` + `discord` `chat.message` with **no `filters.command_prefix`**. Per the data-plane
design spec an absent filter key is "not a constraint", so the stage is woken for every chat
line. `transform()` therefore orders its work cheapest-first: non-text / other `!commands` ->
bot guard -> regex -> kv only after a category actually matched. Messages
that match nothing cost zero kv operations.

## No self-reply / bot guard

Ingest already drops the bot's own echoed messages and does not yet emit an `is_bot` flag, so an
absent flag means "human". Any signal that **is** present is read strictly and fails closed:

- `is_bot` / `author_is_bot` / `bot` must be exactly `False` -- `True`, `"false"`, `None`, `1`
  all skip the message;
- `author_id` equal to `bot_id` / `self_id` / `bot_user_id` skips it (that is the bot itself);
- an actor named `waddles` / `waddlebot` is skipped as a last-resort loop guard.

The bot's own replies are also written to avoid re-triggering any category.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | per-community responder toggles and per-user reply cooldowns |

## State keys

All keys are `.`-separated (the host reserves `:`, gh-631) and prefixed `c.<community>.` by
`community_kv`; the tenant-wide sentinel scope is `c.0.`.

| Key | Value | TTL |
|---|---|---|
| `submodule.greeter.<sub>` | `1` when enabled (absent = OFF) | none |
| `greeter.cd.<category>.<sha256(platform, author id or actor)>` | last reply time, epoch ms | category cooldown |

The cooldown key holds a SHA-256 pseudonym, never the raw username or id. The stored timestamp is
re-checked on read, so a host that serves an expired entry cannot let a burst through.

## Behavior notes

- **Fail loud.** A kv failure logs `greeter.kv_failure` (op + exception type only). On the
  free-text path `transform()` raises (the pipeline sees a real failure, no silent no-reply); on
  the `!greeter` path the moderator gets a "temporarily unavailable" reply.
- **PII-free logs.** Logs carry only community / op / category / error type -- never the message
  text, the actor, an author id or a mention target. The actor appears only in the visible chat
  reply to that same user (`Hey @<user>!`), as in `lurk`.
- `transform()` builds the reply; `dispatch()` is a pure `relay.push` of it. Host imports (`kv`,
  `log`, `relay`, `clock`, sub-modules) are imported eagerly at module top
  (`docs/reference/bundle-host-imports.md`).

## Test

```bash
cd bundles/python/greeter
uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python3 pytest==8.3.3 pytest-cov mypy==1.14.1 ruff==0.14.1
.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
MYPYPATH=../../../sdk/waddle-sdk/src .venv/bin/mypy --strict src/
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
