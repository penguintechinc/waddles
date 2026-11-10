# Discord platform connector (`waddles.core.service.discord`)

P6b Task 2. A core `kind: service` WASM component on `world connector-tools` that supplies **only the
Discord wire spec** on top of the generic host transports. It is **not** the Discord app bundle
(`bundles/python/discord`, `waddles.core.example.discord`).

Cutover: PostHog flag `waddles.connector-discord`, **default OFF**. While OFF the hardcoded path
(`core/svc_ingest/src/ingest/discord.rs`, `discord_identify_budget.rs`, `discord_rest.rs`) runs unchanged
and nothing here is exercised. The hardcoded code is deleted only in P6b T4.

```
 Discord Gateway --wss--> host WS transport (WsConfig, heartbeat timer, ACK timeout, reconnect, IDENTIFY budget)
                              |  raw frame            ^ handshake bytes (+ token substituted by HOST)
                              v                       |
 Discord interactions --POST--> host webhook route --Ed25519 (webhook_verify.rs) BEFORE the guest
                              |  verified body
                              v
        +---------------- connector component (this crate) ----------------+
        | receiver.on-connect / on-frame / on-heartbeat-due / on-disconnect |
        | sender.build-request     tool-provider (classify_frame, classify_close_code) |
        +-------------------------------------------------------------------+
                              |  normalized-event            | http-request-tpl (host pins target, injects credential)
                              v                              v
                       publish_event / spine         host outbound (T6d)
```

## Behavior inventory (what the hardcoded path does, and where it lives now)

Source of truth read in full: `ingest/discord.rs` (supervisor, permissions resolver, reconnect loop),
`ingest/discord_identify_budget.rs` (budget + session store), `discord_rest.rs` (outbound), and the pinned
`penguin-connector-discord` crate (`gateway.rs`, rev `ecf906a`) the supervisor drives.

| Concern | Hardcoded path | Connector / host split |
|---|---|---|
| Opcodes handled | 0 DISPATCH, 1 HEARTBEAT (answer immediately), 2 IDENTIFY (send), 6 RESUME (send), 7 RECONNECT, 9 INVALID_SESSION (`d` = resumable flag, never collapsed), 10 HELLO, 11 HEARTBEAT_ACK | `gateway.rs` builds IDENTIFY/RESUME/HEARTBEAT and `classify`es 1/7/9/10/11/READY/RESUMED. Acting on them is the host's job (see Host duties) |
| Intents | `GUILDS \| GUILD_MESSAGES \| MESSAGE_CONTENT` = 33281, nothing else | `GUILDS \| GUILD_MEMBERS \| GUILD_MESSAGES \| MESSAGE_CONTENT` = 33283 (`DEFAULT_INTENTS`) -- **1.0.3 adds the PRIVILEGED `GUILD_MEMBERS` intent** for inbound role sync; enable **Server Members Intent** in the Discord developer portal (Bot tab) or the gateway closes 4014 and the connection parks |
| IDENTIFY payload | `properties {os: linux, browser/device: penguin-connector-discord}` | identical bytes minus the token: `"token":"{{DISCORD_BOT_TOKEN}}"` slot + secret-ref handle `discord-bot` |
| RESUME | `session_id` + `seq` from `READY`, reconnect to `resume_gateway_url`; RESUME-first, exempt from the IDENTIFY budget; `resumable:true` INVALID_SESSION retries RESUME once, else clear + IDENTIFY | RESUME body built when the host session-token names a `session_id`; persistence/ordering/retry stay host-side |
| Heartbeat | timer on `heartbeat_interval`, body `{op:1,d:seq}`, `awaiting_ack` -> `HeartbeatAckTimeout` -> reconnect | `on-heartbeat-due` returns the `seq` body when the token carries `seq`, else `none` (host default `d:null`). Timer + ACK-timeout = host (P6a T5 scheduler) |
| Session state | `session_id`/`seq`/`resume_gateway_url`/`self_user_id` in `GatewaySession`, mirrored to Valkey `SessionStore` | A component holds NO state between calls -> state is the host-owned `session-token` JSON (`session.rs`) |
| Dispatches | **`MESSAGE_CREATE` yields `message`**; 1.0.3 adds `GUILD_MEMBER_UPDATE` -> `guild.member.update` (actor = tokenized UUID; payload `guild_id`/`user_id`/`role_ids`) and `GUILD_ROLE_CREATE\|UPDATE\|DELETE` -> `guild.role.*` (ids only); `READY`/`RESUMED`/`GUILD_CREATE` are logged and dropped; everything else ignored | same. Added (no hardcoded equivalent): `INTERACTION_CREATE` + webhook interactions |
| MESSAGE_CREATE | drop if `author.id == self_user_id` (by id, other bots kept); required `author.id`, `channel_id`, lenient `username/id/content` (empty default); `guild_id` optional (DM) | `normalize::normalize_message_create`, same rules |
| Permissions | per guild message: `GET /guilds/{g}` + `GET /guilds/{g}/members/{u}` -> `compute_discord_permissions` (owner / ADMINISTRATOR -> broadcaster; MANAGE_MESSAGES -> mod); DM: no lookup; any failure -> `false/false` (fail closed) | `perms.rs` (same math), lookups via guarded guest `http` w/ secret-ref; failure -> fail closed + PII-free WARN |
| Permission cache / breaker | 60s TTL, 1h stale cap (stale never elevates), 10k entries, 5-failure breaker | **Not reproducible in a stateless component.** Cost: up to 2 REST calls per guild message. See Host duties |
| Event shape | `event_type: message`, `actor = username`, payload `{text, guild_id, channel_id, message_id, author_id, is_mod, is_broadcaster}`, `source{account_id: guild_id or "dm", channel_id}` | identical event_type/payload (parity 10/10); `actor` is the tokenized UUID (`identity.lookup`), not the raw handle. `source` is stamped by the host sink from `ctx.source_id` (see Known gaps) |
| Close codes | 4004,4010-4014 Fatal (stop, mark unhealthy); 4000-4003,4005,4008 Resumable; 4007,4009 + undocumented ReconnectFresh | `classify_close_code` (tool + `gateway.rs`) identical; host acts |
| Reconnect backoff | `Backoff` + `STABILITY_WINDOW` (reset only after a stable session) | host `ConnectorDriver` |
| Identify budget / sharding | Valkey token bucket per `sha256(token)`, `max_concurrency` buckets (`shard_id % max_concurrency`), 5s window, rolling 24h `session_start_limit` + 20% low-headroom alert, fail-open on store outage | **HOST ONLY** — a stateless guest cannot and must not enforce a fleet-wide budget. Not reimplemented |
| Outbound | `discord_rest.rs`: `chat.send`, `chat.delete` (channel-scoped), `dm.send` (membership-gated, two calls), `allowed_mentions {"parse":["users"]}` on every message POST, 429 `Retry-After` handling | `sender.rs` templates (below); rate-limit handling = host |

### Host-only concerns (an connector cannot enforce these — documented, not reimplemented)

* **Interaction 3-second deadline.** Discord requires a response to a webhook interaction within 3 s.
  The connector only normalizes; the host route must answer type-1 PING with `{"type":1}` itself and ack
  commands within the deadline (deferred response) independent of `on-frame` latency.
* **Ed25519 verification.** Done by the host (`webhook_verify.rs` `Ed25519Spec`; headers
  `X-Signature-Ed25519` / `X-Signature-Timestamp`, signed string `timestamp + body`, hex, the app public
  key as the route's secret-ref). A forged interaction is rejected with a uniform 403 **before** `on-frame`; the
  connector never sees unverified traffic and never holds the key.
* **IDENTIFY budget / `session_start_limit` / shard buckets / fail-open policy** — see table.
* **Heartbeat timer and ACK-timeout reconnect** -- built host-side (`SessionRules.heartbeat_ack`, metric `svc_ingest_transport_heartbeat_ack_timeout_total`); see `docs/connector-host-capability.md` "Gateway gaps closed".

## Host duties required to switch the flag ON (integrator checklist)

1. **WsConfig** for `platform: discord`, url `wss://gateway.discord.gg/?v=10&encoding=json`,
   `handles {"discord-bot" -> <bot-token connection>}`, **`slot_prefixes: [ "\"token\":\"" ]`**
   (`TOKEN_PREFIX`), `egress_allow` empty. The credential slot is `{{DISCORD_BOT_TOKEN}}`.
1b. **Gateway rules (host).** Add `reconnect_url` (READY `/d/resume_gateway_url`, `allowed_hosts: ["*.discord.gg"]`,
   `query: "v=10&encoding=json"`), `heartbeat_ack` (op 11, `max_unacked: 1`), `handshake_after: FirstFrame` (HELLO before
   IDENTIFY/RESUME) and `jitter_first_heartbeat` to the session rules -- full example in `docs/connector-host-capability.md`.
   The host (not the connector) captures and validates the resume URL; the connector's `resume_gateway_url` session-token field is
   no longer needed for dialling. **Intents** are `33283` (1.0.3: adds privileged `GUILD_MEMBERS`; portal toggle required);
   **sharding** is single-shard (no `shard` field), identify buckets / `session_start_limit` remain host-only.
2. **Session token.** `ctx.session-token` MUST be the JSON in `session.rs` (`{"session_id","seq",
   "resume_gateway_url","self_user_id"}`, all optional), maintained by the host from the frames
   (`tool classify_frame` returns the `READY` session; `seq` is the frame's `s`). A non-JSON token (e.g. the
   static `WsConfig.resume_token`) fails `on-connect` **loudly** rather than burning an IDENTIFY.
   **Without `self_user_id` the self-message filter cannot run** (the hardcoded path reads it from `READY`)
   and the bot would ingest its own messages — the host MUST populate it before enabling the flag.
3. **Lifecycle actions.** Op 7 / op 9 / close codes are not signals the guest can raise (`frame-error` keeps
   the connection up). The host transport must call `classify_frame` / `classify_close_code` (or inspect
   op/code itself) to reconnect, RESUME vs IDENTIFY, and park on Fatal.
4. **HTTP bridge for the guest.** `perms.rs` needs `waddle:bundle/http` (egress `discord.com` GET,
   secret-ref slot `Authorization` = handle `discord-bot`, the stored value being the full `Bot <token>`
   header). Pinned connectors currently have no host-call bridge: the lookup then fails **closed**
   (`is_mod`/`is_broadcaster` = false, WARN, verified through the real engine) — safe but degraded.
   Consider host-side permission resolution + cache to restore the 60s TTL/breaker behavior.
5. **Webhook route** for interactions with `Ed25519Spec` (above) and the 3 s answer path.
6. **Outbound (T6d) — exact placeholder set and host responsibilities.** The connector emits ONLY these
   host-filled placeholders; the host MUST substitute each from the source stated, and REJECT (fail the
   action) any template that still contains an unresolved `{...}` placeholder or any value from another source:

   | placeholder | filled by the host from | never from |
   |---|---|---|
   | `{target.channel_id}` | the binding resolved via `target-source-id` (`app_outbound_bindings`) | payload |
   | `{target.guild_id}` | the same binding | payload |
   | `{target.role.<key>}` | the binding's **managed-roles allowlist** entry for `<key>` | payload / a raw snowflake |
   | `{user:<uuid>}` | the host's **community-scoped tokenization** (UUID -> platform user id) | a raw snowflake |
   | `{dm.channel_id}` | the `id` in Discord's reply to the `POST /users/@me/channels` the HOST just ran for this very operation's recipient | any other channel; the host rejects a DM post to a channel it did not open itself |

   Also required of the host: inject `Authorization` from the `discord-bot` handle; for `dm.send` read and
   **strip** the pseudo-header `X-Waddle-Host-Dm-Recipient` (the `{user:<uuid>}` recipient; never sent to
   Discord), open the DM channel (`POST /users/@me/channels {"recipient_id": <resolved>}`), then run the
   message request against the channel from that reply; enforce the **`send_dm_in_community` gate** BEFORE
   executing (the recipient must be a member of the guild of the triggering channel — the connector cannot);
   **refuse any managed role carrying `ADMINISTRATOR`, `MANAGE_ROLES` or `MANAGE_GUILD`** even if allowlisted;
   honour `429` `Retry-After`.
7. **Actor tokenization needs `connector.pii.read`.** `on-frame` calls
   `identity.lookup(platform-identity("discord", author_id))` and emits the UUID as `actor` (never the raw
   handle; no raw-handle fallback — a failed lookup fails the frame). `connector.pii.read` is declared in
   `hub-manifest.yaml` (catalog id: dangerous, core-only); the core seeder pre-grants it (P6b integration).

`ConnectorHost::load` REFUSES this component unless `connector.pii.read` is granted (it imports `identity`).

Shared parity harness (`chore/connector-parity-harness`, `make test-connector-parity PLATFORM=discord`):
`examined=10 passed=10 failed=0 identity_lookups=9`, with 3 vectors carrying declared divergences
(`tests/data_plane_parity/expected/discord/divergences.json`: `is_mod`/`is_broadcaster` fail closed because the
harness does not bridge the guest `http` lookup).

## Sender (`build-request`) — part B

| kind | request | payload (allow-list; anything else is rejected) |
|---|---|---|
| `chat.send` | `POST /channels/{target.channel_id}/messages` | `{content}` (1..2000 chars) |
| `chat.delete` | `DELETE /channels/{target.channel_id}/messages/{message_id}` | `{message_id}` (snowflake) — channel-scoped, never from the id alone |
| `dm.send` | `POST /channels/{dm.channel_id}/messages` (host opens the DM first) | `{recipient_id, content}` — `recipient_id` ONLY a `{user:<uuid>}` placeholder (raw snowflake refused); recipient rides in pseudo-header `X-Waddle-Host-Dm-Recipient` |
| `role.add` / `role.remove` | `PUT` / `DELETE /guilds/{target.guild_id}/members/{user:<uuid>}/roles/{target.role.<key>}` | `{user_id, role}` — `user_id` ONLY a `{user:<uuid>}` placeholder; `role` is a managed-role KEY `[a-z][a-z0-9_-]{0,31}`, never a snowflake |
| `event.create` / `event.update` / `event.cancel` / `event.delete` | `POST /guilds/{target.guild_id}/scheduled-events`, `PATCH .../{id}` (full field set), `PATCH .../{id}` `{"status":4}` (history kept), `DELETE .../{id}` | `{name, description?, scheduled_start_time, scheduled_end_time, location}` (+ `event_id` snowflake for update/cancel/delete). Fixed `EXTERNAL` / guild-only body. A `guild_id` / `channel_id` / unknown field is REFUSED; the guild is always the host's `{target.guild_id}` binding slot |

**Scheduled-event tools (host-mediated).** The service bundle declares `discord.scheduled_event.create|update|cancel|delete`
(`access: write`, `risk: dangerous`) in `exported_tools`; the guest only validates arguments through the same template
builder. The HOST executes them (svc-action `tool.invoke` -> signed `event.*` outbound entry -> svc-ingest executor ->
this connector's template; guild + credential from the community binding). App bundles call them with a manifest
`requested_tools: [{tool: discord.scheduled_event.create, access: write, target_app_id: waddles.core.service.discord}]`
and the approved grant `tool.call:waddles.core.service.discord:discord.scheduled_event.create` (`params: {write: true}`).


* The connector never holds a token (`secret-refs: [("Authorization","discord-bot")]`) and never chooses the
  destination: a payload naming `channel_id`/`guild_id` is refused. Every message body carries
  `allowed_mentions {"parse":["users"]}` (`@everyone`/`@here`/role pings never notify).
* There is no `dm.post` kind and no payload field can name a DM channel, role snowflake, recipient snowflake,
  channel or guild: the host pins/derives every destination and privilege (see the placeholder table above).
* `{user:<uuid>}` placeholders are passed through for the host to resolve (community-scoped tokenization).
* Status: executed by the host once T6d lands; the executor's `ConnectorHost` does not yet expose the
  sender export, so `build-request` is covered by native unit tests (and its presence is checked at load).

## Moderation + message ops (1.0.7)

Kinds `mod.kick|ban|unban|timeout` (`moderation.rs`) and `message.edit|reaction.add` (`message_ops.rs`);
tools `discord.moderation.{kick,ban,unban,timeout,untimeout}` (manifest `required_scope: community:moderate`,
host-verified actor) and `discord.message.{edit,react,delete}`. The target is ALWAYS a `{user:<uuid>}` token (a raw
snowflake is refused: PII rule); the guild is the host binding. The host duty hook
(`discord_duties.rs::mod_gate`) additionally enforces, from live guild state: bot outranks target (strictly), target
is not the owner/bot, timeouts never hit administrators, the bot holds KICK/BAN/MODERATE_MEMBERS; every refusal is a
fixed reason code and one PII-free audit event (`target: "audit"`) + `svc_ingest_discord_moderation_total` is
emitted per authorized action. Timeouts: the host converts `duration_seconds` (1..=28d) to the absolute
`communication_disabled_until` from its own clock; `clear` lifts it. Bans: `delete_message_seconds` 0..=604800.

### Manifest-declared host tools

Every `discord.*` tool (events, roles, moderation, message ops, embed, webhook) is a manifest `host_op`
declaration in `bundle.yaml` / `hub-manifest.yaml`; the guest serves only `classify_frame` /
`classify_close_code` and refuses every other name. The host planner (svc_action `host_tools.rs`) holds no per-tool
table. Core/host rules the declarations rely on:

* Moderation ops use `user_wire: placeholder` (the user rides as `{user:<uuid>}`, resolved by svc_ingest's hierarchy gate); the planner
  still detokenizes the target host-side for the protected-target check on kick/ban/timeout.
* `discord.moderation.untimeout` requires the literal `clear: true` (no constant mapping exists in `host_op`;
  `mod_payload` refuses anything else).
* `discord.message.embed` `{content?, embeds}` rides as the JSON wire text (`text_object`); the guest builder
  (`message_ops.rs`) allows only title/description/color/timestamp/footer.text/fields (no url/image/author), 1..=10 embeds,
  <=6000 chars. Needs `chat.send:discord`.
* `discord.webhook.create` `{name}` needs `webhook.manage:discord` + `community:moderate`; only the new webhook `id` is returned,
  the token never leaves the host.
* `core/svc_action/tests/fixtures/host_tools/discord.json` is generated by `scripts/host_tool_summary.py bundles/rust/discord --write ...`;
  a hub_api test fails on drift and `host_tools_discord_manifest_tests.rs` drives the real declarations through the planner.
## Slash commands and interactions (S2b)

| piece | where | contract |
|---|---|---|
| Registration planner | `commands.rs` `plan` | Parses hub-api `GET .../app-commands/desired` (`version`, `changed`, `commands[{name,description}]`). `changed:false` or a version already registered = no write; otherwise ONE `{"commands":[...]}` payload. Names `[a-z0-9_-]{1,32}`, descriptions 1-100 chars, <=100 commands, no duplicates; each command is re-derived from name+description plus the fixed free-text `args` option, so no permissions/nested options survive |
| `commands.register.guild` template | `commands.rs` `build` | `PUT /applications/{app.application_id}/guilds/{target.guild_id}/commands`; app id + guild are host slots, never payload. Global `commands.register` is refused (per-guild only) |
| Interaction replies | `interaction.rs` `build` | `interaction.callback` (types 4/5/6/7) and `interaction.followup`; URL slots `{interaction.id}`/`{interaction.application_id}`/`{interaction.token}` are filled by the HOST from its token store; no `Authorization`; `ephemeral` = flag 64 |
| Interaction event | `normalize.rs` | `interaction.command|component|autocomplete|modal` with `trigger:"webhook"`, synthesized `text` (`/verb args`), `args`, `custom_id`, `values`, modal `fields`; the token is never in the event |

**Security (reconciler only).** `commands.register.guild` is a bulk overwrite of the whole guild's shared command
set, so it is NOT a bundle-callable relay op (absent from `platform_ops::RELAY_OPS`). Only the host reconciler
(`svc_ingest::commands_reconcile`) issues it, and the executor's duty gate re-verifies that the submitted set is EXACTLY
hub-api's desired state for the binding's guild (`commands_mismatch` otherwise). Every overwrite is audit-logged
(guild, version, count).

**Host-held interaction token.** `svc_ingest::interaction_store` stores `(application_id, token, guild_id)` in Valkey
keyed by interaction id (TTL 15 min = token lifetime, `SET NX` = idempotency). Bundles hold only the interaction id.
The `interaction.*` duty refuses an unknown/expired handle, a DM, or another community's guild. The gateway door
(`interaction_tap::GatewayTap`) stores, dedupes and posts the deferred ack within 3 s before the connector runs; the
webhook door stores the signature-verified body (503 if it cannot).

## Credential-slot smuggling guard (`sanitize.rs`)

Every string that reaches a template (message `content`, `target-source-id`, user refs) is REJECTED — never
stripped or normalized — if it holds a control character (`\n`/`\t` allowed in `content` only), a zero-width /
bidi / BOM / soft-hyphen character, or a brace lookalike (fullwidth, ornamental, math); then `{{` / `}}` are
rejected on both the raw string and a whitespace-collapsed copy. This closes the strip-then-check bypass
(`{<ctrl>{SECRET}}`). Regression + mutation-checked tests: `sanitize.rs`, `sender.rs`.

## Tools take no host-pinnable ids

`classify_frame` / `classify_close_code` take only a gateway frame / close code (no guild/channel/user ids,
no network, no credential). Anything that needs a guild/channel/user is a `sender` action whose destination is
a host-pinned `{target.*}` slot with the `discord-bot` handle, all listed in "Host duties" above (T6d).

## Normalized events

* `message` — identical to the hardcoded event (see table; `tests/parity.rs`, 10/10 corpus vectors).
* NEW `interaction.command` / `.component` / `.autocomplete` / `.modal` (types 2/3/4/5):
  payload `{interaction_id, guild_id, channel_id, author_id, command, custom_id, options, values,
  is_mod, is_broadcaster}`; permissions from the interaction's own resolved `member.permissions`
  (no REST). The interaction **token is never copied**. PING yields no event.

## Known gaps vs the hardcoded path (not silent — each is called out for the integrator)

1. `source`: the host sink (`connector/sink.rs`) stamps `account_id = ctx.source_id`, `channel_id = None`;
   the hardcoded path derives `dg-{guild_id}` / `dg-dm` and `channel_id` **per message**. The data needed is
   in the payload (parity test proves it is derivable); the host must derive the source per event (or one
   connection per guild) before the flag is turned on, or workstream routing changes.
2. Permission cache/breaker (see table).
3. No guest-initiated reconnect / session persistence (connector contract limit).

## Secrets

None in this repo. Token = host connection resolved by opaque handle `discord-bot`; interactions public key =
the webhook route's secret-ref (host-side). Per-tenant bot credentials
(`docs/superpowers/specs/2026-11-02-per-tenant-bot-credentials.md`) are a host/connection-resolver concern:
the connector is credential-agnostic and one component serves every tenant; the host binds the handle to the
tenant's connection (`TransportSecrets::new(resolver, community_id)`), never to a tenant named by the guest.

## Logging / keys

Logs carry op/class/mode/gateway-op only — never usernames, content, ids, tokens (tests assert it). The connector
uses no kv. Tenant/community identity is never read by the guest.

## Build / test

```bash
cargo test --locked                                  # 40 unit + parity (10/10) + resume-sequence
cargo clippy --all-targets --locked -- -D warnings && cargo fmt --check && cargo deny check
cargo llvm-cov --locked --summary-only               # >= 90% lines
cargo component build --release --target wasm32-wasip2 --locked
# real engine path (production linker, compiled component):
cargo test --manifest-path ../../../core/bundle_executor/Cargo.toml --test discord_connector
```

Rebuild the executor fixture after any change here:
`cp target/wasm32-wasip2/release/waddle_bundle_discord_overlay.wasm ../../../core/bundle_executor/tests/fixtures/discord_connector.wasm`.
