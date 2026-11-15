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
  instant the transport hands the driver a frame (`d:g`; ws gateway, webhook queue, poll). Any path not wired stamps
  its own publish-entry time (`d:p`, a lower bound, never a silent zero).
* **Clocks:** hops that cross pods compare wall clocks (NTP skew, sub-ms to a few ms on one cluster). Queue wait is
  computed from the Valkey `XADD` entry id (server clock) vs the consumer pod's clock.
* **Command label** comes from a registered-command registry (`WADDLES_COMMAND_LATENCY_COMMANDS=ping,roll` on
  svc-process, the only place decrypted text is in memory); text is read in place, never stored or logged; anything
  unregistered is `other`. **`bundle_lang`** comes from `WADDLES_BUNDLE_LANGS=app_id:lang,...` on svc-action
  (closed set `rust|python|csharp|go|js|other`, unmapped = `unknown`).

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
| `waddles_command_e2e_seconds` | histogram | `command`, `bundle_lang`, `outcome` (`ok`,`refused`,`timeout`,`error`), `door` (`gateway`,`sink`,`http`,`publish`) |
| `waddles_command_e2e_over_budget_total` | counter | `command`, `bundle_lang`, `outcome` (e2e >= 1.0 s) |
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

## First door on the HTTP / ingress path (read-only findings, nothing applied)

* On dal2alpha the shared Cilium Gateway (`gateway-system/shared`, class `cilium-shared`) has ONE route for
  `waddles-alpha.penguintech.cloud`: `/` -> `waddlebot-hub-webui-v3:8080`. **svc-ingest (`:8200`) is not exposed
  through the gateway at all today**; Discord interactions/webhooks (`POST /intake/webhook/{route_key}`) have no
  ingress route on alpha. So there is no gateway first door to stamp yet.
* What the LB offers: Gateway API `RequestHeaderModifier` only sets STATIC values (no request-start time). The Envoy
  config is generated and owned by the Cilium operator (`CiliumEnvoyConfig/cilium-gateway-shared`), not patchable from
  a chart. Envoy can stamp `x-request-start` via `request_headers_to_add: %START_TIME(%s.%3f)%` or log
  `%START_TIME%`/`x-envoy-upstream-service-time`; Hubble L7 flow records carry timestamps for the LB hop.
* **Prepared in code:** `command_latency::first_door::parse_request_start` accepts `t=<s.ms>`, `<s.ms>`, `<ms>` and
  `<s>`, refuses implausible values; it is not wired into the webhook intake because verified webhook bodies cross an
  `mpsc` to the connector driver with no side channel (a future change adds a received-at to that frame). Until then
  an HTTP webhook's first door is the driver's frame receipt (`d:g`), excluding the LB leg.

### Admin / infra owner handoff (config this repo cannot apply)

1. **Expose** `/intake/webhook` (svc-ingest `:8200`) through the Gateway with its own HTTPRoute if interactions must
   arrive via the LB (security review: webhook signature verification stays in ingest).
2. **Stamp t0 at the gateway:** a Cilium/Envoy route-level `request_headers_to_add` of `x-request-start:
   t=%START_TIME(%s.%3f)%` (needs a CiliumEnvoyConfig-capable owner of the shared gateway), or enable Envoy access logs
   with `%START_TIME%` and correlate by `x-request-id`. Strip a client-supplied `x-request-start` at the gateway.
3. **Collector tail sampling** (traces) so error and over-budget commands are always kept:
   ```yaml
   processors:
     tail_sampling:
       decision_wait: 10s
       policies:
         - {name: keep-errors, type: status_code, status_code: {status_codes: [ERROR]}}
         - {name: keep-slow,   type: latency,     latency: {threshold_ms: 1000}}
         - {name: sample-rest, type: probabilistic, probabilistic: {sampling_percentage: 10}}
   ```
4. **Per-service env:** `WADDLES_COMMAND_LATENCY_COMMANDS` (svc-process), `WADDLES_BUNDLE_LANGS` (svc-action),
   `OTEL_EXPORTER_OTLP_ENDPOINT` + a collector allowed by the executor's egress policy if executor metrics are wanted.
5. Canary provisioning (owner decisions above) and its `CANARY_*` env on svc-ingest and svc-action.

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

**Status (feature/set-executor-perf):** suspect #2 is addressed by the executor warm instance pool (`EXECUTOR_WARM_POOL_SIZE`, off by default, on per pool in alpha/dal2alpha; watch `waddles_executor_warm_pool_total{result}` hit ratio and `waddles_executor_instantiate_seconds`), and #4 by bounded per-app drain concurrency (`APP_DRAIN_CONCURRENCY`, default 4, per-source ordering kept). Neither was measured against a live `!ping` baseline in this change; confirm with the hop histograms before relying on them.

Order to attack once the report names the slowest hop: (1) outbound drain concurrency, (2) bundle warm pools,
(3) ack wake-up, (4) per-app batch concurrency. Do not apply any before the hop histograms confirm it.
