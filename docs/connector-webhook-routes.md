# Connector webhook routes (svc-ingest)

Generic signed-webhook intake: `POST /intake/webhook/{route_key}`. Spec: `docs/superpowers/specs/2026-11-05-connector-runtime-inbound-manager.md` section 4.

## Activation

| Env | Default | Meaning |
|---|---|---|
| `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS` | empty | csv of platforms that register routes (`discord`, `wix`; `twitch` is supported but NEVER a default) |
| `CONNECTOR_ROUTE_KEY_SECRET` | none | >= 32 bytes; required when any platform is listed; env/secret only |

- Empty list: `AppState.webhook` stays `None`, the route answers 503 (fail loud).
- Listed platform with a missing secret or no Valkey: startup error naming the item.
- Boot: `webhook_boot::build_ingress` -> `AppState.webhook = Some(..)` BEFORE `http::router`. The runtime then calls `activate_route` per `TransportPlan::Webhook`.

## Route key

`base64url(HMAC-SHA256(CONNECTOR_ROUTE_KEY_SECRET, "wh:v1:{platform}:{connection_id}"))` -- 43 chars, no ids in the URL. `activate_route` refuses any key that is not this derivation. Logs carry only `route_fp` (first 8 hex of SHA-256 of the key).

**Rotation:** change the secret, restart svc-ingest, re-publish every provider callback URL. Old URLs answer 404.

## Discord interactions

- Ed25519, headers `X-Signature-Ed25519` / `X-Signature-Timestamp`, signed string `timestamp + body` (the one allowed no-separator template: digits-only timestamp, body must open with `{`).
- Public key = the connection's bearer secret, resolved per request via `connection_resolver`; never sent to the guest.
- Host-side responses (verified requests only): PING -> `{"type":1}`; command -> `{"type":5}`; component/modal -> `{"type":6}`; autocomplete -> type 8 with no choices; other types -> 400.
- 3 s deadline: the ingress runs detached and the handler waits 2.5 s; a stalled secret/dedup backend answers 503, never hangs.
- **Ownership routing:** followups/callbacks are refused (`interaction_not_owned`) until the host records the routed community. `ContextSink` (`core/svc_ingest/src/active_context.rs`) resolves the invoker's active community from hub-api (`communityId`, numeric; an answer without it is malformed and fails closed), verifies the stored interaction against the connection's own guild (`guild_mismatch`) and application id (`application_mismatch`), then calls `InteractionStore::set_routed`. Guild, application and community never come from the guest or payload. Any failure answers the user a generic ephemeral notice and publishes nothing. Telemetry: span `interaction.route`, log `interaction routed to community`, metrics `svc_ingest_interaction_routing_total{outcome}` and `svc_ingest_interaction_routing_duration_seconds{outcome}` (bounded outcome labels, no ids).
- Invalid signature answers 403 (uniform). Discord's endpoint-validation check expects 401 for a bad signature; if the dashboard refuses the URL, switch the Discord route's rejection to 401.

## Wix app webhooks

- Body is the raw RS256 JWT (no signature headers). Key = the connection's bearer secret (Wix app public key), per request via `connection_resolver`; the signed `instanceId` must equal the connection's `instance_id`. Freshness from `iat`/`exp`; replay key = Wix event id. Full contract and event mapping: `docs/connector-wix.md`.
- Requires `CONNECTOR_WIX_ENABLED=true` (selector) AND `wix` in `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS`; helm `connectorRuntime.connector.wix.enabled` + `webhookPlatforms: [wix]` + `pipeline.connectors.digests.wix`.
- Rejections stay uniform 403; the route answers 200 to Wix within its 1250 ms budget (verify + Valkey `SET NX PX`).

## Kick webhooks

Kick signs every delivery with ONE global RSA key (fetched from `api.kick.com/public/v1/public-key`, TTL-cached, rotation-aware), so a valid
signature proves "from Kick", not which community. The Kick profile therefore plans a second, `Lane::Webhook` connection per binding when
`kick` is listed in `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS`: route key = the host derivation for `(kick, connection_id)`, scheme =
`Scheme::kick(<broadcaster_user_id>)` from the binding metadata, activated with `webhook_boot::activate_route`. After the signature
verifies, `broadcaster.user_id` must equal the route's bound id. A binding without a valid numeric `broadcaster_user_id` is refused
loudly, and a broadcaster claimed by two communities drops both. Activation also subscribes the broadcaster to the bundle's events
(`kick_subscribe`), host-side with the community's own credential.

## Replay and abuse limits

`replay_window` 300 s (<= dedup TTL 900 s); Valkey dedup fails CLOSED (503, nothing delivered); 20 failed verifications per source per minute then 429; pre-auth budget before any secret fetch; 1 MiB body cap.
