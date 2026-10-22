# translate (`waddles.core.example.translate`)

`!translate <target-language> <text>` -> an AI translation; the source language is auto-detected.
Example: `!translate spanish good morning` -> `[spanish] buenos dias`.

Waddles v2 port: the prompt ("professional translator ... return ONLY the translation") comes from
`action/interactive/translate_interaction_module/services/translation_providers/waddleai_provider.py`,
with the separate detect-language call folded into the one prompt (the host allows 10 `ai.generate`
calls / 60 s).

## AI-bundle pattern (reuse for other AI bundles)

| Item | Value |
|---|---|
| Permission id (hub-manifest V2) | `ai.generate` (Dangerous, consent-ack, 10 calls / 60 s) |
| SDK call | `from waddle_sdk import ai; await ai.generate(prompt, max_tokens=..., temperature=..., json=False)` |
| Where | **process stage only** (`transform()`); the action stage has no `ai` handler -- `dispatch()` just relays |
| World | `stage-next` (bundle must be built against it) |
| Model | Host-controlled, NOT nameable by a bundle: `json=False` -> `gemma4:e2b` (`WADDLEAI_MODEL`), `json=True` -> `gemma4:e4b` (`BUNDLE_AI_JSON_MODEL`). Translate uses plain text -> e2b |
| Host flag | `waddles.bundle-ai-capability` (default OFF; see `docs/reference/bundle-host-imports.md`, issue #858) |
| Guards | Host REJECTS prompts with PII/secrets (`contains_pii`) -- this bundle tells the caller; completion is untrusted, collapsed to one bounded line |
| Test double | `waddle_sdk.testing.install_fake_ai_host` (see `tests/conftest.py::host`) |

## Behaviour and limits

- Missing/invalid language or text -> usage reply. Text over 400 chars -> refused before any AI call.
- Any AI refusal/failure (PII, rate limit, not granted, model unreachable, empty completion) ->
  a clear chat reply AND `dispatch()` raises -- never a silent or canned translation.
- Permissions: `ai.generate`, `chat.send:discord`, `chat.send:twitch`. No kv (no cache).
- Logs are PII-free: platform, language code, input/output lengths and exception type only -- never
  the typed text, the translation, or the actor.

## Output safety

The model completion is untrusted text, so it passes through `waddle_sdk.safe_text.sanitize_user_text` last, before it enters the reply: a forged `{user:<token>}` placeholder is escaped and `@everyone`/`@here`/`<@id>` markup is defanged. No `allow_tokens` -- nothing in model output is a host-minted mention.

## Testing

`python3 -m pytest bundles/python/translate` -- runs against a MOCKED AI host. **Full end-to-end
(real model output, latency, the 8 s host timeout) needs the Ollama server and is not yet done.**
