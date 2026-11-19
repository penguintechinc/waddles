# P3b Task 0: Connection re-entrancy spike

VERDICT: SYNC NESTED conn.invoke SAFE (with caveats: executor slot starvation + host-call permit cap; use a depth guard + shorter nested deadline)

## Connection model: MULTIPLEXED by frame id (not serialized)
- Stage side `core/svc_action/src/host_api.rs:200-300`: `Connection{ids: IdAllocator, pending: CorrelationTable, writer_tx: mpsc::Sender, scopes: Mutex<HashMap<u64,InvokeScope>>}`; doc at :196-199 says "one connection multiplexes many invokes".
- `Connection::invoke` (:273-307): allocs id, `pending.register(id)` -> oneshot rx, inserts scope keyed by id, `send()` (bounded mpsc try_send, short await), then `rx.await`. No lock held across the await (scopes Mutex is locked/dropped in short sync blocks; `unwrap_or_else(into_inner)`). svc_process twin: `core/svc_process/src/host_api.rs:328`.
- Executor side `core/bundle_executor/src/wire.rs:403+ read_loop`: `Message::Invoke` -> `tokio::spawn` per invoke (:479-481); `Message::HostCall` replies delivered via `Connection::deliver` -> same correlation table. Single writer task drains mpsc (wire.rs:281).
- Stage read loop `host_api.rs:625-690`: each `HostCall` is `tokio::spawn`ed (:657) after a `try_acquire_owned` on `host_call_permits` (cap `max_concurrent_host_calls`, :213/:236); over cap -> shed loudly with `overloaded` (:633-650). Read loop never blocks on handlers.
- So a host-call handler runs on its OWN spawned task, not on the task awaiting the outer `conn.invoke`. Outer invoke future is just parked on `rx.await`; nested invoke registers a distinct id/oneshot. No self-deadlock path in the stage.
- Reverse channel = the same connection (host-call/host-result frames, correlated by `call_id` == outer invoke frame id; scope lookup `scope_for(call_id)` :344, handler gets `&InvokeScope` only).

## Why Discord used queue+poll (capabilities.rs:286-303, 1340 await_discord_ack)
NOT a re-entrancy limit. The drain lives in a different service (svc_ingest) that owns the Discord bot token; result must cross process boundaries, so it is Valkey queue + GETDEL poll (50ms) with a 3s timeout sized to fit inside the 10s invoke budget (:286-303). Polling vs blocking pop was chosen to avoid stalling the shared Valkey connection. Irrelevant to bundle-to-bundle on the same executor.

## Caveats the implementer MUST handle
1. Handler has no Connection today: `CapabilityHandler::handle(&self,&InvokeScope,HostCallBody)` (capabilities.rs:193-203). Inject `Arc<ConnectionRegistry>` (host_api.rs; `active()`, never cache) or a `Weak<Connection>` into the handler; resolve the connection at call time (reconnects replace it). Note svc_action `invoke` takes `InvokeScope`; svc_process takes `Arc<dyn CapabilityHandler>`.
2. Executor starvation: outer guest holds its bundle slot + global call permit (`acquire_call_slots`, invoke.rs:470-500, timeout `pool_wait`) while blocked in the host-call. Nested invoke needs another (target bundle slot + global). Saturation or A->B->A cycles wait up to pool_wait then fail `pool_exhausted` (bounded, no hang). Mitigate: depth cap (e.g. 1-2) carried in scope, reject self/cycle calls, nested deadline < remaining outer deadline.
3. Host-call permit: each in-flight tool call holds one `host_call_permits` slot for its whole duration; nested chains consume N. Size the cap or expect `overloaded` sheds.
4. Scope: nested invoke must build a fresh InvokeScope (target app_id, same tenant/community from the verified outer scope, target app_version); never reuse caller's app_id (grants keyed by app+version, :dispatch.rs:~490).
5. Writer queue backpressure (cap 256, send timeout) applies equally; nested adds frames, not a new failure mode.

## Not verified
- Executor-side behavior of a wasm guest blocked in host-call: did not read host/imports.rs deeply (whether the guest store/instance is pinned to a thread or `spawn_blocking`); assumed async host imports so another invoke proceeds. Confirm via a test with 2 concurrent invokes.
- Actual defaults of executor_max_concurrent_calls / pool_wait_ms / max_concurrent_host_calls (cli file not located).
- Did not run any tests (no cargo build, per instructions).
