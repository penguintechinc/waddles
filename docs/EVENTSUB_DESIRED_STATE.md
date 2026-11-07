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
| `POST /api/v1/internal/eventsub/reconcile` `{afterCommunityId?, limit?}` | `eventsub:reconcile` | sweep + work (`action` 1 create, 2 delete; `callbackKey`, never a secret) + `enabledTwitchSubscriptionIds` |
| `POST /api/v1/internal/eventsub/results` `{results:[{subscriptionId, outcome, twitchSubscriptionId?, failureReason?}]}` | `eventsub:reconcile` | outcome 1 created, 2 deleted, 3 failed, 4 revoked; atomic |
| `GET /api/v1/internal/eventsub/secret/<callback_key>` | `eventsub:secret-read` | webhook secret for the ingest verifier |

Page through communities while `sweep.nextCommunityId` is set. Responses are `no-store`.

## Secret handling (host-side only)

The webhook secret NEVER appears in the reconcile response and must never enter bundle (WASM guest)
memory. The Twitch bundle's create work item carries only `callbackUrl` + `callbackKey`. The HOST
(svc-ingest connector sender_exec, when building the Helix `POST /eventsub/subscriptions` request)
fetches the secret via `GET /api/v1/internal/eventsub/secret/<callback_key>` and injects it into
`transport.secret` of the request body as a host-filled slot. The S0 host-seam policy must define
that slot: name `transport.secret`, origin `eventsub_secret` (keyed by `callbackKey`). Grant
`eventsub:secret-read` ONLY to the svc-ingest identity, never the bundle identity.
