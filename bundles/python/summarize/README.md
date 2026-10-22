# summarize (`waddles.core.example.summarize`)

`!summarize <text>` -> a concise summary of the supplied text. Example: `!summarize <a long paragraph>`.

AI bundle modelled on `translate` (stateless, no kv). **Activation-gated only -- no feature flag**
(installed -> enabled -> activated per community).

## AI pattern

| Item | Value |
|---|---|
| Permission | `ai.generate` (Dangerous, consent-ack, 10 calls / 60 s) -- justification in `hub-manifest.yaml` |
| SDK call | `await ai.generate(prompt, max_tokens=160, temperature=0.3, json=False)` in `transform()` only; `dispatch()` relays |
| World | `stage-next` |
| Model | Host-controlled (`json=False` -> gemma e2b); never named by the bundle |
| Guards | Host rejects PII (`contains_pii`) -> "remove personal info" reply; input capped at 1500 chars; completion collapsed to one <=450-char line |

## Behaviour

- Empty text or over-cap text -> usage reply, no AI call. One AI call per invocation.
- Any AI refusal/failure -> clear chat reply AND `dispatch()` raises -- never silent.
- Permissions: `ai.generate`, `chat.send:discord`, `chat.send:twitch` (no flags.read, no kv).
- Logs PII-free: platform, op, input/output lengths, exception type only.

## Testing

`python3 -m pytest bundles/python/summarize` -- MOCKED AI host. **Full e2e (real model, latency) needs Ollama; not yet done.**
