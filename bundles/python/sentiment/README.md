# sentiment

`!sentiment <text>` -> a friendly mood reply, e.g. `Sentiment: positive (92% confident)`. Stateless.

- AI via `waddle_sdk.ai.generate` (`ai.generate`, DANGEROUS; `stage-next` world), `json=True` (host picks gemma e4b) returning `{sentiment, confidence, emoji}`; one call per command, in `transform()` only.
- The JSON is untrusted: parsed defensively (closed label set, confidence 0..1, emoji only if non-ASCII/short); malformed output replies "unreadable analysis" and fails loud in `dispatch()`.
- Activation-gated, no feature flag. Perms: `ai.generate`, `chat.send:{discord,twitch}`.
- Input capped at 400 chars; host rejects PII. Logs are PII-free (platform, op, sentiment label, input length only).
- Tests use a mocked AI host; full e2e needs Ollama: `python3 -m pytest` in this dir.

## Output safety

The model-supplied emoji is untrusted text (label and confidence are validated), so it passes through `waddle_sdk.safe_text.sanitize_user_text` last, before it enters the reply: a forged `{user:<token>}` placeholder is escaped and `@everyone`/`@here`/`<@id>` markup is defanged. No `allow_tokens` -- nothing in model output is a host-minted mention.
