# eli5 (Python, AI-backed)

`!eli5 <thing>` -- a simple 'explain like I'm 5' explanation of the thing. First-party (`provider: builtin`,
`app_id: waddles.core.example.eli5`), stateless, WASI component built against `world stage-next`
(the `ai` import). Modeled on `eightball`.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); **no feature flag** |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress) |
| AI | exactly one `ai.generate(json=False)` per invocation, PROCESS stage only |

## Commands

| Input | Behavior |
|---|---|
| `!eli5` | Usage reply, no AI call. |
| `!eli5 <thing>` | One AI call, reply relayed to the origin platform. |
| over 200 chars | "Too long" reply, no AI call. |
| PII in input | Host rejects (`contains_pii`) -> "please remove personal info" reply. |
| any other AI error | Raised (fail loud) -- never a canned/empty reply. |

## AI pattern

| Concern | Choice |
|---|---|
| Permission | `ai.generate` (Dangerous -- sends user text to the model; host rate limits 10/60 s) |
| Model | Host-picked (`json=False` -> default text model) |
| Options | `max_tokens=160`, `temperature=0.7` |
| Output | Untrusted: whitespace-collapsed, capped at 400 chars, then relayed |

## Permissions (V2)

| id | why |
|---|---|
| `ai.generate` | one text generation per command |
| `chat.send:discord` / `chat.send:twitch` | reply on the origin platform via `relay.push` |

## Behavior notes

- **PII-free logs.** Only command, platform, and input/output lengths -- never the thing, actor,
  or AI output (gh-674).
- Full e2e needs a live Ollama; unit tests use `waddle_sdk.testing.install_fake_ai_host`.
- Not in `bundles/core-bundles.yaml` yet (catalog registration is a separate change).

## Output safety

The model completion is untrusted text, so it passes through `waddle_sdk.safe_text.sanitize_user_text` last, before it enters the reply: a forged `{user:<token>}` placeholder is escaped and `@everyone`/`@here`/`<@id>` markup is defanged. No `allow_tokens` -- nothing in model output is a host-minted mention.
