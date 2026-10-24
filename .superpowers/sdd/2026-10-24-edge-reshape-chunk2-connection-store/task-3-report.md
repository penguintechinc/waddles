# Task 3 report: community-bound, SPIFFE-ready machine auth (HIGH-2 part 1)

Status: DONE (one design concern below).

## Implementation
- `libs/flask_core/flask_core/service_jwt.py`: `BOUND_TOKEN_TTL_SECONDS=300`, `SCOPE_CONNECTIONS_RESOLVE/LIST`,
  `ALLOWED_EXTRA_CLAIMS={community,conn}`; `issue_with_claims(..., extra_claims)` allow-lists names, requires
  non-negative int (bool refused; community 0 = tenant-wide sentinel is valid), clamps bound TTL to 300s, ceiling kept.
- `hub_api/blueprints/service_jwt_bp.py`: `connections:resolve` requires `community_id` (400), optional `connection_id`;
  other scopes (incl. unbound `connections:list`) reject binding fields (400). Bound tokens 300s; `expires_in` now derived
  from signed claims; audit adds `community_id`/`connection_id` (ids only).
- `hub_api/services/machine_auth.py`: `bind_community` (missing/garbled claim 401, mismatch 403, conn claim checked),
  SVID path (only when `SPIFFE_PEER_TRUST` is True; header named by `SPIFFE_PEER_ID_HEADER`; regex fullmatch; allow-listed
  `ServiceIdentity` scope + `system` tenant; community via `X-Connection-Community`), 503 when verifier/issuer unconfigured,
  generic bodies, counter `machine_auth_total{mechanism,outcome}`. Bearer wins over peer header.
- `hub_api/config.py` + `app.py`: `SPIFFE_PEER_TRUST` (default false), `SPIFFE_PEER_ID_HEADER` (default `X-Spiffe-Peer-Id`).

## TDD
RED (impl reverted): `ImportError: cannot import name 'BOUND_TOKEN_TTL_SECONDS'` (flask_core suite);
hub_api: `test_bound_token_matching_community_passes` FAILED (no `bind_community` kwarg).
GREEN: flask_core bound 7+ pass; `hub_api tests/test_machine_auth_bound.py tests/test_service_jwt_token_endpoint.py` 45 passed.
Regression: flask_core full 2725 passed/10 skipped; hub_api `tests/provider_connections` 542 passed, `tests/dataplane_scope` 44 passed,
`test_app_factory` 13 passed (needed `defusedxml` on PYTHONPATH from scratchpad: env gap, unrelated).
Coverage (touched files): machine_auth.py 96%, service_jwt_bp.py 94%. ruff clean on touched files; mypy --strict clean on hub_api files.
Run with `PYTHONPATH=../libs/flask_core` from `hub_api/`.

## Concerns
1. SVID community binding is caller-asserted (`X-Connection-Community`), as the brief specifies; it only proves the caller
   names the route's community, not that the SVID is entitled to it. Real isolation on that path rests on the allow-list.
2. Existing `connections:resolve` bootstrap callers (svc-ingest) now get 400 without `community_id`; Task 5 cutover must
   update them. Resolve routes are not yet `bind_community` (Task 4).
3. Helm chart does not yet expose the two SPIFFE env vars (defaults are safe/OFF).
