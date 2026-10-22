# vibecheck (Python)

`!vibecheck [@user]` -> a random fun 'vibe: X%' mood readout, optionally for a mentioned user. First-party (`provider: builtin`, `app_id: waddles.core.example.vibecheck`), stateless
(no kv/db), WASI component. Static SFW lines original to Waddles; no AI, no external API.

| | |
|---|---|
| Gating | PostHog flag `waddles.command-vibecheck` (default OFF), checked after the command matches |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none |

## Usage

`!vibecheck [@user]` -- anything malformed replies with usage (never silently
dropped, never echoed). A mention is echoed only as the host `{user:<token>}` placeholder.

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | feature-flag gate |
| `chat.send:discord` / `chat.send:twitch` | relay the reply |

## PII

Logs carry only the platform (no actor, token, or message text). No raw handle is stored or echoed.
