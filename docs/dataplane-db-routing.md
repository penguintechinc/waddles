# Data-plane database routing: primary vs read replica

Default topology is a PostgreSQL primary plus read replicas (chart README, "PostgreSQL topology"). A replica can lag
(`readReplicas.maxLagSeconds`, default 30s, removes a lagging replica from the `-ro` Service). Rule: **a path reads a
replica only when a stale row is harmless and self-healing; anything that gates a decision, advances a cursor, or
could release erased data reads the primary.**

| Component / path | Env (Helm) | Target | Reason |
|---|---|---|---|
| svc-process / svc-action changelog consumer: safe-seq watermark, `read_changes`, active-set / scope / source-binding reads, initial + full-reconcile loads | `DB_WATERMARK_HOST` (primary FQDN) | **primary** | The watermark comes from the primary; if change rows or active-set rows came from a lagging replica, `tracker.advance(safe_seq)` would skip rows below `safe_seq` forever (a missed revocation or deactivation). All of these reads share ONE connection so the cursor and the rows it covers are always from the same node. Tests: `consumer_applies_a_change_below_safe_seq_from_its_own_connection_never_a_lagging_replica`, `changelog_consumer_host_never_resolves_to_the_reader_replica_host` (both crates), chart `test_changelog_consumer_stays_on_primary_and_ingest_has_no_replica`. |
| svc-process / svc-action bundle-grant gate loader (`PgGrantLoader`) | `DB_READER_HOST` (`-ro`) | replica | Polled on an interval already (`bundle_config_poll_interval`); a grant change becomes visible within poll + replica lag (bounded by `maxLagSeconds`). Revisit if grant revocation must be immediate. |
| svc-process / svc-action per-community bundle config (`bundle_config_db`, issue #1306) | `DATABASE_REPLICA_URL` (`-ro`) | replica | Read-mostly config; falls back to the primary with a WARN when unset. |
| svc-ingest display-name / detokenization resolver (`egress.rs`) | `DATABASE_URL` (primary); **no replica env** | **primary** | A lagging replica could still hold a user's name after an erasure and release PII. Test: `display_name_resolver_is_wired_to_the_primary_database_url_only` (Rust) and the chart assertion that svc-ingest has no `DATABASE_REPLICA_URL`. |
| hub-api writes, migrations, read-your-own-write paths | `DATABASE_URL` | primary | Writes; freshness required. |
| hub-api read pool | `DATABASE_READ_REPLICA_URL` (`-ro`) | replica (opt-in per route) | The pool exists; routes opt in. Routes needing read-after-write, authz, or erasure freshness must stay on the primary. |
| svc-streaming orchestrator state | `DATABASE_URL` | primary | Coordination state. |

Adding a replica read: put it in this table with its staleness argument and a test that pins the target.
