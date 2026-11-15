# Alpha smoke test

`make alpha-smoke` (or `scripts/smoke/alpha-smoke.sh`) proves the deployed Waddles on
dal2alpha is functional. Fully read-only: it needs `create pods/portforward` but no `pods/exec`
and creates no pods (same port-forward approach as the deploy gate in #1337).

- K8s layer: all Deployments ready (count reported), Helm release `deployed`, seeder: Helm `deployed` AND (seeder Job absent or Succeeded) AND no Failed/Error seeder pod.
- HTTP layer: `kubectl port-forward` to hub-api and hub-webui, readiness gated by the shared
  `scripts/ci/hub_api_probe.py` (count-verified), then `alpha_checks.py` runs on the caller's host (stdlib
  only) against the forwarded ports.
  - hub-api `/health`, `/ready`; hub-webui `/` contains `<div id="root">`.
  - Unauthenticated `/api/v1/superadmin/communities` is 401/403.
  - Admin login using `INITIAL_ADMIN_*` from Secret `<release>-hub-admin-bootstrap` (read with kubectl, or
    `ADMIN_EMAIL`/`ADMIN_PASSWORD` env override; passed to the checker via env, never printed) returns a JWT, then authed `auth/me`,
    `admin/bundle-versions`, `marketplace/catalog` (total >= bundle-versions total), `superadmin/communities`.
  - `marketplace/vendors`: non-empty, each item has key/display name/count, counts sum to catalog total,
    first-party vendor present and `catalog?vendor=` returns total > 0. Field names are constants at the
    top of `alpha_checks.py` (assumed until the vendor-listing PR lands).
  - Bundle activation round-trip is reported `SKIP` (only mutating endpoints exist).

Output ends with `checks=N passed=P failed=F skipped=S`; exit is non-zero on any failure or
zero checks. JWTs are redacted from all output.

Env: `KUBE_CONTEXT` (dal2alpha), `NAMESPACE` (waddlebot), `RELEASE` (waddlebot),
`SMOKE_FIRST_PARTY_VENDOR` (penguintechinc). Guard-logic tests: `make test-alpha-smoke`.
