# wordcount (Python)

Counts how many messages and words each user has sent (per community) and reports them with
`!wordcount [@user]`. First-party (`provider: builtin`, `app_id: waddles.core.example.wordcount`),
`kv`-backed, WASI component.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv.increment` counters `wordcount.msgs.<pseudonym>` / `wordcount.words.<pseudonym>` |
| Permissions | `storage.kv`, `chat.send:discord`, `chat.send:twitch` |

| Command | Behavior |
|---|---|
| `!wordcount` | Your message + word totals (includes the command message itself). |
| `!wordcount @user` | The mentioned user's totals (0/0 if never seen). |
| other args | Usage / "mention them" reply -- never silently dropped. |

PII-free: the actor and @mention arrive as `{user:<token>}`; the token id is SHA-256 hashed into
the pseudonym used in the key (the community is scoped server-side by the `kv` host). Raw handles
are rejected, never hashed/echoed/logged. Messages from an unidentifiable actor are not counted.
Words = whitespace-delimited tokens. Tests: `python3 -m pytest bundles/python/wordcount/tests`.
