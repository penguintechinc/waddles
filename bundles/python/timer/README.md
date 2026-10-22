# timer (Python)

`!timer` -- **recurring auto-announcement messages** ("follow the socials", "drink water"):
moderators register a message with an interval and the bot posts it periodically. First-party
(`provider: builtin`, `app_id: waddles.core.example.timer`, `author: PenguinTech/waddles`,
Apache-2.0), **original Waddles content** -- recurring announcements are a common stream-tool
concept, but no third-party source, code or text is used, so no attribution applies. kv-only
(`storage.kv`); no `db`, no egress.

> **Scheduler verdict: there is no tick/timer event, so firing is chat-driven.** The data plane
> delivers `chat.*`, `stream.online`/`stream.offline` and a few `channel.*` events -- nothing
> periodic -- and the WIT `stage` worlds expose no scheduler/cron capability and no way to push a
> message other than as a reply to an inbound event (the same gap `remind` documents, gh-613). So
> the bundle cannot post on a wall-clock schedule. It posts a due timer on the **first chat line
> after the interval has elapsed** instead (the model most stream timers use anyway). See
> "How firing works" and "Follow-up" below.

## Commands (all moderator / broadcaster only)

| Command | Behavior |
|---|---|
| `!timer add <name> <interval-min> <message>` | Registers a timer **bound to this channel**. Name 1-24 of `[a-z0-9_-]` (case-insensitive), interval 5-1440 minutes, message 1-300 chars, at most 10 timers per community. Starts enabled; the first post is one full interval away. |
| `!timer del <name>` (also `delete` / `remove`) | Deletes a timer. |
| `!timer list` | Every timer: `name (30m, on): <30-char preview>`; stops at ~420 chars with `(+K more)`. |
| `!timer enable <name>` / `!timer disable <name>` | Pause / resume. Enabling **restarts the countdown**, so a long-disabled timer does not fire the instant it is switched back on. |
| `!timer reset` | Deletes ALL timers -- also the recovery path for a corrupted config blob. |

`del` is normalised to the shared grammar's `delete` verb; everything else goes through the real
`waddle_sdk.command.parse_command()`. A bare `!timer`, an unknown verb, a missing name or trailing
arguments reply with usage text -- never silently dropped. Non-mods get "Only the broadcaster or a
moderator can manage timers." with zero kv access.

```text
mod>    !timer add promo 30 Follow us on Discord: discord.example/join
bot>    Timer 'promo' added: posts here every 30 min once chat is active (it needs a chat line to
        post, so a silent channel stays silent).
viewer> hello chat                                         (28 min later -- nothing yet)
viewer> anyone here?                                       (31 min later)
bot>    Follow us on Discord: discord.example/join
mod>    !timer list
bot>    Timers (1): promo (30m, on): Follow us on Discord: disco…
mod>    !timer disable promo
bot>    Timer 'promo' disabled.
```

## How firing works

The manifest has **no** `command_prefix` filter, so the bundle sees every chat line in the
community (like `greeter`). `transform` forwards a `!timer` line as a command and any other line
as a silent `tick`; `dispatch` -- which alone knows the community and owns `kv` -- posts at most
**one** due timer per tick.

| Property | Behavior |
|---|---|
| Due time | `max(anchor, last fired) + interval`. The anchor is set at `add` and again at `enable`. |
| Late, never early | A timer posts on the first chat line at/after its due time. Granularity = how often your chat talks. |
| Silent channel | Posts nothing (no reader, no spam into the void). |
| Long silence | Posts **once** on the next line, then waits a full interval from *that* moment -- no catch-up burst. |
| Many due at once | The most overdue posts first, one per chat line; the rest follow on later lines. |
| Channel binding | A timer remembers the platform+channel it was added in (a 16-hex SHA-256 -- the raw id is never stored) and only that channel's chat triggers it and receives the post. |
| Concurrent chat | A `kv.increment` claim keyed `(timer, due-time)` makes exactly one racing line post; the winner records the new last-fired time **before** relaying, so a failed relay is a missed announcement, never a repeat. |
| Cost per chat line | 1 `kv.get` when nothing is enabled for the channel; plus 1 `get` per enabled timer of that channel (<= 10); plus claim + set only when one fires. Host cap: 64 ops. |

The tick path never replies to chat on a failure (it runs for every line, so a reply would spam the
channel): a `kv` error or corrupt config logs ERROR and raises, and a mod fixes it with
`!timer reset`.

## Follow-up: a real scheduler capability

Wall-clock firing in a silent channel, "only while the stream is live", and "minimum N chat lines
between posts" all need a host capability the data plane does not have yet: a periodic trigger
(a `timer.tick` event or a `schedule` import) plus a way to send without an inbound event. When
that lands, `_handle_tick` in `src/app.py` is the single function to point it at -- the storage
model, claim logic, validation and sanitization are unchanged. Tracked with `remind`'s delivery gap
(gh-613).

## State (`kv`, community-scoped)

All keys go through `waddle_sdk.community_kv` (`c.<community_id>.` prefix; `None` is the host's
tenant-wide sentinel `0`, an empty string fails loud). Keys are `.`-separated, never `:` (gh-631).

| Key | Value | TTL |
|---|---|---|
| `timer.cfg` | JSON `{"v":1,"timers":[{n,i,m,e,c,a}]}` -- name, interval, message, enabled, channel hash, anchor ms | none |
| `timer.last.<name>` | last-fired time (ms) | 30 days; expiry falls back to the anchor |
| `timer.claim.<name>.<due-ms>` | `kv.increment` claim for one occurrence | 5 min |
| `timer.lock` | `kv.increment` mutex for admin edits | 10 s |

Admin edits (`add`/`del`/`enable`/`disable`/`reset`) read-modify-write the single config blob under
`timer.lock`; a mod that cannot take it after three immediate attempts gets "busy, try again"
instead of a silently lost edit. The config is strictly validated on every load (types, bounds,
duplicate names, entry cap); a bad blob fails loud and is never silently reset.

## Output safety and PII

- The stored message is mod-authored text echoed later, so it is
  `waddle_sdk.safe_text.sanitize_user_text`-ed at **every** output (the post and the `list`
  preview), with no `allow_tokens` -- nothing stored can stay a live mention or egress token.
  `list` truncates *before* sanitizing so an escape pair is never split.
- Refused at `add` time: a message containing a mention (a stored `{user:<token>}` would later
  resolve to a different person) and a message whose first visible character is `/`, `.` or `!`
  (the host relays a bot line verbatim, so `/ban x` would run as the bot, possibly above the
  author's own rights; invisible/format prefixes are seen through). The same lead check runs
  again at post time as defence in depth.
- A timer stores no author. Logs carry only `command`, counts and exception class names -- never
  names, messages or channel ids.

## Failure semantics (fail loud)

| Condition | Behavior |
|---|---|
| Admin `kv` error | ERROR `timer.kv_error` (`op` + class), chat reply "temporarily unavailable", `RuntimeError`; the lock is still released. |
| Tick `kv` error | ERROR + `RuntimeError`, **no chat reply**. |
| Corrupt config | ERROR `timer.state_corrupt`; admin commands reply "corrupted -- run !timer reset"; blob never overwritten except by `reset`. |
| Corrupt/negative `timer.last.<name>` | ERROR `timer.last_corrupt`, falls back to the anchor (the tick still works). |
| Stored message with a command lead / empty after sanitizing | ERROR `timer.unsafe_message_skipped`, not posted. |
| Empty-string `community` | ERROR + `ValueError` (chat reply for admin commands only). |
| Missing `channel_id` / unknown command | `ValueError`. |

## Permissions (V2, `bundle.yaml` / `hub-manifest.yaml`)

| id | Why |
|---|---|
| `storage.kv` | Timer config, last-fired times, claim and lock keys. |
| `chat.send:discord` / `chat.send:twitch` | Replies to `!timer` commands and posts due announcements. |

No `flags.read`, no `db`, no egress. Mod gate is `is True` on `is_mod`/`is_broadcaster`; absent badge
fields (e.g. the Discord normalizer today) are denied.

## Gating

Activation-gated only (installed -> enabled -> activated), **not** feature-flag gated:
`transform()` carries no flag check. See `docs/reference/command-bundles.md`.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with **no** `command_prefix` filter (the tick
needs every chat line -- see "How firing works"); replies and posts go back to the event's own
origin platform + channel.

## Not built (documented extension points)

Wall-clock firing / live-only / min-chat-lines gating (need the scheduler capability above),
`$(variable)` templating, per-timer channel re-binding, rotating message lists, cross-community
timers.

## Test

```bash
cd bundles/python/timer
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```
