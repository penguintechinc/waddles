# waddles.core.service.wix -- Wix webhook connector (Rust, `connector-tools`, inbound-only)

Maps **host-verified** Wix app webhooks to normalized member / subscription / event records. The host
(`core/svc_ingest`, `Scheme::WixJwt`) owns the ingress route, the RS256 JWT verification against the connection's
app public key, freshness, instance binding and replay; this component never verifies and must not be fed an
unverified body. No egress, no sender (`build-request` answers `Unsupported`), no tools.

Contract (events, payload keys, consumer rules, Wix doc citations, operator setup): `docs/connector-wix.md`.
Flag: PostHog `waddles.connector-wix` (default OFF); env baseline `CONNECTOR_WIX_ENABLED`.

```
POST /intake/webhook/<route_key>  --verify(host)-->  receiver.on-frame(JWT)  --> normalized-event
                                                       member id --identity.lookup--> UUID (unlinked: dropped)
```

Tests: `cargo test --locked` (mapping + hostile input); build: `cargo component build --release --locked`.
