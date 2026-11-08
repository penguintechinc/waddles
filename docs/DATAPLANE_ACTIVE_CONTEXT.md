# Data-plane active context (per-user community routing)

A Discord guild can be paired to several communities. Slash interactions are guild-wide, so the
invoking **user's active context** picks the processing community: their explicit choice, else the
first-registered community of the source. svc-ingest (no database) asks hub-api over **internal
gRPC**; the client is `core/svc_ingest/src/active_context.rs` (`ContextSink`, `/context`) on
`hub_client::active_context`.

## Contract

`proto/waddles/hub/internal/v1/active_context.proto` — `ActiveContextService` on hub-api's internal
TLS gRPC listener (`hub_api/grpc_internal/active_context_servicer.py`). Machine JWT in the
`authorization` metadata, scope `dataplane:active-context`, `system` tenant (any other tenant:
`PERMISSION_DENIED`). Granted in the chart to svc-ingest's SPIFFE identity alone.

| RPC | Input | Success |
|---|---|---|
| `GetActiveContext` | `platform`, `source_id`, `user` | `ActiveContext` |
| `SetActiveContext` | `platform`, `source_id`, `user`, `community` | `ActiveContext` (the new context) |

`user` is the **tokenized user UUID**; hub-api never sees a platform user id. `community` is a
community `public_id` UUID — names never resolve at hub-api. `community_id` is the **numeric**
community id: the key the data plane routes on and the interaction ownership check compares
(numeric-id rule); svc-ingest treats an answer without it as `Malformed` and fails closed. The
field set is pinned by a regression test.

| gRPC status | Meaning |
|---|---|
| `INVALID_ARGUMENT` | `platform`, `source_id`, `user` or `community` not in canonical form |
| `UNAUTHENTICATED` / `PERMISSION_DENIED` | missing/wrong-scope credential / non-`system` tenant |
| `NOT_FOUND` | no usable community linked to the source, or `community` not paired to it |
| `FAILED_PRECONDITION` | the source is paired across tenants (never pinned) |
| `ABORTED` | the pairing changed between the two reads of one request — retry |
| `UNAVAILABLE` | the install store is not configured (never a default answer) |

## Logic and storage

The choice itself is owned by `services/app_command_registry.py` (`get_active_context` /
`set_active_context`, table `user_active_context` from migration `0082_user_active_context`): the
switchable set is the source's paired communities in registration order, a stored choice that has
left the set yields to the default (`explicit=false`), and a source paired across tenants is
refused. The gRPC adapter only adds the numeric id (resolved from the same paired set) and the
`system`-tenant check. Rows are deleted on Art. 17 erasure
(`erasure_registry`: `delete("user_active_context", uuid("user_uuid"))`).

Tests: `hub_api/tests/test_active_context_grpc.py` (real gRPC, production interceptor chain, real
Postgres migrated to head, one real TLS listener); client side in
`core/hub_client/src/active_context.rs` and `core/svc_ingest/src/active_context.rs`.
