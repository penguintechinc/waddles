# Twitch EventSub desired state (control plane)

hub-api owns *which* EventSub subscriptions should exist; the Twitch platform bundle's reconcile
hook executes create/delete with an app token and reports back. svc-ingest only receives.

- Table `twitch_eventsub_subscriptions` (migration 0082): community x connection x type+version,
  int enum codes (`EventSubType` 1-7, `SubStatus`, `FailureReason`), per-subscription secret sealed
  AES-256-GCM (AAD `eventsub:<public_id>`, `CREDENTIAL_ENCRYPTION_KEY`), opaque `callback_key`.
- Desired = approved+enabled active Twitch channel connection x types matched by activated bundles'
  `consumes` rules on `twitch` (community-scoped or tenant-wide).
- Config: `TWITCH_EVENTSUB_CALLBACK_BASE_URL` (no default; unset -> 503). Callback =
  `<base>/eventsub/twitch/webhook/<callback_key>`.

## Internal contract (machine JWT / SVID, `system` tenant)

| Route | Scope | Purpose |
|---|---|---|
| `POST /api/v1/internal/eventsub/reconcile` `{afterCommunityId?, limit?}` | `eventsub:reconcile` | sweep + work (`action` 1 create w/ `secret`, 2 delete) + `enabledTwitchSubscriptionIds` |
| `POST /api/v1/internal/eventsub/results` `{results:[{subscriptionId, outcome, twitchSubscriptionId?, failureReason?}]}` | `eventsub:reconcile` | outcome 1 created, 2 deleted, 3 failed, 4 revoked; atomic |
| `GET /api/v1/internal/eventsub/secret/<callback_key>` | `eventsub:secret-read` | webhook secret for the ingest verifier |

Page through communities while `sweep.nextCommunityId` is set. Responses are `no-store`.
