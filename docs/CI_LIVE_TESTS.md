# Live Credential CI Tests

Some correctness properties (a real Discord Gateway auth handshake, a real
outbound REST reply reaching a real channel) can only be proven against the
real Discord API -- no amount of `helm lint`/`helm template`/dummy-credential
kind runs can see them. `.github/workflows/live-discord-e2e.yml` is the one
place in this repo that talks to real Discord, and it does so with
deliberately narrow, gated access to a real secret.

## Why a separate bot

The live workflow uses its own Discord bot application and token
(`DISCORD_BOT_TOKEN_CI`) -- **never** the alpha environment's bot.

- **No duplicate replies.** If CI reused the alpha bot's token, every real
  alpha user's `!ping` would get a second `pong` from whatever ephemeral kind
  pod CI happened to be running, and vice versa.
- **IDENTIFY budget.** Discord caps a bot application to ~1000 gateway
  `IDENTIFY` calls per 24h, shared across every process holding that token.
  A CI run that reconnects/retries during a flaky pull would eat into alpha's
  budget and could get the *production* bot rate-limited.
- **Blast radius.** A CI-only token can be rotated, scoped to a throwaway
  test server, and revoked without touching anything user-facing.

Create the CI bot the same way as any Discord bot (Discord Developer Portal
→ New Application → Bot → reset token), invite it to a dedicated test guild
with `Send Messages`, `Manage Webhooks`, and `Manage Messages` on the test
channel (webhook creation + cleanup deletes need both).

## Environment status: provisioned

The `ci-live-credentials` GitHub Environment is provisioned (deployment
branch policy `release/*`) with:

- Secret: `DISCORD_BOT_TOKEN_CI` -- a dedicated CI bot token, never the
  alpha/production bot.
- Variables: `DISCORD_CI_APP_ID`, `DISCORD_CI_GUILD_ID` (the
  `waddles-test-ci` guild), `DISCORD_CI_CHANNEL_ID` (`#general`).
- CI bot permissions on the test channel: View Channels, Send Messages,
  Read Message History, Manage Messages (webhook create/delete for the
  round-trip sender identity, and cleanup deletes).

`DISCORD_CI_APP_ID` is recorded here as the environment's source of truth
for the CI bot's application identity (Developer Portal lookups, token
rotation); the workflow itself authenticates purely via the bot token and
does not need to reference the application ID directly.

## One-time setup (already applied; kept for re-provisioning / DR)

```bash
# 1. Environment with a deployment branch policy restricting it to release
#    branches -- workflow_dispatch from any other ref still requires the
#    required-reviewer approval configured on the environment.
gh api repos/penguintechinc/waddles/environments/ci-live-credentials -X PUT
gh api repos/penguintechinc/waddles/environments/ci-live-credentials/deployment-branch-policies \
  -X POST -f name='release/*'

# 2. Secret -- via stdin, never a CLI arg (never in shell history).
gh secret set DISCORD_BOT_TOKEN_CI --env ci-live-credentials --body -
# (paste the token, then Ctrl-D)

# 3. Variables -- not secret, but env-scoped alongside the token for a
#    single source of truth about which application/guild/channel the CI
#    bot lives in.
gh variable set DISCORD_CI_APP_ID --env ci-live-credentials --body '<application id>'
gh variable set DISCORD_CI_GUILD_ID --env ci-live-credentials --body '<guild id>'
gh variable set DISCORD_CI_CHANNEL_ID --env ci-live-credentials --body '<channel id>'
```

## Round-trip approach: ephemeral channel webhook as the "sender" identity

The pipeline needs a message from a **different** identity than the CI bot
to make a real round trip observable: `core/svc_ingest/receivers/
discord_gateway.py`'s `_is_self` filters messages authored by the gateway's
own `bot.user.id`, and `svc-ingest-rust` (the connector this workflow
actually exercises) has the equivalent filter one layer down -- the
`penguin_connector_discord::gateway::GatewaySession::next_chat_message`
call that `core/svc_ingest/src/normalize.rs::normalize_discord` consumes
already drops the bot's own messages before they ever reach Rust code
(see the doc comment there). Either way, a message the CI bot posts to
itself would never reach the pipeline, so a genuinely different identity
is required to observe a real round trip at all.

Two ways to get a second identity:

1. **A second bot application/token** -- clean, but requires the user to
   provision and rotate a *second* secret purely so it can post one test
   message.
2. **An ephemeral Discord channel webhook**, created and destroyed by the
   CI bot's own token (`POST/DELETE /channels/{id}/webhooks`, requires only
   `Manage Webhooks` on the existing CI bot) -- a webhook-authored message
   has its own distinct author id (the webhook's, not the bot's), so real
   Discord delivers it as a normal `MESSAGE_CREATE` from a genuinely
   different identity, with zero additional secrets.

**Chosen: (2), the ephemeral webhook.** No second secret for the user to
provision, no second bot to invite/maintain, and the round trip is still
fully real: a distinct identity posts `!ping` over the real Discord API, the
CI bot's live gateway connection receives it, `svc-process-rust`'s
`bot_process` handler produces `pong 🐧`, and `svc-action-rust` posts it back
over the real Discord REST API. The workflow creates the webhook, posts
through it, deletes the test messages, then deletes the webhook -- no
webhook or message survives the run.

## Assertions

| # | Check | Evidence |
|---|---|---|
| a | Live gateway auth succeeded | `svc-ingest-rust` logs `connected, awaiting session stability` (`discord.rs:143`) within `READY_TIMEOUT_SECONDS` (120s) -- unreachable with a dummy token |
| b | Real round trip, exactly once | Polls `GET /channels/{id}/messages?after=<ping_id>` for `pong 🐧`; zero within `PONG_TIMEOUT_SECONDS` (90s) fails, more than one fails ("a duplicate consumer is attached") |

Both counts are printed unconditionally -- a zero denominator is a hard
failure, never a silent skip.

## Secret hygiene

- Token never appears in `run:` template interpolation (`${{ secrets.* }}`)
  -- only ever assigned to a step's `env:` block and read as a shell
  variable, per `.github/workflows/live-discord-e2e.yml`.
- The Kubernetes Secret is created via `--from-file=DISCORD_BOT_TOKEN=/dev/stdin`
  piped from the env var, never `--from-literal` (which would put the token
  on the process's argv).
- The ephemeral webhook's own token is written to a `chmod 600` file under
  `/tmp`, never to `$GITHUB_ENV` (which would list it in the run's visible
  environment-variables dump) and never logged.
- `if: failure()` diagnostics dump pod/event state only -- no `kubectl get
  secret`, no `printenv`/`env` dumps.
- The job only runs under the `ci-live-credentials` environment, gated to
  `release/**` pushes (or an explicitly-approved `workflow_dispatch`) --
  never `pull_request`/`pull_request_target`, so a fork PR can never see the
  secret.

## Rotating the token

1. Discord Developer Portal → CI bot application → Bot → Reset Token.
2. `gh secret set DISCORD_BOT_TOKEN_CI --env ci-live-credentials --body -`
   with the new token.
3. No code change needed -- the workflow always reads the current secret
   value at run time.
