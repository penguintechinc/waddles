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

Flag OFF: unchanged inline REST send. Flag ON: Discord `chat.send` is queued
to the outbound drain (detokenize -> LPUSH -> ack-wait -> meter once).

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
