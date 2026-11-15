# svc-action

Terminal pipeline stage: runs bundle actions, dispatches outcomes to platform
senders, retries with backoff, audits to `action_dispatch_log`.

## Action dispatcher (edge-reshape Chunk 1a)

The dispatcher decides *where* an outbound action goes by state ownership
(`src/dispatch_destination.rs`, `route_destination`). It never sends; the
caller acts on the `Destination`.

| Item | Value |
|---|---|
| Flag | `waddles.core.egress-via-dispatcher` (default **OFF**) |
| Route (only one today) | `StatefulPlatformReply` -> Discord outbound drain (`chat.send`) |
| No route | `DispatchError::NoDestination` -> refused `unsupported_op`, counted, never silent |

There is no inline REST send any more: Discord `chat.send` is always queued to
svc-ingest's outbound drain (LPUSH -> ack-wait -> meter once), whatever the flag
says; svc-action holds no Discord bot token. The text is queued TOKENIZED --
`{user:<uuid>}` is never resolved here (PII boundary: only svc-ingest's drain and
svc-presentation hold real names). `chat.send` envelopes also carry the MAC-bound
numeric `tenant_id` so the drain can resolve the placeholders.

### Outbound producer contract (P6a producer-completeness)

- Every signed outbound entry (Twitch and Discord, every op) carries a unique `op_id` (UUIDv4, minted in `sign_envelope` when absent)
  bound into the `pin_sig` MAC; svc_ingest keys its single-use (replay) check off the signature, so two sends of identical text are
  two distinct entries. Entries without an `op_id` are refused by the new drain (`no_op_id`).
- `dm.send`, `role.add`, `role.remove` stamp the numeric `tenant_id` from the verified `InvokeScope` (never the bundle payload; a
  bundle-supplied `tenant_id`/`community_id` is ignored) and Discord ops stamp the scope's `community_id`. A scope with no resolved
  tenant is refused `tenant_scope_missing` before anything is queued.
- `relay.send` `{"op":"role.add"|"role.remove","user_id":"{user:<uuid>}","role_key":"<managed-role key>"}` (Discord): the guild is
  pinned host-side by the drain from the community's binding, the channel is the event's origin channel, the key is
  `[a-z][a-z0-9_-]{0,31}` (never a role snowflake). Authorized by the dangerous `moderation.discord` grant; throttled 20/min per
  (tenant, community, app), limiter fails closed. Typed refusals: `invalid_args`, `tenant_scope_missing`, `rate_limited`,
  `relay_unavailable`, `not_granted`, plus the drain's `unsupported_op` / `relay_failed` ack codes (and its `role_not_managed` /
  `role_privileged` / `no_tenant_scope` duty refusals, surfaced by the ack).
- Rollout: **svc_action first, then svc_ingest**; in-flight entries signed without an `op_id` are lost (counted `no_op_id`) -- alpha
  is disposable.

### Drain-down divergence and rollback

Flag ON with the drain down: the op is refused `relay_unavailable` and
**queues nothing**, whereas flag OFF sends inline and still succeeds. This is
the same drain dependency `dm.send` already has.

**Rollback: flip `waddles.core.egress-via-dispatcher` OFF to restore the
inline send path.**

### Telemetry

| Signal | Name | Labels / attributes |
|---|---|---|
| Span | `dispatch.route` | `provider`, `op`, `destination`, `flag_on` |
| Counter | `svc_action_dispatch_routed_total` | `provider`, `op`, `destination` |
| Counter | `svc_action_dispatch_no_destination_total` | `provider`, `op` |

All attributes are allowlisted ids / closed enums -- never message text,
channel ids, or user data.

### Extension seam

- Slash Phases 3-5 add an `InteractionFollowup` `OutboundAction` plus a
  `Destination` route here. The interaction token / defer / followup host
  store is built in the slash plan, **not** in this crate's Chunk 1a.
- Chunks 2/3 add external / AWS Lambda / GCP Functions / overlay / streaming
  `Destination` variants; each extends `route_destination` and
  `Destination::label` (bounded label for telemetry).
