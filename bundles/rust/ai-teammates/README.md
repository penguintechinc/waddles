# AI Teammates (`waddles.core.example.ai-teammates`)

Rust APP bundle on the `stage-next-tools` world. Three teammates, each **off by default** and
enabled/configured **per community**. All model access is through the AI service tools
(`ai.complete`, `ai.classify`, `ai.rag.query`), called from code -- the model never selects a
tool, and the bundle imports neither the raw `ai` host capability nor `http` (no egress).

```
chat / member.join / timer.tick / stream hooks
        |  process-stage (identity, kv, tool calls)
        v
   engine ── Researcher ─ ai.rag.query ─ ai.complete        (!research)
          ├─ Receptionist ─ welcome-once claim ─ [ai.complete]   (member.join, first message, !onboard)
          └─ Producer ─ <platform>.live.channels ─ ai.complete ─ blocklist ─ ai.classify
        |  one sanitized reply
        v
   action-stage: re-validate, relay.push to the event's own platform/channel
```

## Commands (shared verb grammar: `!<cmd> [sub-module] [verb] <input>`)

| Command | Who | Effect |
|---|---|---|
| `!research <question>` | anyone (Researcher on) | KB answer, general fallback if `allow_general` |
| `!onboard <question>` | anyone (Receptionist on) | KB-only answer from `onboarding_collection` |
| `!teammate [list]`, `!teammate <name>` | anyone | on/off + config validity |
| `!teammate enable\|disable <name>` (either order) | mod/broadcaster | runtime override |
| `!teammate set <name> <key> <value>` / `reset <name>` | mod/broadcaster | validated override; invalid values are NOT saved |
| `!producer pause\|resume\|status` | mod/broadcaster (status: anyone) | Producer kill switch |

`!ask` is intentionally not claimed: the Python `ask` bundle owns it until retired (flip is a
one-line manifest + `command.rs` change). Mod gate = strict-boolean `is_mod`/`is_broadcaster`;
absent or non-boolean badges deny (platforms that emit no badges, e.g. Discord today, change
config via the hub-managed config / admin actions instead).

## Config (hub-managed `config_schema`, overlaid by chat overrides in community kv)

Invalid config fails closed: that teammate is off and `!teammate <name>` says why. Producer hard
limits are enforced in code AND schema: interval floor 5 min, max 6 posts/hour.

## Safety model

* Chat is DATA: wrapped in `<user_input>`/`<retrieved_data>`/`<channel_context>` blocks with every
  angle bracket escaped, invisible/control chars stripped, length-capped; injection-shaped requests
  are refused without any tool call; poisoned retrieved chunks are dropped.
* Output: control chars, links, `<>`, mentions (`@everyone`, `<@id>`), `{user:..}` egress tokens are
  neutralised; a command lead gets a safe prefix; re-validated in the action stage.
* Receptionist never puts a name in a prompt or reply: greetings carry `{user:<uuid>}`.
* Producer never reads chat into prompts and never targets a user; any failure posts nothing.
* Logs: ids/op/counts/closed-vocabulary codes only; never user text.

## Consumed tool contracts (`contracts/*.json`, validated in every flow test)

Generated from the authoritative contracts: `ai.complete`, `ai.classify`, `ai.rag.query` from
`bundles/contracts/ai-tools.v1.json` (AI service #1109, consolidated in #1120), and
`<platform>.live.channels` from `live-channels-contract.v1.json` (#1119). Inputs are validated
against the producer bounds before every call and fail loud (never truncated). KB mode uses the
service's grounded `answer` + `citations` directly (an uncited or injection-shaped answer is
discarded); `ai.complete` is used only for the general fallback, greeting line and Producer.
Producer `channels[].channel_id` is the platform's `channel_ref`.

## Parity with the legacy features

| Legacy | Status |
|---|---|
| welcome: first-message welcome once per member (`welcome_service`) | covered (atomic kv claim) |
| welcome: template with `{username}` | covered (`{user}` -> egress placeholder) |
| welcome: AI personalization behind flag, template fallback | covered (`ai_personalize`, default off, degrades to template) |
| welcome: member join event | covered when the connector emits `member.join` (see PR "Needs") |
| ai chatter config (enabled / rate windows / probability) | NOT ported: random unsolicited replies to users are replaced by the Producer's channel-level, opt-in model |
| ai chatter proactive replies to specific users | dropped by design (prompt-injection + PII surface) |
| ai_researcher `/ask`, `/research` | covered (`!research`) |
| ai_researcher `/recall`, `/summarize`, `/context`, `/memory`, insights, anomalies, sentiment, bot-detection, profiles, firehose | NOT covered (analytics / memory; `ai.memory.*` is v3.1, analytics not user-facing chat) |
| ai_researcher game / patch / price / tech / build / event / clip lookups | NOT covered: they depend on arbitrary web search egress (SearXNG); needs an explicit egress allowlist decision, otherwise dropped |
| hub-api ai-knowledge sources/reindex/suggest/feedback | owned by the AI service `ai.rag.*` (not this bundle) |
| prompt_safety / ai_guard acceptance corpus | ported into `guard.rs` tests (delimiter breakout in case/spacing/width variants, role/instruction override, exfiltration, tool abuse) |

Nothing legacy is deleted by this bundle; deletion candidates after parity sign-off:
`action/interactive/welcome_interaction_module`, `core/ai_researcher_module` (once the NOT-covered rows are decided),
`ai_interaction_module/services/proactive_chat_service.py`, `hub_api/services/bot_ai_chatter.py`.

## Build / test

```
cargo test                                   # unit + contract + flows + manifest + real component build
cargo clippy --all-targets -- -D warnings
cargo component build --target wasm32-wasip2 --locked --release
```
