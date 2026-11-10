# Wix webhook connector (`waddles.core.service.wix`)

Connector service bundle for Wix app webhooks (inbound) plus the outbound `wix.event.*` tools (1.1.0, see below). Generic: any community that installs a Wix app and
stores its credentials can use it. Flag: PostHog `waddles.connector-wix` (default OFF); env baseline
`CONNECTOR_WIX_ENABLED` (default OFF); route listing via `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS=wix`.
Route/ingress mechanics: `docs/connector-webhook-routes.md`.

## Wix wire format (official sources)

- Webhook = `POST` of a **raw JWT** (body is the compact JWT text, not JSON). Verify with the app **public key**
  from the app dashboard (Webhooks page / "View ID and Keys"). Retries: 12 attempts, 1250 ms timeout, duplicates
  and out-of-order delivery possible; answer 200 quickly.
  https://dev.wix.com/docs/build-apps/develop-your-app/api-integrations/events-and-webhooks/about-webhooks
  https://dev.wix.com/docs/build-apps/develop-your-app/develop-a-self-managed-app/webhooks/handle-events-with-webhooks-for-self-hosting-without-the-java-script-sdk
- Algorithm **RS256** (RSASSA-PKCS1-v1_5 + SHA-256): the Wix JS SDK verifies with `importSPKI(publicKey, 'RS256')`
  (`@wix/sdk` AppStrategy.decodeJWT). The docs' `jwt.verify(body, PUBLIC_KEY)` example uses a PEM RSA key.
- Envelope (three nested layers; `data` and inner `data` are JSON **strings**):
  `JWT payload {data, iat, exp}` -> `JSON.parse(data) = {instanceId, eventType, identity, data}` ->
  `JSON.parse(inner.data) = {id, entityFqdn, slug, entityId, eventTime, createdEvent|updatedEvent|deletedEvent|actionEvent}`
  where `createdEvent.entity`, `updatedEvent.currentEntity`, `actionEvent.body` are again JSON strings.
  https://dev.wix.com/docs/api-reference/articles/work-with-wix-apis/platform/about-the-structure-of-webhooks
  Event `id` = unique id for duplicate suppression (member-created reference:
  https://dev.wix.com/docs/api-reference/crm/members-contacts/members/members/member-created ).
- Event names (`eventType`): `wix.members.v1.member_{created,updated,deleted}`;
  `wix.pricing_plans.v2.order_{created,purchased,started,updated,ended,canceled,paused,resumed,marked_as_paid,...}`
  (order = `{_id, planId, subscriptionId, buyer{memberId,contactId}, status}`; status
  `DRAFT|PENDING|ACTIVE|PAUSED|ENDED|CANCELED`); `wix.events.v3.event_{created,updated,canceled,deleted,...}`.
  Source of names/shapes: `@wix/auto_sdk_members_members`, `@wix/auto_sdk_pricing-plans_orders`, `@wix/events` typings
  and https://dev.wix.com/docs/api-reference/business-solutions/pricing-plans/pricing-plans/orders/order-canceled
  https://dev.wix.com/docs/rest/business-solutions/events/introduction

## Host verification (svc-ingest `Scheme::WixJwt`)

Public key = the connection's bearer secret, resolved per request (PEM, base64 PEM or base64 SPKI DER; RSA >= 2048
bit). `alg` must be exactly `RS256` (`none`/`HS*`/anything else -> 403). `iat` (and `exp` when present) enforced against
the route replay window (300 s, 60 s future skew). `instanceId` must equal the connection's stored Wix instance id.
Replay: Valkey `SET NX PX` keyed on the Wix **event id**. 1 MiB body cap. All rejections are a uniform 403.

## Normalized events (contract for consumers)

Every event: `platform = "wix"`, `occurred_at` = RFC 3339 Wix `eventTime`. `actor` = member **UUID** from
`identity.lookup(platform-identity("wix", <wix member id>))`; no email/name ever. Payload keys below are
exhaustive; every payload also carries `event_id` (Wix event id), `instance_id` and `time_source` (`"wix"` when `occurred_at` is Wix's own `eventTime`, `"received"` for the receive-time fallback -- non-orderable, consumers must never compare it with Wix times). Unknown `eventType`s are skipped
(counted, never an error).

| event_type | Wix source | actor | payload (besides event_id, instance_id) |
|---|---|---|---|
| `member.linked` | `member_created` | member UUID | `{}` |
| `member.updated` | `member_updated` | member UUID | `{}` |
| `member.deleted` | `member_deleted` | member UUID | `{}` |
| `subscription.started` | `order_purchased`/`order_started`/`order_resumed` with status ACTIVE | member UUID | `{order_id, plan_id, status}` |
| `subscription.changed` | `order_updated`, `order_marked_as_paid` | member UUID | `{order_id, plan_id, status}` |
| `subscription.ended` | `order_ended`, `order_canceled`, `order_paused` | member UUID | `{order_id, plan_id, status, reason: "ended"\|"canceled"\|"paused"}` |
| `event.created` | `wix.events.v3.event_created` | none | `{wix_event_id, title, starts_at, ends_at, timezone, location, status}` |
| `event.updated` | `event_updated` | none | same |
| `event.canceled` | `event_canceled`, `event_deleted` | none | same (`status` is the Wix status or `"DELETED"`) |

Rules for consumers: `subscription.started` can fire more than once per order (purchase then start) -- upsert on
`(actor, plan_id)`. Delivery is at-least-once and unordered: compare `occurred_at`. A member-bound event whose Wix
member has no linked Waddles identity is **dropped and counted** (`skipped`), not emitted with a null actor.
`plan_id` / `wix_event_id` are Wix opaque ids; mapping plan -> role / event -> calendar entry is consumer config.
Not emitted (out of scope): RSVP/ticket/order-of-tickets events.

## Operator setup (Wix side)

1. Wix app dashboard -> Webhooks: subscribe Member Created/Updated/Deleted, Pricing Plans Order Purchased/Started/
   Updated/Ended/Canceled/Paused/Resumed, Events Event Created/Updated/Canceled; callback URL =
   `https://<svc-ingest host>/intake/webhook/<route_key>` (route key shown by hub-api for the connection).
2. Copy the app **public key** (app home -> "View ID and Keys") and the site **instance id** into the community's
   Wix provider connection (hub-api `provider_connections`, provider `wix`: secret = public key, metadata
   `instance_id`). Release a new app version so webhook changes take effect.

## Outbound tools (`wix.event.*`, 1.1.0)

| Tool | Wix call | Access / risk |
|---|---|---|
| `wix.event.list` | `POST /events/v3/events/query` | read / normal |
| `wix.event.get` | `GET /events/v3/events/{eventId}` | read / normal |
| `wix.event.create` | `POST /events/v3/events` | write / dangerous |
| `wix.event.update` | `PATCH /events/v3/events/{id}` (current `revision` required) | write / dangerous |
| `wix.event.cancel` | `POST /events/v3/events/{eventId}/cancel` | write / dangerous |

Docs: https://dev.wix.com/docs/rest/business-solutions/events/event-management/events-v3/{query-events,get-event,create-event,update-event,cancel-event}.
Create needs title + `startDate`/`endDate` + `timeZoneId`; `description` maps to `shortDescription` (<=500), `location_name` (<=50) and
`location_type` `VENUE|ONLINE`. Host duties (S0 seam, not in this bundle): execute the template against `www.wixapis.com`, filling
handles `wix.api_key` (Wix API key / app token) and `wix.site_id` from the community's Wix connection. The current `wix` provider
adapter stores only the app PUBLIC key + `instance_id` (inbound verification), so an API credential field must be added to the
connection before the tools can run. `booking.list` is intentionally not offered (participant PII in responses).
