# horoscope (Python)

`!horoscope <sign>` -> a generic, clearly-for-fun horoscope for one of the 12 zodiac signs. First-party (`provider: builtin`, `app_id: waddles.core.example.horoscope`), stateless
(no kv/db), WASI component. Static SFW lines original to Waddles; no AI, no external API.

| | |
|---|---|
| Gating | PostHog flag `waddles.command-horoscope` (default OFF), checked after the command matches |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none |

## Usage

`!horoscope <sign> (aries, taurus, gemini, cancer, leo, virgo, libra, scorpio, sagittarius, capricorn, aquarius, pisces)` -- anything malformed replies with usage (never silently
dropped, never echoed).

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | feature-flag gate |
| `chat.send:discord` / `chat.send:twitch` | relay the reply |

## PII

Logs carry only the platform (no actor, token, or message text). No raw handle is stored or echoed.
