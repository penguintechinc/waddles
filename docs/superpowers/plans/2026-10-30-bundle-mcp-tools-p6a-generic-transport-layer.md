# Bundle MCP-Tools P6a — Generic Dataplane Transport Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (standing default). Steps use checkbox (`- [ ]`).
> **DEPENDS ON:** P3b merged (the `stage-tools` world / tool framework, for Task 4). **COORDINATE WITH** the edge-reshape program (svc_ingest → svc-gateway rename) — see Global Constraints before scheduling.

**Goal:** Generalize the dataplane connector/transport layer into **platform-agnostic primitives** — generic registered-webhook HTTP ingress, generic WebSocket + poller transports, scheme-parameterized signature verification, host-pinned outbound, and secret resolution via `connection_resolver` — so per-platform connectors (P6b) carry only the platform spec, never dataplane code or credentials.

**Architecture:** The `Transport` trait (`svc_ingest/src/connector/transport.rs`) is the existing seam; today it has only test impls. This plan adds real generic impls + a generic webhook route registry + a scheme-parameterized verifier, all host-side, with secrets resolved per-call via `connection_resolver` (`SecretString`, never cached, never in guest memory/logs/queues). The guest/connector receives verified/normalized frames via `on-frame` and sends via host-pinned outbound — it never holds a credential or names a raw target.

**Tech Stack:** Rust (svc_ingest/gateway), `connector/transport.rs` trait, `core/connection_resolver`, `core/bundle_host_http` egress (reuse), `ReplayGuard`, tungstenite (WS), axum (webhook ingress), ed25519/hmac deps.

**Spec:** `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-framework.md` §10/§10a (P6a). Related: `edge-reshape-svc-gateway-dispatcher`, `provider-connection-framework`.

## Global Constraints

- **EDGE-RESHAPE COORDINATION (sequencing risk).** `svc_ingest` is being renamed/reshaped into `svc-gateway` (stateful edge). Building new transport/ingress code in `svc_ingest` while it's mid-rename risks churn/conflict. **Before Task 1, read the edge-reshape design doc + confirm chunk ordering with the user**; land P6a on/after the gateway rename, or in a way that rebases cleanly. This is the one place to STOP and check rather than rule, because it's a cross-program collision outside this plan's worktree.
- **Secrets host-side only** — `SecretString` (zeroizing, no Debug), resolved per-call via `connection_resolver` (community-bound machine JWT, scope `connections:resolve`), **never cached**, never in guest memory, logs, spans, or Valkey payloads. Retire `EnvSecretResolver` (alpha/test-only).
- **Verifier fails closed** — missing secret → reject; scheme + secret_ref come from the HOST route registration, never the request body or the connector; constant-time compare; per-scheme replay window + dedup (reuse `ReplayGuard`); read the secret by route key, never from the body.
- **Outbound is host-pinned** — the target (channel/broadcaster id) is bound host-side from the connection or the origin event, NEVER from guest input (fixes the P0 hole below); all outbound goes through `bundle_host_http`'s SSRF-guarded egress (allowlist, DNS pinning, redirect recheck).
- **`source-id` stays opaque** (no channel names / PII) per the connector.wit rule.
- **Per-community/per-tenant credentials** replace the single process-wide bot identity (net-new) — resolved via `connection_resolver`.
- Docs + tests + OTel every task.

## Review Focus

- **P0 — relay channel host-pinning** (`twitch-relay-channel-not-host-pinned`): today `outbound.rs send` takes `{channel, text}` from the bundle message → a bundle can target ANY channel. The generic outbound MUST bind the target host-side from the connection/origin (the Discord `origin_channel_id` model, `svc_action/capabilities.rs:75-85`). Test: a bundle attempting to send to a non-owned channel is refused. **This is a standalone security win — Task 6 can land first/independently.**
- **Verifier correctness across schemes** — HMAC-SHA256 (signed-string template configurable), Ed25519, Stripe-style must each: read secret from route key, constant-time compare, enforce replay window + dedup, fail closed on missing secret. Test per scheme: valid passes; tampered/replayed/missing-secret all rejected.
- **Secret never leaks to the guest** — the connector gets the verified body + an opaque `secret-ref` for outbound, never the secret. Test: a guest-visible frame/handshake contains no secret material; logs/spans scrubbed.
- **connection_resolver per-call, no cache** — transports resolve secrets per send/connect, never store them. Test: no secret retained across calls (resolver mock asserts one resolve per use).
- **Webhook route isolation + limits** — each route has its own owner bundle, scheme, rate limit, body limit; one tenant's route can't be driven by another. Test: wrong-owner dispatch refused; oversized body + over-rate rejected.

## Tasks (summary — finalize step detail after edge-reshape coordination + P3b merge)

### Task 1: Generic webhook ingress — route registry + scheme-parameterized verifier + `WebhookTransport`
Move the existing Twitch EventSub HMAC logic (`http/eventsub.rs`) in as ONE scheme behind a generic `POST /intake/webhook/{route_key}` (the `TODO(M5)` seam at `http/mod.rs`). Add a route registry `route_key -> {owner_bundle, scheme, secret_ref, replay_window, rate_limit, body_limit}`; a `Verifier` with scheme impls (hmac-sha256/ed25519/stripe); reuse `ReplayGuard`; dispatch the verified body into the owning connector bundle's `on-frame` via a new `WebhookTransport` impl.

### Task 2: `WebSocketTransport` + `PollTransport` (Transport trait impls)
Real generic WS (tungstenite) + poller (the `on_frame_outcome` cursor) impls of the `Transport` trait — platform-agnostic (URL/auth/subscription come from the connector's handshake + config).

### Task 3: Wire `connection_resolver` into transports + verifier; retire `EnvSecretResolver`
`send_handshake` secret substitution + the verifier secret lookup resolve via `connection_resolver` (per-call, `SecretString`). Remove the static-env credential path.

### Task 4: Connector `tool-provider` export + `tool` import in the connector WIT world *(depends on P3b stage-tools)*
Extend the connector world so a connector service bundle bundle can EXPOSE tools (`get_chatters`/`send_chat`) and (optionally) call tools — reusing the P3b tool-provider/tool mechanism, so app bundles invoke platform data via `tool.invoke`.

### Task 5: Heartbeat scheduler
Implement the stubbed heartbeat scheduling (`on-heartbeat-due`) in the driver.

### Task 6: Generic host-pinned outbound primitive  *(contains the P0 fix — can land first)*
A generic outbound that resolves `secret-ref` via `connection_resolver`, binds the target host-side from the connection/origin event, and sends through `bundle_host_http`'s egress guard. Replaces `outbound.rs`'s bundle-supplied channel + single-bot identity. **Ship the host-pin fix early** regardless of the rest of P6a.

---

## Execution Handoff
`feature/generic-transport-layer` off `release/v3.0.X`, AFTER P3b merges and AFTER edge-reshape chunk ordering is confirmed. Task 6 (P0 host-pin) may be carved out as its own earlier PR (security fix). One PR per coherent slice into `release/v3.0.X`; capability/security change → adversarial review. Connectors (P6b) depend on this.

*Plan drafted 2026-10-30 from P6 recon. Step-level detail finalizes after edge-reshape coordination + P3b merge. The P0 relay-channel host-pin is the highest-priority item and is independently shippable.*
