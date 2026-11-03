# Bundle MCP-Tools P6b — Platform Overlay Bundles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (standing default). Steps use checkbox (`- [ ]`).
> **DEPENDS ON P6a merged** (generic transports, webhook registry + scheme verifier, host-pinned outbound, connector tool-provider WIT, connection_resolver wiring) and P3b (tool framework).

**Goal:** Move the per-platform specifics for **Twitch, Discord, YouTube** out of hardcoded dataplane code and into **service-bundle overlays** that drive the P6a generic transports, verify via the scheme-parameterized verifier, normalize platform payloads to events, and expose platform tools (`get_chatters`, `send_chat`, …) — so adding a platform later is a new overlay bundle, not dataplane code.

**Architecture:** Each overlay is a `kind: service` WASM bundle on the connector world (P6a's tool-provider/tool additions). It declares: which generic transport it uses (webhook / WS / poll), the verify scheme + `secret_ref` (host applies it), the subscription/handshake config, the payload→normalized-event parsing, and the tools it exposes. It holds NO credentials (host substitutes `secret-ref`), NO raw targets (host-pinned outbound), and NO transport/socket code (that's P6a). The hardcoded `ingest/twitch*.rs` / `discord.rs` / `outbound.rs` are deleted only after parity tests pass.

**Tech Stack:** Rust (or Python) WASM overlay bundles on the connector + stage-tools worlds; P6a generic transports; `connection_resolver` secret substitution.

**Spec:** `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-framework.md` §10a (P6b).

## Global Constraints

- **Overlays hold no secrets, no raw targets, no transport code** — they emit requests/subscriptions with opaque `secret-ref`s; the host (P6a) substitutes secrets and host-pins the target from the connection/origin event.
- **Signature verification is host-side** (P6a verifier) — the overlay only declares the scheme + secret_ref; it never verifies in-guest (no secret in guest memory).
- **`on-connect` returns opaque secret-refs**; `on-frame` receives already-verified bodies; outbound tools build the request shape, host sends it.
- **Parity before deletion** — the hardcoded path stays until the overlay passes parity tests against it (same events in, same relay out).
- **Per-community credentials** via `connection_resolver` (replaces the single process-wide bot identity).
- `source-id` opaque (no PII). Docs + tests + OTel every task.

## Review Focus

- **Normalization fidelity** — the overlay must map platform payloads to the SAME normalized events the hardcoded path produced (e.g. Twitch `channel.follow` → the existing follow event shape), or downstream bundles break. Test: golden-payload → normalized-event parity vs the old path.
- **send_chat host-pinning holds through the overlay** — the overlay's `send_chat` tool must NOT be able to target a non-owned channel; the host binds the target (P6a). Test: overlay-supplied channel is ignored/rejected in favor of the host-pinned one.
- **Discord interaction verification** — Ed25519 interaction verification is done host-side (P6a verifier) with the app public key as the route secret; a bad signature never reaches the overlay. Test: forged interaction rejected before `on-frame`.
- **Subscription idempotency / resume** — Twitch EventSub re-subscription and Discord gateway resume must be idempotent (no duplicate subscriptions, correct resume sequence). Test: reconnect doesn't double-subscribe.
- **No regression in live flows** — role sync / event sync / chat relay (the beta-critical trio) must work identically through the overlay. Test: the e2e for each live flow passes on the overlay path.

## Tasks (finalize step detail after P6a merge)

### Task 1: Twitch overlay bundle
EventSub subscription semantics (register subscriptions, HMAC scheme + secret_ref for the webhook route), payload→normalized-event parsing (follow/sub/raid/etc.), IRC chat via the generic WS or the relay path, and tools `send_chat` (host-pinned channel) + `get_chatters`/`get_followers`/`get_subscriptions` (resolves the lurker-economy data gap). Parity-test against `ingest/twitch*.rs`.

### Task 2: Discord overlay bundle
Gateway identify/resume mapped to `on-connect`/`on-frame` over the generic WS transport; interactions via the Ed25519 webhook scheme (host-verified); normalized events; tools (send message host-pinned to `origin_channel_id`, role ops). Parity-test against `ingest/discord.rs` + `handle_discord_relay`.

### Task 3: YouTube overlay bundle
Poller (or PubSubHubbub webhook) transport; live-chat/event normalization; tools as applicable. (YouTube has no current core implementation — this is the greenfield proof that a new platform = just an overlay.)

### Task 4: Delete the hardcoded platform code
After parity tests pass for Tasks 1-3, remove `ingest/twitch*.rs`, `ingest/discord.rs` (bespoke parts), and `outbound.rs`'s platform-specific send paths. Confirm the beta-critical trio (role sync / event sync / chat relay) still passes e2e on the overlay path. This is the "can't take it back after the major version" cleanup — do it last, behind green parity gates.

---

## Execution Handoff
One `feature/platform-overlay-<platform>` worktree per overlay off `release/v3.0.X`, AFTER P6a merges (Tasks 1-3 can parallelize — disjoint bundles). Deletion (Task 4) is a separate final PR gated on all parity tests. Each PR into `release/v3.0.X`; capability/security change → adversarial review. This completes the P3→P6 migration program.

*Plan drafted 2026-10-30 from P6 recon. Final plan of the bundle-MCP-tools migration program. Step detail finalizes after P6a merges.*
