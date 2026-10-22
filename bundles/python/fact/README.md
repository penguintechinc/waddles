# fact (Python)

`!fact` -> a random fun trivia fact from a static built-in list. First-party (`provider: builtin`, `app_id: waddles.core.example.fact`), stateless
(no kv/db), WASI component. Static SFW lines original to Waddles; no AI, no external API.

| | |
|---|---|
| Gating | PostHog flag `waddles.command-fact` (default OFF), checked after the command matches |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none |

## Usage

`!fact` -- anything malformed replies with usage (never silently
dropped, never echoed).

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | feature-flag gate |
| `chat.send:discord` / `chat.send:twitch` | relay the reply |

## PII

Logs carry only the platform (no actor, token, or message text). No raw handle is stored or echoed.
