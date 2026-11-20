# Post-cutover verification sweeps (api / bundle / old-id)

Reusable, read-only-by-default checks against alpha (`dal2alpha`, ns `waddlebot`).
Conventions match the webui page sweep: `BASE_URL`, `HOST_HEADER`, credentials from
env (`HUB_TEST_EMAIL` / `HUB_TEST_PASS`), JSON + markdown report, Makefile target.

| Target | What | Pass criteria |
|---|---|---|
| `make api-sweep` | Every OpenAPI route (`openapi/v1.yaml`) as admin; url_map vs spec diff | 0 FAIL (5xx, 401 w/ admin, 4xx contradicting spec) and 0 old-prefix response bodies |
| `make bundle-sweep` | Every `app_catalog` bundle: resolved / loaded / invoked / described; app_id regex | 0 failed bundles, 0 non-conforming app_ids |
| `make old-id-scan` | Old prefixes in DB columns, Valkey (SCAN), S3, ConfigMaps, pod env | total = 0 |

Pre-cutover baseline: `make api-sweep ARGS=--allow-old-ids`, `make bundle-sweep ARGS=--allow-old-ids`.

## Access
No `BASE_URL` -> `kubectl port-forward svc/waddlebot-hub-api-v3` (Cloudflare Access fronts the
public URL). Otherwise `BASE_URL` (+ `HOST_HEADER` for the internal LB). Unverified TLS is honoured
only for loopback. With no `HUB_TEST_PASS`, the admin credentials are read from the
`waddlebot-hub-admin-bootstrap` secret into env vars only (never echoed, logged or written).
hub-api rate-limits 100 req/min per user; the sweep throttles to 1.4 req/s (`SWEEP_RATE`) and
honours `Retry-After`.

## Safety
- Mutating routes get **bogus ids + an empty body** and must 4xx; real ids are never sent.
  No-param mutating routes with no body schema or a destructive name are SKIPPED.
- `--allow-mutations` (off) sends real mutating requests. Never creates/modifies users by default.
- DB access: `tests/e2e/lib/psql-ro.sh` forces `default_transaction_read_only=on`.

## Classification
PASS 2xx / 403 scope / expected 4xx on must-fail probes. FAIL any 5xx or network error, 401 with
admin token, 400/422/405/404 on a request built from discovered ids. SKIPPED no id available (a
bogus-id probe still must not 5xx; reasons listed). Old-prefix scan list: see `OLD_PREFIXES`.

## bundle-sweep scope note
`invoked` = svc-action `action_dispatch_log` shows a successful dispatch for the app_id on the real
ingest -> process -> action path. There is no external hook that injects a synthetic chat event for
an arbitrary bundle (only svc-ingest's flag-gated canary), so live per-command injection is a
follow-up; `--harness` additionally runs `make test-bundle-flag-on-command-e2e` (real wasm).

Reports land in `REPORT_DIR` (`/tmp/<sweep>-report`); `baseline/` holds the committed pre-cutover counts.
