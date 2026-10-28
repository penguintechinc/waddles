# Chat / Activity Data Retention - Design Note

> **Status: DESIGN - pending legal/DPO review. Not implemented.** Closes the *documentation*
> half of GRC finding G4 (Art.5(1)(e) storage limitation); enforcement is a tracked follow-up.

## Finding

Chat and activity tables are erased per-subject on request but **never aged out**. Only mem0
(90d), analytics and suppression records have TTLs. Investigated at release/v3.0.X: no
scheduler or purge job exists for these tables, and hub-api has no generic background-job
runner, so a safe deletion job is a feature, not a one-line change (deliberately not half-built).

## Proposed windows (to confirm with legal/DPO)

| Table | Content | Proposed retention | Basis |
|---|---|---|---|
| `live_activity_events` | handle + message text + bot reply (WebUI live feed) | **7 days** | display feed only; also bounds pre-0062 rows with no erasure key |
| `hub_chat_messages` | hub chat text | **90 days** (tenant-configurable, 30-365) | operational; aligns with mem0 90d |
| `activity_message_events` | per-message counters / handle | **180 days** raw; `activity_stats_daily` aggregates kept | leaderboards need aggregates, not raw rows |

Retained regardless: `audit_events`, consent, deletion ledger (Art.5(2)/17(3)(e)).

## Mechanism (follow-up work)

1. `retention_policies` config: per-table `days` with deploy defaults above, tenant override
   bounded by min/max; kill-switch `waddles.disable-retention-purge` (opt-out flag, default ON).
2. `hub_api/services/retention_purge.py`: batched `DELETE ... WHERE <ts> < now() - interval`
   (`LIMIT`-sized batches, advisory lock so one replica runs it, per-table timeout).
3. Scheduler: Kubernetes `CronJob` (hourly) invoking a hub-api CLI (precedent:
   `hub_api/cli/suppression.py` expiry purge). Stateless, rootless.
4. Telemetry: deleted-row counter per table, last-success gauge, ERROR log on failure
   (PII-free: table + counts only). Alert on stale last-success.
5. Tests: purge removes only expired rows, honours tenant override, is idempotent, never
   touches retained tables; erasure-coverage test unchanged.

## Open questions

- Are hub chat messages tenant records the controller must set windows for (likely yes)?
- Backups: purged rows persist until backup expiry; document backup retention.
