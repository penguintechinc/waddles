# Star Citizen Spectrum Integration

One-way sync from RSI's **Spectrum** (the Star Citizen community platform) into Waddles: forum threads and replies, lobby chat, **organization roster changes** and **organization events**. Spectrum is read-only to Waddles by design.

> **One-way, always.** Waddles never posts, edits or reacts on Spectrum. Spectrum has no official public API; automating writes against its undocumented endpoints would risk RSI actioning the connected account or the organization. There is no outbound path in the code (`SpectrumPollReceiver.directions == {INBOUND}`) and none will be added unless RSI ships a sanctioned write API.

## What syncs

| Source | `kind` | What Waddles ingests | Default poll |
|--------|--------|----------------------|--------------|
| Forum channel | `forum` | new threads and replies | 15 s |
| Lobby | `lobby` | new chat messages | 15 s |
| Org roster | `roster` | members **joined**, **left**, **roles changed** (role grants/revokes and org-rank changes) | 300 s |
| Org events | `events` | events **created**, **updated**, **cancelled**, **removed**, **RSVP count changed** | 300 s |

Each org community gets one `roster` poller and one `events` poller. Every poller is lease-guarded in Valkey, so exactly one svc-ingest replica polls a given source.

## Not yet built

Websocket real-time lobby streaming (polling covers it), opt-in DM capture, linking an RSI handle to a Waddles profile, the scheduled health probe, and the admin connection-management API with a per-community encrypted token store (the session token is deployment-level today). Tracked on [#101](https://github.com/penguintechinc/waddles/issues/101) and [#486](https://github.com/penguintechinc/waddles/issues/486).

## Enable it

1. Put the RSI session token in the operator-managed `waddlebot-platform-credentials` Secret under key `SPECTRUM_RSI_TOKEN`. It is read from the environment at connect time and never logged, never rendered by the chart.
2. Set the sources and turn the flags on (Helm values, `config.*`):

   | Value | Env var | Default | Purpose |
   |-------|---------|---------|---------|
   | `spectrumForumChannels` | `SPECTRUM_FORUM_CHANNELS` | empty | comma-separated forum channel ids |
   | `spectrumLobbies` | `SPECTRUM_LOBBIES` | empty | comma-separated lobby ids |
   | `spectrumOrgCommunities` | `SPECTRUM_ORG_COMMUNITIES` | empty | comma-separated Spectrum community ids (org sync) |
   | `spectrumIntegrationEnabled` | `FLAG_WADDLES_SPECTRUM_INTEGRATION` | `false` | master flag, `waddles.spectrum-integration` |
   | `spectrumOrgSyncEnabled` | `FLAG_WADDLES_SPECTRUM_ORG_SYNC` | `false` | org-sync flag, `waddles.spectrum-org-sync` |
   | `spectrumOrgPollIntervalS` | `SPECTRUM_ORG_POLL_INTERVAL_S` | `300` | org poll interval (floor 60) |
   | `spectrumRosterMaxDepartureRatio` | `SPECTRUM_ROSTER_MAX_DEPARTURE_RATIO` | `0.5` | roster shrink guard, see below |
   | `spectrumOrgEmitBacklog` | `SPECTRUM_ORG_EMIT_BACKLOG` | `false` | bootstrap: emit the current roster/events as `joined`/`created` on the first poll |

3. **Flags.** Org sync runs only when **both** flags are on; the master flag is the single kill switch for everything Spectrum (checked every 30 s, no restart needed). PostHog overrides the ENV baseline when connected; alpha needs no PostHog.

A configured source with no token logs a WARN and nothing starts. It never silently no-ops.

## Org sync behavior

- **First poll primes.** Connecting records the current roster/events and emits nothing (unless `spectrumOrgEmitBacklog`), so enabling it never floods Discord or any other platform with history.
- **Snapshots live in Valkey** (`waddles:t:<tenant>:spectrum:snapshot:<kind>:<community>`, 30-day TTL, ids and role/content fingerprints only, no names). A restart, a deploy or a lease failover therefore still detects joins/leaves that happened in the gap.
- **At-least-once.** The snapshot advances only after the changes were handed downstream; a crash in between re-emits them. Consumers must be idempotent (granting an already-held role is).
- **Complete or nothing.** The roster is paged until RSI returns an empty page. Hitting the page cap with data still arriving fails loud rather than diffing a truncated list, which would read as a mass departure.
- **Shrink guard.** A roster that empties, or loses more than `spectrumRosterMaxDepartureRatio` (50 %) of its members in one poll (orgs of 10 or more), is treated as an RSI glitch: nothing is emitted, the snapshot is **not** advanced, the poller backs off one full interval, and `waddles_spectrum_snapshot_anomalies_total` increments. Persisting for 8 polls escalates to the supervisor. A genuine mass change: set the ratio to `1.0` for one poll, then restore it. Upcoming events get the same guard.
- **Events** that vanish *after* their start time simply ended and are pruned silently; only a vanish before the start is `removed`.

## Events produced

Org-sync changes are fanned out under the `spectrum.org` consume tag (messages stay on `spectrum.message`) to the default app `waddles.bot.spectrum.default`, and normalized to `PlatformEvent`s:

| `event_type` | When | Notable `payload` fields |
|--------------|------|--------------------------|
| `member_joined` | member appears in the roster | `member_id`, `display_name`, `roles_added`, `role_names` |
| `member_left` | member disappears | `member_id` only (names are not stored) |
| `member_roles_changed` | role set or org rank changed | `roles_added`, `roles_removed`, `role_names` |
| `org_event_created` / `org_event_updated` | new / edited event | `event_id`, `title`, `description`, `starts_at`, `ends_at`, `location`, `organizer_id`, `status`, `rsvp_count` |
| `org_event_cancelled` / `org_event_removed` | status flipped to cancelled / gone before start | same; `removed` carries only `event_id` |
| `org_event_rsvp_changed` | attending headcount changed | `rsvp_count`, `rsvp_previous` (counts only, never per-user RSVPs) |

`actor` is the RSI member id (roster) or the organizer id (events). Org rank appears as a pseudo-role id `rank:<id>`.

## Failure behavior

| Condition | Result |
|-----------|--------|
| 401/403 or RSI "login required" | non-retryable, never retried, loud ERROR |
| 404, non-JSON, changed envelope, unparseable roster | non-retryable `endpoint_changed`, loud ERROR naming the path (never an empty poll) |
| 429 / 5xx / network | exponential backoff in the loop (org pollers never retry faster than one interval), then supervisor restart after 8 in a row; `Retry-After` honored |
| Valkey snapshot store down | transient: backs off, never read as "no snapshot" |
| Unreadable stored snapshot | discarded with an ERROR and `waddles_spectrum_snapshot_resets_total`; the poller re-primes |

The RSI endpoint paths and response shapes are community-documented (reverse-engineered) and **unverified against live Spectrum**. Defaults (all under `https://robertsspaceindustries.com/api/spectrum`): roster `POST /community/member/list`, events `POST /community/event/list`, both with `{community_id, page, pagesize}`. They are overridable per deployment (`api_base`, `paths`) without a code change, and the parsers accept the field aliases seen across revisions. Expect to adjust them on first contact with a real org.

## Observability

OTel (endpoint configured by the standard `OTEL_EXPORTER_OTLP_*` env vars):

| Signal | Name |
|--------|------|
| histogram | `waddles_spectrum_poll_duration_seconds` (by `kind`, `outcome`), `waddles_spectrum_ingest_lag_seconds`, `waddles_spectrum_org_snapshot_size` |
| counter | `waddles_spectrum_items_ingested_total`, `waddles_spectrum_poll_errors_total`, `waddles_spectrum_org_changes_total` (by `kind`, `change`), `waddles_spectrum_snapshot_anomalies_total`, `waddles_spectrum_snapshot_resets_total` |
| trace | `spectrum.poll` span per poll |

Logs carry source ids, counts, status codes and exception types only. Member names, handles, event text and the token are never logged.

## Privacy

RSI member ids and display names enter the ingest stream exactly like message authors do today; they leave it only through the normal pipeline. Valkey snapshots hold member ids and role ids only. Event RSVPs are counts, never identities. Roster join/leave tracking is gated by its own flag so it can stay off while message ingest runs.

## Operations

- **Reset a poller's memory** (e.g. after intentionally restructuring an org): delete `waddles:t:<tenant>:spectrum:snapshot:roster:<community>` in Valkey; the next poll re-primes without emitting.
- **Tests:** `core/svc_ingest/tests/test_receivers_spectrum_poll.py`, `test_receivers_spectrum_org.py`, `test_app.py` (wiring).
