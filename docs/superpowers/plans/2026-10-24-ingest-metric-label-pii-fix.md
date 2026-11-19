# Ingest Metric Label PII Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `source_id` (which embeds a raw Twitch channel login, `tw-<channel>`, and Discord guild ids, `dg-<id>`) from the `svc_ingest_events_published_total` Prometheus label set, leaving `platform` as the only label.

**Architecture:** The label is applied at exactly one site: `impl penguin_spine::SpineMetrics for IngestMetrics::stream_event_written` in `core/svc_ingest/src/telemetry.rs`, called by the single publish choke point `publish_event_with` (`publish.rs:344`) for every platform. Fix = register the counter with `&["platform"]` and ignore the trait's `source_id` argument (the pinned `penguin-spine` trait signature cannot change). This mirrors the already-merged sibling fix in `svc_action`/`svc_process` `stage_metrics.rs` (`fn stream_event_written(&self, platform: &str, _source_id: &str)`, doc: "source ids are deliberately NOT labels").

**Tech Stack:** Rust, `prometheus =0.14.0` (existing), `penguin-spine` (pinned, DO NOT bump).

## Global Constraints

- **Ruling: drop the label, no replacement identifier.** A numeric `community_id` is PII-free but still per-community unbounded cardinality (grows with every paired community), and no dashboard/alert/script reads `source_id` (all consumers are `sum(...)` or `sum by (platform)`). Per-community volume belongs in the envelope/trace, not a Prometheus label. Label set after fix: exactly `["platform"]`; `platform` values stay the closed set (`twitch`, `discord`, ...).
- **Scope: all platforms.** The single `stream_event_written` impl serves Twitch (`tw-<channel>`, `tw-eventsub-<broadcaster_id>`), Discord (`dg-<guild id>`, `dg-dm`) and the generic connector sink (`connector/sink.rs:198`). Fixing the impl fixes all; do not special-case Twitch.
- **penguin-spine is pinned — DO NOT bump or change the `SpineMetrics` trait.** `source_id` still flows to stream-key/workstream/app_id derivation (infra keys, not telemetry); only the metric label changes. Do not touch envelope/`publish_event_with` logic.
- **Out of scope (flagged, not fixed here):** `tracing` log fields `source_id = %source_id` in `ingest/twitch.rs:113-183` and `ingest/discord.rs:394` also carry the channel login. Chunk 2 Stream B2 Task 1 (`docs/superpowers/plans/2026-10-24-edge-reshape-chunk2-twitch-inbound.md`) removes them for Twitch; Discord log fields remain a follow-up GitHub issue (guild id, not a handle, lower severity).
- Rust only, no `.unwrap()`/`.expect()` outside tests (the existing registration `.expect` on a static metric definition is retained); `cargo fmt`/`clippy -D warnings` clean; 90%+ coverage on touched code; no stubs; no `|| true` gates.
- OTel/penguin conformance: counter stays emitted per published event (≥1 data point still asserted by smoke telemetry gate); no new hand-rolled logging.
- Base: fresh worktree off `origin/release/v3.0.X` tip `cfa003fe`; re-verify paths/lines with Read/Grep first.

## Review Focus

- **Series loss assumed harmless:** old `{platform,source_id}` series disappear on rollout. Verified consumers only aggregate: `k8s/observability/alerts/waddles-slo.rules.yml:147`, `k8s/observability/manifests/prometheusrule.yaml:162`, `k8s/observability/dashboards/waddles-data-plane.json:183,241,506`, `k8s/observability/scripts/obs_tool.py:610,618,650`. Reviewer re-greps for any `source_id` matcher/`by (source_id)` (Task 2 does this).
- **Label arity mismatch panics at runtime:** `with_label_values` with the wrong number of values panics. Registration and the single call site must change together (Task 1 test hits the real call path).
- **Regression only asserts the unit:** the leak must be asserted through `publish_event_with` rendering the real registry, not just `stream_event_written` (Task 1 step 1b).

---

### Task 1: Drop `source_id` from `svc_ingest_events_published_total`

**Files:**
- Modify: `core/svc_ingest/src/telemetry.rs` (`register_ingest_metrics` `:147-157`, `impl SpineMetrics for IngestMetrics` `:386-391`, test `ingest_metrics_stream_event_written_increments_the_labeled_counter` `:~440`, doc comment on `events_published_total` `:110` and the help string `:150`)
- Modify: `core/svc_ingest/src/publish.rs` (tests module: add one real-registry regression test next to the existing `publish_event` tests, ~`:420`)

**Interfaces:**
- `IngestMetrics.events_published_total: prometheus::IntCounterVec` — label names `&["platform"]` (was `&["platform","source_id"]`).
- `<IngestMetrics as penguin_spine::SpineMetrics>::stream_event_written(&self, platform: &str, _source_id: &str)` — signature unchanged; increments `with_label_values(&[platform])`.
- Help text: `"Total normalized platform events XADDed onto the spine, labeled by platform (source ids are deliberately not labels: PII + unbounded cardinality)"`.

- [ ] **Step 1a: Write the failing unit test.** Replace the existing test body in `telemetry.rs` with: register metrics, call `stream_event_written("twitch", "tw-somechannel")` twice and `("twitch", "tw-eventsub-999")`, `("discord", "dg-123456789")` once each; assert `events_published_total.with_label_values(&["twitch"]).get() == 3` and `["discord"] == 1`; render via `render_metrics`; assert rendered text contains `svc_ingest_events_published_total{platform="twitch"} 3` and does NOT contain `source_id`, `tw-somechannel`, `tw-eventsub`, `dg-123456789`; assert the family's label names (via `registry.gather()` -> metric label pairs) are exactly `["platform"]`.
- [ ] **Step 1b: Write the failing real-path regression test** in `publish.rs` tests: build a real `IngestMetrics` from `register_ingest_metrics(&Registry::new())`, run `publish_event_with(None, &FakeAppender, &metrics, ..., source_id "tw-channelA", ...)` for a twitch event and a `"dg-111"` discord event, render the registry, assert the counter is present with only `platform` labels and neither `tw-channelA` nor `dg-111` appears anywhere in the rendered exposition. Comment `// regression: ingest-metric-label-pii` (no GH issue number exists; file one per `github-issue-tracking` only if the user wants it tracked).
- [ ] **Step 2: Run both tests, confirm FAIL** (`cargo test -p svc_ingest telemetry:: publish::` via the repo's containerized/`make` Rust test target — see Makefile; report counts: tests run ≥2, failures = the two new/changed).
- [ ] **Step 3: Implement.** In `register_ingest_metrics` change the label slice to `&["platform"]` and update the help string; in `stream_event_written` rename the parameter `_source_id` and use `.with_label_values(&[platform])`; update the `events_published_total` field doc to say labeled by platform only and point to the PII/cardinality rule (`rules/critical-rules.md` Observability, `security.md`).
- [ ] **Step 4: Run tests, confirm PASS** — full `svc_ingest` Rust suite (`cargo test -p svc_ingest`), `cargo clippy -p svc_ingest -- -D warnings`, `cargo fmt --check`; confirm `tests/telemetry.rs` and `tests/telemetry_otlp.rs` (the smoke OTel emission gates) still pass with printed counts (data points ≥1).
- [ ] **Step 5: Commit** — `fix(svc_ingest): drop source_id (channel login) from events_published_total label`. Push the branch (backup rule); no PR/merge unless asked.

### Task 2: Verify no consumer depends on the label; document the migration

**Files:**
- Modify: `docs/observability/data-plane-metrics.md` (the `svc_ingest_events_published_total` mention, `:82-83`)
- Read-only verify: `k8s/observability/**`, `docs/**`

**Interfaces:** none (docs + verification).

- [ ] **Step 1: Verify (the failing check is a grep that must return zero).** Run `git grep -nE 'svc_ingest_events_published_total\{[^}]*source_id|by \([^)]*source_id' -- k8s docs core` and `git grep -n 'source_id' -- k8s/observability`; expected: zero matches that reference this metric. Report the number of files scanned (denominator > 0: the known consumers listed in Review Focus must each be seen and confirmed to aggregate only). Any hit = stop and migrate that query to `by (platform)` in this task.
- [ ] **Step 2: Edit the doc** to state: the counter is labeled `platform` only; `source_id`/channel/guild identifiers are deliberately not labels (PII + unbounded cardinality); per-source breakdown is available from traces/envelope, not metrics. Add a one-line **migration note**: "Released vX: `source_id` label removed from `svc_ingest_events_published_total`; `sum`/`sum by (platform)` queries are unaffected; any ad-hoc `by (source_id)` query returns nothing — use `by (platform)`." (Use the actual next version only if a tag exists — see `versioning` skill; otherwise say "next release".)
- [ ] **Step 3: Re-run the Step 1 grep and `make lint` (or the repo's docs/markdown lint target)**; confirm pass.
- [ ] **Step 4: Commit** — `docs(observability): note events_published_total is platform-only`. Push the branch.
