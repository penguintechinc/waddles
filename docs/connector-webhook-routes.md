# Connector webhook routes (svc-ingest)

Generic signed-webhook intake: `POST /intake/webhook/{route_key}`. Spec: `docs/superpowers/specs/2026-11-05-connector-runtime-inbound-manager.md` section 4.

## Activation

| Env | Default | Meaning |
|---|---|---|
| `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS` | empty | csv of platforms that register routes (`discord`; `twitch` is supported but NEVER a default) |
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
- Invalid signature answers 403 (uniform). Discord's endpoint-validation check expects 401 for a bad signature; if the dashboard refuses the URL, switch the Discord route's rejection to 401.

## Replay and abuse limits

`replay_window` 300 s (<= dedup TTL 900 s); Valkey dedup fails CLOSED (503, nothing delivered); 20 failed verifications per source per minute then 429; pre-auth budget before any secret fetch; 1 MiB body cap.
