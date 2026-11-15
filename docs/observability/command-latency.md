# Command latency SLA: traces, metrics and logs

**Owner SLA (2026-11-14):** non-external bot commands (no web search, Lambda or other outside call) must have
**p99 < 1 second**, timed from the moment the request hits our FIRST DOOR until it heads back out. Includes the
Cilium LB / gateway ingress, excludes Cloudflare. "Heads back out" = the outbound platform call completing
(svc-action `discord outbound op confirmed`).

Everything here sits behind the core-mechanism opt-out kill switch **`waddles.disable-command-latency-telemetry`**
(unseen/OFF = telemetry ON; ON = no stamps, no metrics, no spans, no WARN; ENV baseline
`FLAG_WADDLES_DISABLE_COMMAND_LATENCY_TELEMETRY`, live PostHog value re-read every 30 s). Code: `core/command_latency`.

## Where t0 is taken and how it travels

```text
 first door ──t0──> svc-ingest publish ──XADD──> svc-process ──XADD──> svc-action ──invoke──> executor
 (gateway frame /     ingest.publish    (entry id   process.consume   (entry id   action.dispatch   executor.invoke
  driver frame)        stamps t0, d      = XADD ms)  adds ps pc pd c   = XADD ms)  adds ac iv        guest -> relay chat.send
                                                                                                          │ outbound.send
 svc-action capabilities: LPUSH relay ──> svc-ingest outbound drain ──REST──> Discord ──ack──> poll sees ack
                          (outbound request)                                                     └─ CONFIRM: e2e = now - t0
```

* **Carrier:** the W3C `tracestate` `waddles` member of the existing envelope `trace` (`penguin_spine::Trace`):
  `waddles=t0:<ms>;d:g;ps:..;pc:..;pd:..;ac:..;iv:..;c:<command>`. The spine envelope is an external crate that cannot
  take a field without a library release; `tracestate` already travels ingest -> process (copied onto the action
  envelope) -> action (forwarded into the executor invoke scope), so **no payload, wire or struct change** was needed.
  The binding MAC covers the trace id only, so stamping the tracestate never invalidates a MAC.
* **Not forgeable into a decision:** metadata only, never read for authorization or routing. Only the stage services
  write a stream or the envelope trace (a bundle cannot). A reader refuses a stamp more than 5 s in the future or
  older than 10 min (`waddles_command_latency_marker_invalid_total{reason=skewed|stale|missing}`), so a forged value
  can at worst skip or skew its own latency sample.
* **First door per path:** Discord gateway loop (`ingest/discord.rs`): the instant the decoded gateway frame is
  returned, before the permissions lookup (`d:g`). Connector runtime (`connector/driver.rs::process_frame`): the
  instant the transport hands the driver a frame (`d:g`; ws gateway, poll). HTTP webhooks / interactions (`/intake/webhook`):
  the HTTP handler's entry instant, or the trusted Gateway `x-request-start` (`d:h`, door `http`; see "First door on the HTTP
  / interactions path"). Any path not wired stamps
  its own publish-entry time (`d:p`, a lower bound, never a silent zero).
* **Clocks:** hops that cross pods compare wall clocks (NTP skew, sub-ms to a few ms on one cluster). Queue wait is
  computed from the Valkey `XADD` entry id (server clock) vs the consumer pod's clock.
* **Labels** come from the DATABASE READ REPLICA, never env lists and never user text (owner decision
  2026-11-15). `command` = a registered command name (`app_commands.command`, read by svc-process, the only
  place decrypted text is in memory; text is read in place, never stored or logged). `bundle` = a registered
  app bundle's `app_id`, `bundle_lang` = that bundle's newest version language (`app_versions.app_id,language`,
  read by svc-action; closed set `rust|python|csharp|go|js|other`). Anything unregistered is `other`
  (`bundle_lang` of an unmapped app = `unknown`). See "Label registries" below.

## Label registries (command / bundle / bundle_lang)

```text
 replica (DATABASE_REPLICA_URL, per-service RO role)         command_latency::registry (process-wide, bounded)
   app_commands(command)        -- svc-process  --seed/refresh/miss-->  commands  (cap 256)  -> `command`
   app_versions(app_id,language) -- svc-action  --seed/refresh/miss-->  bundles   (cap 256)  -> `bundle`, `bundle_lang`
```

* **Seed** at start (retried every 15 s until it succeeds; labels are `other`/`unknown` meanwhile), **periodic
  refresh** every 5 min, **lazy-load on miss**: a `!`/`/`-prefixed, label-shaped, unregistered token (or an unmapped
  `app_id`) wakes the refresher, rate limited to one read per 15 s however many misses arrive. A failed read keeps the
  last snapshot and logs WARN (`command-latency label registry refresh failed`). Plain chat never triggers a read.
* **Replica only:** with `DATABASE_REPLICA_URL` unset there is NO primary fallback; one ERROR is logged and labels stay
  `other`. The Helm chart already renders `DATABASE_REPLICA_URL` for both stages when
  `infrastructure.postgresql.readReplicas.host` is set.
* **Bounded:** at most 256 commands (most widely registered first) and 256 bundles; more fold into `other`
  (WARN at refresh). Worst case series per histogram = (256 + 1) x (256 + 1) x 6 langs x outcomes x doors, but real
  deployments register tens, and the registry cannot grow past the cap by construction.
* **Least privilege (alembic `0112_latency_label_reads`, mirrored in `config/postgres/service-roles.yaml`):**
  `waddles_svc_process` gets `SELECT (command)` on `app_commands` only (no tenant/community/app column);
  `waddles_svc_action` gets `SELECT (app_id, language)` on `app_versions` on top of `(id)`. SELECT only.
* **Visibility note:** the command vocabulary spans all tenants (it is a metrics label set, visible only to whoever
  can read the metrics backend); it never carries who registered a command.

## Traces (one distributed trace per command)

Plain `tracing` spans bridged to OTel by `penguin-logging`'s `tracing-opentelemetry` layer; the OTLP destination is
strictly the standard `OTEL_EXPORTER_OTLP_*` variables (unset = no trace export, everything still works).
`ingest.publish` mints the envelope `traceparent` **from its own span ids** (fallback: a locally minted trace as
before), every later stage parents its span on that `traceparent` + `tracestate` (remote parent). Attributes are
bounded and non-PII only.

| Span | Service | Attributes |
|---|---|---|
| `ingest.receive` | svc-ingest | `platform`, `pre_publish_ms` (frame -> publish leg) |
| `ingest.publish` | svc-ingest | `platform`, `tenant_id`, `community_id`, `door` |
| `process.consume` | svc-process | `app_id`, `tenant_id`, `community_id`, `deliveries`, `queue_wait_ms`, `result` |
| `process.decrypt`, `process.tokenize` | svc-process | (children of `process.consume`) |
| `action.dispatch` | svc-action | `app_id`, `tenant_id`, `community_id`, `deliveries`, `queue_wait_ms`, `result` |
| `outbound.send` | svc-action | `provider`, `op`, `app_id`, `tenant_id`, `community_id` |

`executor.invoke` is not emitted: the executor is a credential-less sandbox host that by design never creates spans
(spec section 13.2) and its stage-side leg is already the `executor_to_outbound` hop + the `waddles_executor_*`
histograms. Errors set span status = error with `exception.type` + `exception.message` (dead-letter, non-success
dispatch, refused/timed-out outbound).
gRPC hops (hub-api mint/DEK) carry no command trace context today; the stream hop is covered by the tracestate.

**Sampling.** Head sampling cannot know duration or failure. The first stage mints `traceparent` with the sampler's
decision in the flags and every later stage honors the remote parent (`ParentBased`), so the SDK's standard
`OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` apply at the ingest root. To keep error and over-budget commands
ALWAYS, run services at `parentbased_always_on` (the default) and tail-sample in the collector (admin ask below).

## Metrics

| Exposed name | Type | Labels |
|---|---|---|
| `waddles_command_hop_seconds` | histogram | `hop` = `ingest_publish`, `queue_wait`, `process_stage`, `action_queue_wait`, `dispatch_to_invoke`, `executor_to_outbound`, `outbound_call` |
| `waddles_command_e2e_seconds` | histogram | `command`, `bundle`, `bundle_lang`, `outcome` (`ok`,`refused`,`timeout`,`error`), `door` (`gateway`,`sink`,`http`,`publish`) |
| `waddles_command_e2e_over_budget_total` | counter | `command`, `bundle`, `bundle_lang`, `outcome` (e2e >= 1.0 s) |
| `waddles_command_latency_marker_invalid_total` | counter | `reason` (`skewed`,`stale`,`missing`) |
| `waddles_canary_e2e_seconds` | histogram | `outcome` |
| `waddles_canary_failures_total` | counter | `reason` (`timeout`,`misconfigured`,`publish_failed`,`store_unavailable`,`not_ok`,`result_unreadable`) |

Buckets (s): 5 ms .. 10 s, with 100 ms steps from 0.5 to 1.0 and 1.1/1.25/1.5 so the 1.0 s SLO edge is a bucket
boundary. Hop meaning: `ingest_publish` first door -> XADD acked (receive, permissions, encrypt, bind);
`queue_wait` XADD -> process consume (consumer-group lag); `process_stage` consume -> action XADD (verify, decrypt,
tokenize, transform, route); `action_queue_wait` XADD -> action consume; `dispatch_to_invoke` consume -> invoke sent;
`executor_to_outbound` invoke sent -> outbound request issued (executor queue + pool wait + instantiate + guest);
`outbound_call` request issued -> platform op confirmed (relay queue + ingest drain + REST + ack poll).
Only svc-action's Discord ack-confirmed ops are a "heads back out" point; Twitch/other relay sends are
fire-and-forget LPUSH and record no e2e. A command that sends N confirmed messages records N e2e samples.
Exemplars: attached by the OTel SDK only when its exemplar filter is enabled (`OTEL_METRICS_EXEMPLAR_FILTER`) and
the record happens inside a sampled span; the e2e record is inside `outbound.send`'s task context but this was not
verified end to end (no collector with exemplar storage is available to test against).

## Logs (penguin logging / `tracing`)

* **WARN `command over latency budget`** at e2e >= 1.0 s, one line, fields: `command`, `app_id`, `tenant_id`,
  `community_id`, `outcome`, `e2e_ms`, `slowest_hop`, `breakdown` (`ingest_publish=20,queue_wait=80,...` ms per hop),
  `trace_id`, `span_id`. No user text, username or payload.
* **ERROR** on hop failure: hop verification / decrypt / tokenize dead-letters (existing, now with a span error),
  `canary: ...` failures (`timeout`, publish, store), outbound `not confirmed in time`.
* **DEBUG** per-hop decisions (`ingest publish hop`, `queue wait recorded`).

## Follow one slow command through all three signals

1. Alert / report: `waddles_command_e2e_over_budget_total` rising, or `make command-latency-report` flags a command.
2. **Metrics:** `histogram_quantile(0.99, sum by (le,hop) (rate(waddles_command_hop_seconds_bucket[5m])))` names the hop.
3. **Logs:** search `command over latency budget` (filter `command=...`): `breakdown` + `slowest_hop` split that one
   command by hop and carry its `trace_id`.
4. **Traces:** open that `trace_id` in the trace backend: `ingest.receive -> ingest.publish -> process.consume
   (process.decrypt, process.tokenize) -> action.dispatch -> outbound.send`; `queue_wait_ms` on the consume spans and
   the span gaps show waiting vs working; error spans carry `exception.type/message`.

## Reporting tool

`make command-latency-report METRICS_URLS="http://127.0.0.1:19090/metrics http://127.0.0.1:19091/metrics"`
(port-forward svc-action and svc-process `:9090` first; read-only GETs). Prints samples, p50/p95/p99 per command and
per hop, flags the slowest hop and whether p99 < 1 s. Exit 0 ok, 1 over budget, **2 no samples (FAIL, never a pass)**.

## Synthetic canary (flag `waddles.command-latency-canary`, default OFF)

Once a minute (one driver across replicas via `SET NX EX`) svc-ingest publishes a synthetic Discord chat message
(`!ping` by default) in a dedicated canary tenant/community/guild through the REAL `publish_event` funnel (identity
encryption, v3 binding, consumer groups) -> process -> action -> the REAL executor bundle -> relay `chat.send` to a
**sentinel channel id**. svc-ingest's Discord outbound drain acknowledges exactly that `(tenant_id, community_id,
channel)` triple **without calling Discord** (`canary::sink_matches`), svc-action records
`waddles_canary_e2e_seconds` at the confirm point (so it measures the same endpoint as a user command, never polluting
the user histograms) and pushes the result to a Valkey list the driver polls. No result within 5 s -> ERROR log +
`waddles_canary_failures_total{reason="timeout"}`; a misconfigured-but-enabled canary is a per-minute ERROR +
`{reason="misconfigured"}`, never silent. Config env on svc-ingest AND svc-action: `CANARY_TENANT_ID`,
`CANARY_COMMUNITY_ID`, `CANARY_GUILD_ID`, `CANARY_CHANNEL_ID` (+ optional `CANARY_COMMAND_TEXT`). The canary flag
ignores the license bypass domain (default OFF everywhere until deliberately enabled).

### Owner decisions needed before it can run (nothing was guessed)

1. A **dedicated canary tenant + community** (numeric ids) with their own per-tenant DEK, and a **sentinel guild id**
   paired to that community in hub-api (so the v3 scope resolver maps `dg-<guild>` to it). Real communities must
   never share them.
2. A **sentinel channel snowflake** that is not a real Discord channel (the sink swallows sends to it).
3. Which **registered command** to drive (default `!ping`) and that its bundle is **activated for the canary
   community** (the ping bundle consumes `discord chat.message`).
4. Is minting an **ephemeral pseudonym for the synthetic author** (the sentinel guild id as `author_id`) in the canary
   tenant acceptable, or should the tokenizer skip a configured synthetic author?
5. Canary traffic goes through reputation/economy/analytics like any command in that community; confirm that is fine
   for a dedicated community.

## First door on the HTTP / interactions path (Gateway route + edge `x-request-start`)

Owner decision 2026-11-15: Discord interactions / webhooks come in through the shared Cilium Gateway and the SLA
clock starts at that first door (the Cilium LB, NOT Cloudflare).

```text
 Discord -> Cloudflare -> NAT -> Cilium Gateway (shared)  --HTTPRoute /intake/webhook, POST-->  svc-ingest :8200
                                  | RequestHeaderModifier SET x-request-start = t=%START_TIME(%s.%3f)%
                                  v                                    (Envoy operator, overwrites a client value)
        svc-ingest http handler: first_door_for(headers) -> t0 (edge stamp if trusted+plausible, else handler entry)
        -> WebhookIngress::handle_from_at -> mpsc Queued{bytes, first_door_ms} -> WebhookTransport::frame_first_door
        -> ConnectorDriver::process_frame(with_first_door(t0, door=http)) -> publish stamps t0 -> ... -> e2e (door=http)
```

* **Chart** (`k8s/helm/waddlebot/templates/interactions-gateway.yaml`, values `interactionsGateway`): an HTTPRoute of
  its own (`<release>-interactions`, `POST` + PathPrefix `/intake/webhook`, longer prefix wins over hub-webui's `/`)
  on the same shared Gateway parentRef as `httproute`, plus a CiliumNetworkPolicy admitting the Gateway (reserved
  `ingress` entity + the Gateway namespace) to svc-ingest's HTTP port ONLY (never the metrics port). **Alpha
  (`values-dal2alpha.yaml`) enables it; every other environment is off by default and supported** (set
  `interactionsGateway.enabled=true` with `httproute.enabled` and the Rust data plane). Enabled without a Gateway is a
  render error, never a silent skip. Chart tests: `k8s/helm/waddlebot/tests/test_interactions_gateway_render.py`
  (helm 3.17.3 and helm 4, every `values-*.yaml`).
* **Stamp semantics:** `RequestHeaderModifier.set` OVERWRITES (never appends) so a client-supplied `x-request-start`
  cannot survive; svc-ingest additionally refuses a request carrying several values. svc-ingest trusts the header only
  when `INGEST_TRUST_EDGE_REQUEST_START=true` (rendered exactly when the stamping route is on); otherwise a
  client-sent header is ignored and the handler entry time is the first door.
* **Skew bounds** (`command_latency::first_door::resolve_edge_start`): a stamp more than 5 s ahead of the pod clock or
  unparseable -> refused `skewed` (this includes an Envoy operator the gateway did NOT expand, i.e. the literal
  `%START_TIME(...)%`); a stamp up to 5 s ahead reads as "no edge leg"; older than `MAX_EDGE_LEG_MS` (5 s) -> refused
  `stale`; absent while trusted -> `missing`. Every refusal falls back to the handler entry time, increments
  `waddles_command_latency_marker_invalid_total{reason}` and logs one WARN (no value). The door label is `http` in all
  cases. With the edge stamp used, the `ingest_publish` hop includes the LB -> pod leg (no separate hop).
* **UNVERIFIED on a live cluster:** that the Cilium Gateway's Envoy expands `%START_TIME(%s.%3f)%` inside a Gateway API
  header modifier value (Envoy evaluates command operators in `request_headers_to_add`, which is what the Gateway API
  filter is translated to). After the first alpha deploy, check `make command-latency-report` /
  `waddles_command_latency_marker_invalid_total`: a steady `skewed` rate means the operator is not expanded; then use the
  fallback admin step 2 below. Nothing breaks in that case, the clock just starts at the handler.

### Admin / infra owner handoff (config this repo cannot apply)

1. **Shared Gateway listener admission:** the `https` listener of `gateway-system/shared` must admit the Waddles
   namespace (label `gateway-access: shared-dal2a`, already required by the hub-webui route; no new step on alpha).
   Other environments: the equivalent label/selector on their Gateway.
2. **Only if the header modifier does not expand `%START_TIME%`:** a `CiliumEnvoyConfig`-capable owner of the shared
   Gateway adds a route-level `request_headers_to_add` `x-request-start: t=%START_TIME(%s.%3f)%` with
   `append_action: OVERWRITE_IF_EXISTS_OR_ADD` for host `waddles-alpha.penguintech.cloud` path `/intake/webhook`, or
   enables Envoy access logs with `%START_TIME%` correlated by `x-request-id`.
3. **Discord Developer Portal:** set the application's *Interactions Endpoint URL* to
   `https://<public host>/intake/webhook/<route_key>` (route registered in svc-ingest; owner action, never automated).
4. **Collector tail sampling** (traces) so error and over-budget commands are always kept:
   ```yaml
   processors:
     tail_sampling:
       decision_wait: 10s
       policies:
         - {name: keep-errors, type: status_code, status_code: {status_codes: [ERROR]}}
         - {name: keep-slow,   type: latency,     latency: {threshold_ms: 1000}}
         - {name: sample-rest, type: probabilistic, probabilistic: {sampling_percentage: 10}}
   ```
5. **Per-service env:** `OTEL_EXPORTER_OTLP_ENDPOINT` + a collector allowed by the executor's egress policy if executor
   metrics are wanted. **A read-replica host** (`infrastructure.postgresql.readReplicas.host` -> `DATABASE_REPLICA_URL`)
   for svc-process and svc-action: the label registries never read the primary, so without it `command` / `bundle` stay
   `other` (one ERROR at start).
6. Canary provisioning (owner decisions above) and its `CANARY_*` env on svc-ingest and svc-action.

## Latency suspects in the hot path (code reading; nothing measured, nothing optimized in this PR)

Ranked by expected p99 contribution; all `file:line` verified on this branch. The pods on dal2alpha still run the
pre-instrumentation images, so there are no per-hop numbers yet: deploy this, then run the report.

| # | Suspect | Evidence | Low-risk fix | Expected saving |
|---|---|---|---|---|
| 1 | **Serial outbound drain**: one `BRPOP` loop handles one op at a time (`claim_once` Valkey RTT -> `egress.prepare` detokenize -> REST) | `core/svc_ingest/src/outbound.rs` `discord_drain_loop_with` / `handle_discord_entry` | bounded concurrency (N in-flight ops, per-channel ordering kept) | tens-hundreds of ms under any concurrency; removes head-of-line blocking (p99, not p50) |
| 2 | **Python/C# bundle cold start** and instantiate per invoke | `bundle_executor/src/invoke.rs` (`Store` per call, `instance_pre` reuse); Python via componentize-py (`waddles_executor_instantiate_seconds`) | pre-warm / pool warm instances; check `waddles_executor_instantiate_seconds{app_id}` | 50-300 ms for Python bundles, ~1-5 ms Rust |
| 3 | **Ack poll interval 50 ms** | `core/svc_action/src/capabilities.rs:364` `DISCORD_OP_ACK_POLL` | `BLPOP` on the ack key, or poll at 5-10 ms | ~25 ms avg, up to 50 ms |
| 4 | **Per-consumer serial batch handling** in process and action | `svc_process/src/spine.rs` `drain_batch` (`for d in &batch { handle_delivered(d).await }`), `svc_action/src/dispatch.rs` `drain_batch` | bounded per-app concurrency | removes head-of-line wait when a slow command precedes |
| 5 | **PII tokenize / identity decrypt** per message (hub-api `MintEphemeralPseudonyms` gRPC on a memo miss) | `svc_process/src/spine.rs` `process.tokenize`, `tokenize_memo.rs`, `pii_tokenize.rs`; DEK cache `hub_client/src/dek.rs` | already memoized per message; confirm hit ratio via `waddles_process_tokenize_memo_total` and DEK `waddles_hub_dek_cache_total` | 0 on a hit; one hub-api RTT (5-30 ms) per distinct message on a miss |
| 6 | **Executor pool wait**: `EXECUTOR_POOL_WAIT_MS` = **500** live (not 2000; 2000 is the bundle's guest `limits.timeout_ms`, e.g. `bundles/rust/ping/bundle.yaml`) with 4 instances/bundle, 32 concurrent calls | `bundle_executor/src/config.rs:130`, `k8s/helm/waddlebot/values.yaml:1995-2001`, live `kubectl get deploy` env | raise `instancesPerBundle` if `executor_to_outbound` p99 tracks load; lower pool wait to fail fast | only under saturation; bounded at 500 ms |
| 7 | **`XREADGROUP BLOCK` is NOT a latency floor**: `SPINE_BLOCK_MS` (default 1000) only bounds the idle wake, a new entry returns immediately | `penguin-spine` `reader.rs` (`xread_options(... .block(block_ms))`) | none needed; `queue_wait` p99 shows the true lag | ~0 (a suspect ruled out by reading) |
| 8 | **Egress proxy hop**: only `http.send` (outside calls) goes through `core/egress_proxy`; relay `chat.send` goes Valkey -> ingest drain directly, not through it | `svc_action/src/capabilities.rs` relay path vs `egress_proxy` | n/a for the non-external SLA | 0 |
| 9 | **Gateway-frame leg**: `permissions.resolve(guild, author)` runs before publish on the legacy Discord loop; it is TTL-cached, but a miss does a Discord REST fetch INSIDE the first-door leg | `svc_ingest/src/ingest/discord.rs` (`permissions.resolve`; resolver `cache_get`/`fetch`) | refresh-ahead, or publish first and attach permissions later; watch `ingest_publish` p99 | one Discord REST RTT (~50-200 ms) per cold or expired author |

Order to attack once the report names the slowest hop: (1) outbound drain concurrency, (2) bundle warm pools,
(3) ack wake-up, (4) per-app batch concurrency. Do not apply any before the hop histograms confirm it.
