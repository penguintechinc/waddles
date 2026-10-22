# tldr (`waddles.core.example.tldr`)

`!tldr <text>` -> a one-line TL;DR of the supplied text. Example: `!tldr <a long paragraph>`.

AI bundle modelled on `translate` (stateless, no kv). **Activation-gated only -- no feature flag**
(installed -> enabled -> activated per community).

## AI pattern

| Item | Value |
|---|---|
| Permission | `ai.generate` (Dangerous, consent-ack, 10 calls / 60 s) -- justification in `hub-manifest.yaml` |
| SDK call | `await ai.generate(prompt, max_tokens=60, temperature=0.3, json=False)` in `transform()` only; `dispatch()` relays |
| World | `stage-next` |
| Model | Host-controlled (`json=False` -> gemma e2b); never named by the bundle |
| Guards | Host rejects PII (`contains_pii`) -> "remove personal info" reply; input capped at 1500 chars; completion collapsed to one <=450-char line |

## Behaviour

- Empty text or over-cap text -> usage reply, no AI call. One AI call per invocation.
- Any AI refusal/failure -> clear chat reply AND `dispatch()` raises -- never silent.
- Permissions: `ai.generate`, `chat.send:discord`, `chat.send:twitch` (no flags.read, no kv).
- Logs PII-free: platform, op, input/output lengths, exception type only.

## Output safety

The model completion is untrusted text, so it passes through `waddle_sdk.safe_text.sanitize_user_text` last, before it enters the reply: a forged `{user:<token>}` placeholder is escaped and `@everyone`/`@here`/`<@id>` markup is defanged. No `allow_tokens` -- nothing in model output is a host-minted mention.

A reply that would start with a chat-command lead (`/`, `.`, `!` -- Twitch/IRC run `/` and `.`
as the bot's own commands; `!` re-triggers other bots) is prefixed with a visible speech-balloon
so a prompt-injected `/ban x` completion can never be relayed as a command. The lead check
ignores control and invisible characters (they are stripped or invisible on the wire).

## Testing

`python3 -m pytest bundles/python/tldr` -- MOCKED AI host. **Full e2e (real model, latency) needs Ollama; not yet done.**
