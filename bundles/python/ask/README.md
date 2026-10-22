# ask (`waddles.core.example.ask`)

`!ask <question>` -> a concise (<=2 sentence), SFW-guarded AI answer. Same AI-bundle pattern as
`translate` (`ai.generate`, process stage only, `stage-next` world, host-picked model, one call per
command, `dispatch()` relays).

## Injection-resistant prompt

Fixed rules first (SFW, <=2 sentences, reply exactly `REFUSE` for unsafe/jailbreak/role-change),
then the user text inside a single `<question>...</question>` block declared UNTRUSTED DATA, then
the rules restated. `<question>` tags in user text are stripped so the block cannot be closed or
reopened. `REFUSE` -> polite canned reply. Completion is collapsed to one line (<=450 chars).

## Contract

- Activation-gated (installed -> enabled -> activated); **no feature-flag guard**.
- Permissions: `ai.generate` (Dangerous), `chat.send:discord`, `chat.send:twitch`. No kv.
- Input cap 300 chars (refused before any AI call). PII prompts rejected by host -> "remove
  personal info" reply. Any AI failure -> chat reply AND `dispatch()` raises.
- Logs: platform, op, reason, lengths only -- never question, answer or actor.

## Output safety

The model completion is untrusted text, so it passes through `waddle_sdk.safe_text.sanitize_user_text` last, before it enters the reply: a forged `{user:<token>}` placeholder is escaped and `@everyone`/`@here`/`<@id>` markup is defanged. No `allow_tokens` -- nothing in model output is a host-minted mention.

## Testing

`python3 -m pytest bundles/python/ask` -- MOCKED AI host. Full e2e (real model) needs Ollama; not done.
