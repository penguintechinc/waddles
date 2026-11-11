# Alpha smoke test

`make alpha-smoke` (or `scripts/smoke/alpha-smoke.sh`) proves the deployed Waddles on
dal2alpha is functional. Read-only apart from one short-lived pod in the `waddlebot`
namespace (deleted on exit via trap).

- K8s layer: all Deployments ready (count reported), Helm release `deployed`, core-bundle-seeder Job not failed.
- HTTP layer, run from inside the cluster by `alpha_checks.py` in a PSA-restricted pod
  (non-root, seccomp RuntimeDefault, drop ALL caps, read-only rootfs). Image is the
  digest-pinned `python:3.13-slim-bookworm` from `k8s/helm/waddlebot/values.yaml`
  (`waitForHubApi.image`).
  - hub-api `/health`, `/ready`; hub-webui `/` contains `<div id="root">`.
  - Unauthenticated `/api/v1/superadmin/communities` is 401/403.
  - Admin login using `INITIAL_ADMIN_*` from Secret `<release>-hub-admin-bootstrap` (injected as pod
    env via secretKeyRef; never printed) returns a JWT, then authed `auth/me`,
    `marketplace/catalog` (total >= 100), `superadmin/communities`, `admin/bundle-versions`.
  - Bundle activation round-trip is reported `SKIP` (only mutating endpoints exist).

Output ends with `checks=N passed=P failed=F skipped=S`; exit is non-zero on any failure or
zero checks. JWTs are redacted from all output.

Env: `KUBE_CONTEXT` (dal2alpha), `NAMESPACE` (waddlebot), `RELEASE` (waddlebot),
`SMOKE_IMAGE`, `SMOKE_MIN_CATALOG` (100). Guard-logic tests: `make test-alpha-smoke`.
