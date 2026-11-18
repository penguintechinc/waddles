# Analytics: empty, unavailable and error states

The hub-webui analytics pages (`PlatformAnalytics`, `SuperAdminAnalytics`, `MyAnalytics`,
`AdminMemberAnalytics`) render each widget independently, in one of three presentations. One failed
widget never blanks the page.

| State | When | Presentation |
|---|---|---|
| Empty | Successful response, no rows or all-zero counters | Neutral `role="status"`: "No data yet" / "No events yet" / "No activity yet" |
| Unavailable | `error.code = ANALYTICS_UNAVAILABLE`, or a bare 502/503 | Neutral `role="status"`: "Analytics aren't available on this deployment yet." Never the raw `service_unavailable` string |
| Real error | Anything else (5xx from a configured upstream, 4xx, network) | `role="alert"` with the actual message and a Retry button |

A 402 (plan/flag gate) renders a neutral per-widget "Not included in your plan" notice.

## hub-api error codes (`/api/v1/analytics/*`)

Envelope: `{success:false, error:{code, message, timestamp}}`.

| Status | `error.code` | Meaning |
|---|---|---|
| 503 | `ANALYTICS_UNAVAILABLE` | `ANALYTICS_CORE_API_URL` unset (backend not deployed) |
| 502 | `ANALYTICS_UNAVAILABLE` | Upstream unreachable (connection refused/timeout). Fails loud as 502 (gh-894, never 200-empty) but carries the same code so the UI shows the neutral unavailable state |
| 502 | `ANALYTICS_UPSTREAM_ERROR` | A configured upstream answered 5xx/401/403/other 4xx. A real error, never swallowed into "unavailable" |
| 402 | `FEATURE_NOT_ENABLED` | `waddles.analytics.community_health` flag off (Professional+) |
| 400/404/409/422/429 | `ERROR_<status>` | Client-meaningful upstream 4xx, message re-wrapped |

Endpoints that work without the backend (`/platform/reputation`, SQL-only) return 200 with zeros and
empty arrays on an empty table.

## Webui

`components/analytics/classifyAnalyticsError.ts` classifies a thrown API error;
`components/analytics/AnalyticsStates.tsx` holds the presentations and `AnalyticsPanel` (per-widget
loading/failure/empty wrapper); `usePlatformAnalytics.js` loads the five platform widgets with
`Promise.allSettled`.

## Known gap

The core analytics service was deleted (PR #1233), so cross-community analytics
(`/me/*`, `/community/*`, `/platform/{growth,activity,community-health,overview}`) report
"unavailable" until a data source is wired. Tracked in issues #894, #448 and #1226. The community
admin analytics page remains a "not available yet" placeholder by design.
