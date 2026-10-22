# insult (Python)

`!insult [@user]` -- a playful insult, from a built-in static list of original, SFW, light-hearted lines.
First-party (`provider: builtin`, `app_id: waddles.core.example.insult`), stateless, WASI component.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`) |
| Permissions | `chat.send:discord`, `chat.send:twitch` |

| Command | Behavior |
|---|---|
| `!insult` | Addresses the caller. |
| `!insult @user` | Addresses the mentioned user. |
| anything else | Usage / "mention them" reply -- never silently dropped. |

PII-free: the caller and the @mention arrive as `{user:<token>}` placeholders and are echoed
back as-is for the egress detokenizer; raw handles are rejected, never echoed, stored or logged.
Tests: `python3 -m pytest bundles/python/insult/tests`.
