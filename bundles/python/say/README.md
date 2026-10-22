# say

`!say <text>` -- the bot repeats `<text>` as an announcement. **Moderators and the broadcaster only.**

> Inspired by PenguinTwitchBot by SuperPenguinTV (with permission) -- ideas/design only, no code copied.
> (https://github.com/Psychoboy/PenguinTwitchBot). Original Waddles implementation; same
> inspiration-only credit shape as `bundles/python/fish`.

## Behavior

| Caller | Input | Reply |
|---|---|---|
| mod / broadcaster | `!say Stream starts in 5!` | `Stream starts in 5!` |
| mod / broadcaster | `!say` | `Usage: !say <text>` |
| mod / broadcaster | `!say !ban x` (leading `/` `.` `!`) | refused -- would make the bot run a command |
| mod / broadcaster | text > 300 chars | refused |
| anyone else (or no badge data) | `!say ...` | polite denial (never silent) |

## Role gate

`is_mod is True` OR `is_broadcaster is True` -- strict boolean identity, never `bool()`, so a
string badge (`"false"`) cannot pass. Missing badge fields (e.g. Discord's normalizer today)
deny. The gate is enforced in `dispatch` (not only `transform`), so a forged payload fails too.

## Permissions (least privilege)

`chat.send:discord`, `chat.send:twitch`. No kv, no flags, no egress. Activation-gated only (no
feature flag), matching the current `fish`/`echo` convention.

## Privacy

Logs carry only platform + outcome shape. The announced text and the actor are never logged.

## Files

`bundle.yaml`, `hub-manifest.yaml`, `src/app.py`, `src/_entry_wiring.py`, `tests/`.
