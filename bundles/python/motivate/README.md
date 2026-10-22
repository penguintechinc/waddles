# motivate (Python)

`!motivate [@user]` -> a random motivational line, optionally aimed at a mentioned user. First-party (`provider: builtin`, `app_id: waddles.core.example.motivate`), stateless
(no kv/db), WASI component. Static SFW lines original to Waddles; no AI, no external API.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none |

## Usage

`!motivate [@user]` -- anything malformed replies with usage (never silently
dropped, never echoed). A mention is echoed only as the host `{user:<token>}` placeholder.

## Permissions (V2)

| id | why |
|---|---|
| `chat.send:discord` / `chat.send:twitch` | relay the reply |

## PII

Logs carry only the platform (no actor, token, or message text). No raw handle is stored or echoed.
