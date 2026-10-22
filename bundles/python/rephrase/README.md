# rephrase

`!rephrase <text>` -> the text reworded more clearly and politely (SFW). Stateless.

- AI via `waddle_sdk.ai.generate` (`ai.generate`, DANGEROUS; `stage-next` world), `json=False` (host picks gemma e2b), called in `transform()` only; one call per command.
- Activation-gated, no feature flag. Perms: `ai.generate`, `chat.send:{discord,twitch}`.
- Input capped at 400 chars; host rejects PII ("remove personal info" reply); every AI error replies clearly and fails loud in `dispatch()`.
- Logs are PII-free (platform, op, lengths only).
- Tests use a mocked AI host (`waddle_sdk.testing.install_fake_ai_host`); full e2e needs Ollama: `python3 -m pytest` in this dir.
