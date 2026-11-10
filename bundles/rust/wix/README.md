# waddles.core.service.wix -- Wix connector (Rust, `connector-tools`): inbound webhooks + outbound Events tools

Maps **host-verified** Wix app webhooks to normalized member / subscription / event records. The host
(`core/svc_ingest`, `Scheme::WixJwt`) owns the ingress route, the RS256 JWT verification against the connection's
app public key, freshness, instance binding and replay; this component never verifies and must not be fed an
unverified body. 
**Outbound (1.1.0):** `sender.build-request` shapes host-executed Wix Events V3 templates for the tools
`wix.event.{list,get,create,update,cancel}` (schemas: `bundle.yaml` `exported_tools`, fixture
`tests/fixtures/wix_event_tools.json`). Endpoints: `POST /events/v3/events/query`, `GET /events/v3/events/{id}`,
`POST /events/v3/events`, `PATCH /events/v3/events/{id}` (requires current `revision`), `POST /events/v3/events/{id}/cancel`
([Events V3 reference](https://dev.wix.com/docs/rest/business-solutions/events/event-management/events-v3/query-events); auth:
Wix app / API-key identity with the "Manage Events" permission, `Authorization` + `wix-site-id` headers per
[Wix authentication](https://dev.wix.com/docs/api-reference/articles/authentication/about-authentication)).
The guest NEVER holds the credential: the template carries `secret-refs` `Authorization -> wix.api_key`,
`wix-site-id -> wix.site_id`, which the host resolves from the community's Wix connection; a payload naming a site/URL/header is refused.
`booking.list` is refused (`Unsupported`): bookings embed participant contact PII the connector cannot project out.

Contract (events, payload keys, consumer rules, Wix doc citations, operator setup): `docs/connector-wix.md`.
Flag: PostHog `waddles.connector-wix` (default OFF); env baseline `CONNECTOR_WIX_ENABLED`.

```
POST /intake/webhook/<route_key>  --verify(host)-->  receiver.on-frame(JWT)  --> normalized-event
                                                       member id --identity.lookup--> UUID (unlinked: dropped)
```

Tests: `cargo test --locked` (mapping + hostile input); build: `cargo component build --release --locked`.
