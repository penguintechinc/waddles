# Platform Credentials (`waddlebot-platform-credentials`)

## Why this Secret is separate

`templates/secrets.yaml` renders `waddlebot-secrets` on **every** `helm
upgrade`. Externally-issued platform/third-party credentials (Discord bot
token, Twitch OAuth token, Slack/YouTube/Spotify/Kick/Teams/Mattermost/
Google Chat OAuth values, AWS/GCP access keys, WaddleAI + PostHog API keys)
used to be rendered into that same Secret from `values.yaml` defaults. On
2026-09-29, a routine `helm upgrade` on alpha overwrote the real Discord bot
token with the `values-alpha.yaml` dev placeholder
(`REPLACE_ME_discord_bot_token_dev_only`), killing the live Discord gateway
session (4004 Authentication failed).

Fix: these credentials now live **only** in `waddlebot-platform-credentials`
(name configurable via `platformCredentials.existingSecret`), a Secret this
chart **never renders, writes, or reads via `lookup`**. Workload templates
reference it exclusively via `secretRef`/`secretKeyRef` with `optional: true`,
so an unconfigured platform is simply absent, never replaced by a
placeholder.

## Keys

| Platform | Keys |
|---|---|
| Discord | `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_BOT_TOKEN`, `DISCORD_APPLICATION_ID` |
| Twitch | `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_WEBHOOK_SECRET`, `TWITCH_OAUTH_TOKEN` |
| Slack | `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APP_TOKEN` |
| YouTube | `YOUTUBE_API_KEY`, `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` |
| Spotify | `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` |
| Kick | `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET`, `KICK_WEBHOOK_SECRET`, `KICK_ACCESS_TOKEN` |
| Teams | `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD` |
| Mattermost | `MATTERMOST_BOT_TOKEN`, `MATTERMOST_WEBHOOK_SECRET` |
| Google Chat | `GOOGLE_CHAT_SERVICE_ACCOUNT_KEY` |
| AWS | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_LAMBDA_ROLE_ARN` |
| GCP | `GCP_SERVICE_ACCOUNT_KEY`, `GCP_SERVICE_ACCOUNT_EMAIL` |
| WaddleAI | `WADDLEAI_API_KEY` |
| PostHog | `POSTHOG_API_KEY` |

Non-secret companion config for these platforms (URLs, region/project/tenant
IDs) is unaffected and still comes from `values.yaml`/`waddlebot-secrets` as
before.

## Alpha / local: operator setup

Create the Secret once, reading token values from files (never shell
history/argv):

```bash
kubectl create secret generic waddlebot-platform-credentials \
  --namespace waddlebot \
  --from-file=DISCORD_BOT_TOKEN=./discord-bot-token.txt \
  --from-file=TWITCH_OAUTH_TOKEN=./twitch-oauth-token.txt
  # add more --from-file=KEY=./file for any other platform you use
```

Update a single key later (rotation) without touching the others:

```bash
kubectl create secret generic waddlebot-platform-credentials \
  --namespace waddlebot \
  --from-file=DISCORD_BOT_TOKEN=./discord-bot-token.txt \
  --dry-run=client -o yaml | kubectl apply -f -
```

`scripts/alpha-deploy.sh` checks this Secret **exists** before every
`helm upgrade` (existence only -- it never reads its data) and prints the
command above and exits non-zero if it's missing.

## Beta / gamma / production

Populate via External Secrets Operator or Sealed Secrets, consistent with
this chart's existing `autoProvisionedKeys.<key>.externalSecret: true`
pattern (see `README.md` Auto-Provisioned Keys) -- point the controller at a
Secret named `waddlebot-platform-credentials` (or set
`platformCredentials.existingSecret` to whatever name your ExternalSecret/
SealedSecret controller creates).

## Upgrading an existing install

This chart's Secret template stopped writing the keys above; it does **not**
delete them from a pre-existing `waddlebot-secrets` Secret, so already-running
pods keep working through the upgrade. After the upgrade:

1. Create `waddlebot-platform-credentials` per the operator command above (or
   via your ExternalSecret/SealedSecret controller).
2. Restart the affected Deployments (`hub-api`, `svc-ingest`,
   `svc-ingest-rust`, `svc-action`, `svc-action-rust`) so pods pick up the new
   `envFrom`/`secretKeyRef` wiring to the new Secret.
3. Optionally clean up the now-unused platform-credential keys still sitting
   in the old `waddlebot-secrets` Secret (`kubectl edit secret
   waddlebot-secrets` -- not automated, since this chart never mutates that
   Secret's data for keys it no longer renders).
