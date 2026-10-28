# Marketplace sources: flag-on E2E + security regression

| Target | Needs | Proves |
|---|---|---|
| `make test-marketplace-security` | `HUB_PYTHON` with hub_api dev deps; no cluster | consolidated security regressions (`hub_api/tests/test_marketplace_sources_security_regression.py` + referenced per-task files), fixture-server tests, every hostile fixture rejected by the real scanner |
| `make test-marketplace-mutation` | same | disabling each gate (sha-pin / namespace / ack / tier / attestation) turns its tests RED |
| `make test-marketplace-e2e` | live deployment + fixture server | `tests/api/test_marketplace_sources_e2e.py` (flag ON, real egress-proxy, real executor) |

Both gates fail on missing prerequisites, zero tests, any skip, any failure (JUnit verified, no `|| true`).

## Fixture git server wiring (egress-proxy only)

hub-api clones **only** through the `git-proxy` run mode (`MARKETPLACE_GIT_PROXY_URL`): CONNECT-only, port 443, exact-host allowlist, and it refuses every private/loopback/cluster address after resolving DNS itself. There is intentionally no test bypass, so the fixture host MUST:

1. resolve only to a **publicly routable** IP (an in-cluster Service / RFC1918 address is denied by design);
2. be in `gitProxy.allowedHosts` for the E2E release (helm override; also feeds the CNP `toFQDNs`);
3. serve a certificate hub-api trusts (public CA; `--cert/--key`). `--self-signed` is for the standalone smoke only.

```bash
export FIXTURE_ADMIN_TOKEN=...            # env only
scripts/marketplace_fixture_git_server.sh --hostname git-fixture.example.org --port 443 \
  --cert fullchain.pem --key privkey.pem --wasm path/to/real-built-component.wasm --entry '<stage entry>'
```

`--wasm` must be a REAL built component (the executor really loads it); `--allow-placeholder-wasm` is smoke-only.
Chicken-and-egg: the publisher signature binds the platform-assigned `src-<public_id[:12]>` app_id, which exists only after the source is added. The E2E therefore adds the source at the bootstrap (unsigned) commit, asks the fixture to commit a version signed for that `public_id`, lets the auto-update CronJob record it as pending, and approves the pin -- the same path real publishers take.

## 11C run + mutation procedure

1. Deploy (flag ON for the admin tenant, `gitProxy.enabled`, `sourceAutoUpdate.enabled`), start the fixture, export the `MARKETPLACE_E2E_*` env listed in the e2e module docstring.
2. `make test-marketplace-e2e` -> all green, counts printed.
3. Per gate G in `python3 scripts/marketplace_mutation_check.py list`: `apply G`, rebuild/redeploy hub-api, `PYTEST_K="$(python3 scripts/marketplace_mutation_check.py e2e-k G)" make test-marketplace-e2e` MUST fail, then `revert G` and redeploy.
