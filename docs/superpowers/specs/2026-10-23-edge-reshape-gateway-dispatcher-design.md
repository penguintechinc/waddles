# Edge Reshape: svc-gateway + svc-action Dispatcher — Design

**Status:** APPROVED 2026-10-23 — **Chunk 1 greenlit first**; the four remaining open questions settled as noted in §10 ("Chunk 1 first, you settle the rest"). Chunk 1 plan: `docs/superpowers/plans/2026-10-23-edge-reshape-chunk1.md`.
**Date:** 2026-10-23
**Author:** Justin Bowen (with Claude)
**Supersedes/feeds:** slash-command architecture (`docs/superpowers/specs/2026-10-23-slash-command-architecture-design.md`) depends on this; the v2 workflow tool is reborn as a follow-on spec that builds on this.

---

## 1. Intent & Why

Waddles v3's data plane is a three-stage pipeline over Valkey streams — **ingest → process → action** — with a strict, MAC-signed `StageEnvelope` (penguin-spine) between stages. Today the stages are shaped for a **stateless, one-direction** flow: a platform event comes in at ingest, gets transformed in process, and `svc-action` is effectively "the thing that posts back to Discord."

That shape is wrong for three things we now have to support, all at once:

1. **Bidirectional, stateful platform work** — Discord/Twitch gateway websockets, HTTP webhooks, and *interactions* (slash commands). An interaction isn't fire-and-forget: it carries a short-lived **interaction token** (3-second ack wall, ~15-minute followup window) that lives and dies at the platform edge. "Post back to Discord" is no longer a leaf action; it's a stateful request/response the edge owns.
2. **Many kinds of output destination** — a bundle's output might go back to a platform (stateful), out to an external REST API, to **AWS Lambda** or **GCP Cloud Functions** (both first-class), to the overlay (`svc-presentation`), or to streaming (`svc-streaming`). One output can **fan out** to several of these.
3. **Orchestrated multi-step automation** — the v2 workflow tool (trigger → conditions → action sequence, with branching and fan-out) reborn as a service bundle that dispatches across all those destinations.

This reshape makes the edge **explicitly bidirectional and stateful** and makes action an **explicit dispatcher that routes by state ownership** — so that the slash-command feature, the destination-pluralism, and the workflow engine all rest on one coherent boundary instead of three ad-hoc ones.

**It is evolutionary, not a rewrite.** `svc-action` already has a `relay_op_supported` allowlist and already queues some ops back to the gateway's outbound drain with an `op_id` ack handshake. `svc-ingest` already owns the Discord gateway (`discord_gateway.py` + `discord_rest.rs` + `ScopeResolver` + the drain). We are naming, consolidating, and completing a boundary that half-exists, not inventing one.

---

## 2. Goals

- **G1 — One stateful platform edge.** `svc-gateway` (renamed from `svc-ingest`) owns every platform connection *in and out*: gateway websockets, inbound webhooks, interactions, the per-platform REST client, per-platform rate limiting, and all stateful request/response correlation (interaction token, 3s defer, followup). The token never leaves the gateway.
- **G2 — One action dispatcher.** `svc-action` decides *what a bundle's output means* and routes each output op to a destination **by state ownership**, via a shared transport/connection library. It applies cross-cutting output steps (grant-gate, usage-meter, and — only where the destination's PII scope permits — detokenize) before routing. One output op may fan out to several destinations.
- **G3 — Pluggable destinations.** Destinations are **configured connections referenced by id**. Adding a destination type = adding one adapter to the transport/connection library. Each adapter owns its transport, its envelope (e.g. wrapping into a Lambda/GCP serverless event), its auth/retries/timeouts/idempotency, and its PII scope.
- **G4 — Minimize sensitive-data blast radius.** Creds, tokens, and PII live in **as few places as possible, and for PII, nowhere it isn't strictly required**. The interaction token stays host-side in the gateway. PII detokenization happens at exactly one trusted egress boundary per destination class, defaulting to **none**.
- **G5 — Unblock slash commands.** `svc-gateway` holding the interaction token/defer and `svc-action` dispatching the followup back to it is the foundation slash-command Phases 3–5 build on. This spec's first chunk must deliver exactly enough of that boundary to unblock them.
- **G6 — Workflow tool reborn.** The v2 node-graph workflow engine returns as a **service bundle on svc-action** that orchestrates multi-step dispatch across the same destinations. (Detailed in its own follow-on spec; this spec only reserves the seam.)

---

## 3. Non-Goals

- **N1** — Rebuilding `svc-process`. The stateless transform stage is unchanged.
- **N2** — Bumping the `StageEnvelope` / penguin-spine wire format. The envelope stays strict + MAC; the interaction token stays host-side and is referenced by a **non-secret id** on the wire, never embedded. (If a genuinely unavoidable field addition appears during planning, it stops and comes back — it is not assumed here.)
- **N3** — Building Slack/Mattermost platform edges. The gateway is designed to host more platforms, but only Discord (and existing Twitch) are built now.
- **N4** — Shipping the workflow bundle in this spec. It gets its own spec and plan; here we only avoid foreclosing it.
- **N5** — A general-purpose "run any external code" destination. External destinations are HTTP, AWS Lambda, and GCP Cloud Functions via typed adapters — not an arbitrary-code sink.

## 4. Anti-Goals (these would make it a failure even if it "works")

- **A1 — PII leaking to an external/serverless destination.** If `svc-action` can ever hand raw PII (name, email, phone, address, **username**) to Lambda/GCP/external HTTP, the design has failed — even if every current bundle happens not to. The guarantee must be structural (tokens-only by construction), not behavioral.
- **A2 — The interaction token (or any platform credential) on the wire or in a bundle.** A bundle that can read the token, or a token that transits an envelope/payload/stream, is a failure.
- **A3 — The overlay becoming a data-exfiltration channel.** `svc-presentation` is public-facing; an output path that lets a bundle dump arbitrary users' PII onto a public overlay is a failure, even though the overlay legitimately needs a username.
- **A4 — A "big-bang" rename that strands the running alpha.** The cutover must be stageable and parity-validated against today's fan-out, fail-loud, flag-gated — not a flip that requires everything to land at once.
- **A5 — Re-homing so much code that "rename" becomes "rewrite."** The boundary move is bounded (see §7); anything beyond that bound is out of scope for this reshape.

---

## 5. Constraints

- **Languages/tiers (locked by rules):** the data-plane core (`svc-gateway`, `svc-action`, `svc-process`, spine) is **Rust** (in-line of traffic). Connectors/bundles are **WASM** (may be authored in Python). `hub-api` is **Python 3.13 + Quart**. No new Go.
- **PII boundary:** `hub-api` owns the `users` identity table and is the only place raw PII lives. Everything on the data plane uses **UUID/token only**. Detokenization is a per-send lookup into hub-api; **no PII is cached** on the data plane.
- **Identity/keying:** numeric PK internally; **UUID `public_id` across every boundary**; int-enum codes for status/type/kind; destinations and connections referenced **by id**, never by name/slug.
- **Envelope:** penguin-spine `StageEnvelope`, strict + MAC — do not bump (N2).
- **Auth:** every inter-service call carries a short-lived signed JWT regardless of transport; SPIFFE-ready. External/serverless destinations authenticate per-adapter (IAM sign for Lambda/GCP, per-connection creds for HTTP) with creds fetched from the secure connection store at send time — never embedded in a bundle or envelope.
- **Observability:** logs + metrics + traces on every new path; OTLP from `OTEL_*` env; a dead exporter never breaks a request.
- **Python regex (ReDoS):** any Python (service bundles especially) that validates input with a regex **compiles it at module level** (`re.compile`) so each pattern is reviewed once for catastrophic backtracking, and uses `.fullmatch()` (not `.match()`) for whole-string validation — Python `$` also matches before a trailing `\n`, unlike a Postgres `~` CHECK. (See the slash-command `COMMAND_NAME_RE` fix, 9fb254a4.)
- **Testing:** 90%+ coverage, real-path tests (live dependencies at capability/service boundaries), no-stubs, fail-loud on unimplemented paths.

---

## 6. The Design

### 6.1 svc-gateway — the bidirectional stateful platform edge

Renamed from `svc-ingest`. It is the **single trusted boundary for platform connections and platform-sensitive data**. It owns:

- **Inbound:** gateway websockets (Discord/Twitch), HTTP webhook receivers, and `INTERACTION_CREATE`. Inbound events are normalized into the **existing event shape** and published to the spine (`:ingest` → process) exactly as today — bundles and `parse_command` are unchanged.
- **Interaction statefulness:** on `INTERACTION_CREATE`, fire the type-5 **defer immediately** (3s wall), **store the interaction token host-side in Valkey** keyed by interaction id (~15min TTL) — never in the envelope or payload — and dedupe by interaction id (reuse the `event_dedupe` pattern). The wire carries a **non-secret interaction reference id**, not the token.
  - **The ref id is not a bearer capability (A2 + tenant isolation).** The stored token record is **bound** to `{tenant, community, owner bundle, interaction id}`. The ref id is unguessable and **one-shot** (consumed on the first successful followup, within TTL). When a followup op arrives at the drain, the gateway rejects it unless the op's `{tenant, community, bundle}` match the stored binding — so a bundle cannot post a followup to another community's or user's interaction even if it guesses/replays a ref id. This check is structural at the drain, not advisory.
- **Outbound drain:** the existing `op_id` ack-handshake drain. This is where **all stateful platform egress converges** — platform replies, interaction followups, websocket sends. The inline Discord `chat.send` that today lives in `svc-action` **moves here** (see §7), so there is exactly one place that talks to the Discord REST client.
  - **Delivery semantics:** at-least-once with **`op_id` dedupe** (the house v3 stream semantic) — a send is idempotent on `op_id`, so a crash between the platform post and the ack re-drives the op and the dedupe suppresses a duplicate post (platform idempotency keys used where the platform supports them; a narrow duplicate-post window on crash is the bounded residual, acceptable for v1). A token that reaches its ~15min TTL mid-retry → the **deadline watchdog** emits the fallback followup rather than retrying a dead token.
- **Per-platform REST client + rate limiting** (`discord_rest.rs` extended for interaction followups + the command-reconcile `PUT /applications/{app}/commands`).
- **PII detokenization at platform egress** (gateway's PII scope = **FULL**, see §6.4): it is already the trusted edge holding platform creds, so it is the right place to turn tokens back into names for a platform reply.
- **Command reconcile loop:** ~1-min per-guild loop registering the union of paired communities' webhook commands with the platform (slash-command feature).

### 6.2 svc-action — the action dispatcher

No longer "post to Discord." It **consumes the action-stage stream, decides what each bundle output op means, and routes it by state ownership.** For each output op:

1. **Grant-gate** — is this bundle allowed this capability/destination? (existing grant model)
2. **Usage-meter** — count against the metering axes (existing).
3. **Forward tokens — svc-action never detokenizes.** svc-action holds **tokens, not PII**, and has **no hub-api detokenization call path and no PII-read JWT scope** at all. Detokenization happens *only* at a destination's trusted egress, performed by that destination's owner — today the only FULL-scope egress is `svc-gateway`, which does its own per-send hub-api lookup at the moment of the platform send (§6.4). This keeps PII **off svc-action and off the wire entirely** (not merely "svc-action detokenizes carefully"). svc-action's job here is to confirm the destination's adapter scope permits the fields present and forward tokens accordingly; a bundle output carrying a field an adapter's scope forbids is rejected, fail-loud.
4. **Route by state ownership**, via the transport/connection library:
   - **Stateful** (needs a connection/token the gateway holds — platform reply, interaction followup, websocket send) → back to **svc-gateway's drain**. svc-action **never touches the interaction token**; it emits a followup op referencing the interaction id, and the gateway resolves the token from its host store.
   - **Stateless / fire-and-forget** (external REST API, AWS Lambda, GCP Cloud Function) → **direct from svc-action** via the destination's adapter. Tokens only.
   - **Internal** (overlay → `svc-presentation`, streaming → `svc-streaming`) → the internal adapter for that service.
5. **Fan out** — one output op may produce several destination sends (e.g. gateway reply + Lambda webhook + overlay update); each is an independent adapter dispatch with its own auth/retry/idempotency.
6. **Interaction ownership** (slash-command feature): the router marks the **owner** (first-registered non-wildcard bundle for the command); a non-owner's interaction reply is **dropped** so only one followup lands.

### 6.3 The transport/connection library (shared)

A single Rust abstraction, **consumed by both svc-gateway and svc-action**, extending the existing provider-connection-framework. The unit is an **adapter**, one per connection/destination type. Each adapter owns:

- **Transport** — how bytes get there (platform REST/ws, HTTP, Lambda `Invoke`, GCP Functions call, internal gRPC/stream).
- **Envelope** — how the payload is wrapped for that destination. External HTTP passes through; **Lambda/GCP wrap the op into the serverless event shape and IAM-sign** it; the gateway/internal adapters pass the op through to the drain/stream.
- **Auth / retries / timeouts / idempotency** — per destination. Creds are fetched from the **secure connection store by connection id at send time**, never embedded.
- **PII scope** (§6.4) — the adapter declares what sensitive data it is *ever* allowed to carry; default **NONE**.

**Destinations are configured connections referenced by id.** A bundle/workflow names a destination by id; the adapter resolves the connection (endpoint + creds + scope) at dispatch. Adding a destination = registering a new connection of an existing adapter type, or adding a new adapter type.

### 6.4 Sensitive-data model — per-adapter PII scope (the heart of the design)

Default scope is **NONE**. Each adapter declares its scope explicitly; svc-action enforces it at step 3 (detokenize) and step 4 (route):

| Destination class | Adapter | PII scope | Rule |
|---|---|---|---|
| External HTTP / **AWS Lambda** / **GCP Cloud Functions** | external, lambda, gcp | **NONE** | svc-action **never detokenizes** for these. Tokens/UUIDs only, leak-proof **by construction** (A1). The adapter has no code path that calls hub-api detokenize. |
| Platform (Discord/Twitch reply, interaction followup) | gateway-drain | **FULL** (at egress, inside the gateway) | The gateway is the trusted platform edge holding creds; it detokenizes at the moment of the platform send. svc-action forwards tokens to the drain; the gateway does the lookup. |
| Overlay (`svc-presentation`) | overlay | **USERNAME only, guardrailed** | The overlay is public-facing — a potential exfil channel (A3). Scope is limited to **the acting user's own public handle**, bounded (a bundle cannot request arbitrary users' data), and **audited**. No email/phone/address, ever. |
| Streaming (`svc-streaming`) | streaming | **own internal creds** (connection store) | Streaming needs its own user/pass for ingest/transcode endpoints — fetched from the connection store by id, not PII and not detokenized from `users`. |

- **Detokenization mechanism:** a **per-send hub-api lookup**; **no PII is cached** on the data plane. A FULL-scope send resolves the token(s) at the instant of send and discards. This bounds PII exposure to a single in-flight send inside a trusted service.
- **The guarantee is structural, not behavioral.** The lambda/gcp/external adapters contain *no detokenize call path at all* — so no bundle, misconfiguration, or future edit can make them emit PII short of adding such a path (which a reviewer would catch). That is what makes A1 hold.
- **Overlay guardrail detail:** the overlay adapter accepts only a `username` field, populated from the **acting user's** tokenized identity resolved to their public handle, bounded to that one user per op, and every such resolution is audit-logged. A bundle cannot pass a list of users or a free-form PII field through the overlay adapter.

### 6.5 Interaction / slash lifecycle (how the pieces meet)

```
Discord INTERACTION_CREATE
        │
        ▼
  svc-gateway ── fire type-5 defer (≤3s) ──▶ Discord
        │   store token host-side (Valkey, interaction-id key, ~15m TTL)
        │   dedupe by interaction id
        ▼  normalize → existing event shape (NON-secret interaction ref id, NO token)
     :ingest stream → svc-process (unchanged transform)
        ▼
     :action stream → svc-action (dispatcher)
        │  grant-gate · usage-meter
        │  owner = first-registered non-wildcard bundle (non-owner replies dropped)
        │  route: stateful followup → gateway drain (op references interaction ref id)
        ▼
  svc-gateway drain ── resolve token from host store ──▶ Discord followup webhook
        (detokenize at egress if the reply contains PII; ephemeral flag honored)
        deadline watchdog → fallback followup if a bundle never answers
```

The token makes **one** round trip through host-side storage and is read **only** inside the gateway at the moment of the followup. svc-action sees a reference id and a response body of tokens.

**Where the interaction ref id and user references ride (N2 is about the envelope, not the payload).** The `StageEnvelope` header — the strict, MAC-signed penguin-spine frame — is **unchanged**. The interaction **reference id** and any user/channel references (as **tokens**, for the gateway to detokenize at egress) ride in the **op/response *payload*** — the app-defined data the relay already carries as response content. Growing the payload schema with an `interaction_ref` field and keeping mentions tokenized is an app-data change, not an envelope/MAC change, so it satisfies N2. The exact payload field names are left to the plan (§9). If — and only if — planning finds something that genuinely cannot live in the payload and needs an envelope field, it **stops and comes back** (N2); it is not assumed here.

---

## 7. Rename & Re-home Scope (the bound on "evolutionary")

**Pure rename (`svc-ingest` → `svc-gateway`):** code module/dir names, Helm release/chart names, image names, internal service identity (SPIFFE id), docs. Mechanical, broad, but behavior-preserving.

**Stream names:** mostly **unchanged** — they are platform/app-keyed (`:ingest`, `:action`, per-app), not named after the service's old identity. Any stream literally named for `ingest`-the-service is reviewed case-by-case during planning; the default is leave-as-is to avoid a migration.

**Bounded re-home (the only behavior move):** the inline Discord `chat.send` path moves **out of `svc-action` into the `svc-gateway` drain**, so platform egress has exactly one home (§6.1). This is the single "code moves" item; everything else in the rename is naming.

**Everything else stays put:** `svc-process` untouched; the envelope untouched; the grant/meter logic stays in svc-action; the existing drain + `op_id` ack handshake is reused, not rebuilt.

Anything that would grow beyond this bound (e.g. re-architecting the spine, moving process logic) is **out of scope** (A5) and, if it proves necessary, stops and comes back as a new decision.

---

## 8. Phasing (chunks)

**Chunk 1 — Gateway rename + egress consolidation + dispatcher skeleton (this reshape's deliverable, unblocks slash Phases 3–5):**
1. Rename `svc-ingest` → `svc-gateway` (code/helm/images/SPIFFE/docs), behavior-preserving.
2. Move inline Discord `chat.send` into the gateway drain — single platform egress.
3. Stand up the transport/connection library with its first adapters: **gateway-drain** (stateful, FULL scope) and a **route-by-state-ownership** dispatcher skeleton in svc-action.
4. Wire the interaction-followup route (svc-action → gateway drain via interaction ref id) — the exact seam slash Phases 3–5 consume.
5. Flag-gated, **parity-validated against today's fan-out**, fail-loud on an output op with no resolvable destination.
   - **Parity oracle = shadow/dry-run, not double-post.** The gateway drain gets a **capture/dry-run mode**: under the flag, the new dispatcher path computes what it *would* send and the harness compares that to the existing fan-out's output for the same input — the new path does **not** actually post while shadowing, so real Discord egress never runs twice. Exit criterion: byte-equivalent (modulo an allowed-diff list the plan defines) dispatch output across a captured-traffic corpus, then flip the flag to make the new path authoritative and the old fan-out the dry-run. The old path is removed only after the new one is authoritative and clean.

**Chunk 2 — External + serverless adapters:** external HTTP, **AWS Lambda**, **GCP Cloud Functions** adapters (PII scope NONE, IAM-sign/envelope, per-connection creds by id, per-destination retry/timeout/idempotency). Fan-out across multiple destinations.

**Chunk 3 — Internal adapters:** overlay (USERNAME-only, guardrailed, audited) and streaming (own creds) adapters.

**Chunk 4 (own spec) — Workflow service bundle:** the v2 node-graph engine reborn on svc-action — definition model/graph builder/expression engine/caps port forward; execution transport onto the v3 at-least-once stream model with **durable per-step checkpoints + idempotency**; actions become destination dispatches; triggers from the spine (not `TriggerWorkflow`); numeric-id/UUID + tokenization keying; v3 tier gating; the Rust-WASM-vs-Python-control-plane decision for the hot-path engine. **Not built here.**

Chunks 1–3 may be one spec+plan or split; Chunk 4 is definitely its own. Recommendation: **Chunk 1 as its own plan first** (it unblocks slash and carries the rename risk), then Chunks 2–3, then the workflow spec.

---

## 9. What's Left to the Builder (calls the planner/implementer makes)

- Exact adapter trait/interface shape in the transport/connection library (method set, how scope is declared and enforced in the type system — prefer making "no detokenize path" a *compile-time* property of the external/lambda/gcp adapters where Rust's type system allows).
- The concrete Valkey key schema + TTL constant for the host-side interaction token store (reuse `event_dedupe` conventions).
- The op/response contract fields: interaction ref id, ephemeral flag, owner marker, fan-out destination list, deadline-watchdog timing.
- Where the dispatcher skeleton's route table lives and how a destination id resolves to an adapter + connection (cache TTL for connection lookups, mirroring the ~1-min routing cache pattern from the slash spec).
- The parity-validation harness comparing dispatcher output to today's fan-out under the flag.
- Precise rename mechanics (grep scope, Helm release migration, image retag, SPIFFE id transition) — staged so alpha never goes dark (A4).
- The serverless envelope shapes (Lambda event JSON, GCP Functions request) and IAM signing path per adapter (Chunk 2).

---

## 10. Resolved Decisions (user: "Chunk 1 first, you settle the rest", 2026-10-23)

- **Q-B — Chunk 1 as its own plan first: YES (user).** Chunk 1 is planned and executed on its own; Chunks 2–3 and the workflow spec follow.
- **Q-A — Connection store (settled, my call): a hub-api-owned connection store.** A DB table at the hub-api secret boundary; **secret fields envelope-encrypted at rest** (platform-managed key baseline, Enterprise external-KMS/CMK upsell per `security.md`); connections referenced by numeric id / `public_id` UUID. Adapters fetch a connection's endpoint+creds via a **short-lived authenticated hub-api call at send time**, **never cached on the data plane** — same discipline as PII detokenization. Static platform bot creds (Discord/Twitch app tokens) stay in K8s `existingSecret` as today. Reuses the provider-connection-framework connection API. (Chunk 2 concern; not needed for Chunk 1.)
- **Q-C — Gateway is the sole detokenizer (settled, confirmed).** svc-action never holds a PII-read scope and never detokenizes (§6.2 step 3). PII never touches the action→gateway hop or the wire.
- **Q-D — Overlay username = opt-OUT, not opt-in (settled, my call).** The acting user's own already-public handle shows on the overlay by default; a documented per-user **"hide my name on overlays"** opt-out is honored at the overlay adapter when it resolves the handle, alongside the acting-user-only + bounded + audited guardrails. grc-auditor sign-off before prod. (Chunk 3 concern.)
- **Q-E — Alpha rename cutover (settled, my call): fresh release alongside, not in-place.** Deploy a new `svc-gateway` Helm release **alongside** the running `svc-ingest`, verify green, then remove the old release — never an in-place SSA rename (credential-clobber risk, see the clobber incident). `existingSecret` preserves `waddlebot-platform-credentials`; helm pinned **v3.17.3** (v4 fresh-install managedFields bug). Staged so alpha never goes fully dark.

### Calls I made resolving the builder-check (check these while reading)

| # | Builder question | My call (now in the doc) |
|---|---|---|
| 1 | Who detokenizes platform replies? | **Gateway only**, at egress; svc-action never does (§6.2 step 3) — see Q-C to confirm |
| 2 | Where do ref id + user refs ride vs N2? | The **payload**, not the envelope; envelope/MAC unchanged (§6.5) |
| 3 | Ref id as bearer capability? | Bound to `{tenant,community,owner,interaction id}`, unguessable, one-shot, drain-enforced (§6.1) |
| 4 | Parity oracle for cutover? | **Shadow/dry-run** capture mode; new path doesn't post while shadowing (§8 Chunk 1.5) |
| 6 | Drain delivery semantics? | At-least-once + `op_id` dedupe; TTL-expiry → watchdog fallback (§6.1) |
| 5 (part) | Owner & grant authority? | Owner = hub-api `app_commands` (slash Phase 1 registry); grant-gate = existing svc-action logic |
