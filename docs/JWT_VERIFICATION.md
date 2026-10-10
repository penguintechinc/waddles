# JWT Verification (Phase 0 Hardening)

How every Python service verifies a JWT, what it refuses, and what it emits. This is **Phase 0**
of the H-2 asymmetric-JWT migration plus the MED-5 permissive-claims finding (2026-10-09 security
audit). It is non-breaking hardening only: **minting is still HS256** with the shared
`SECRET_KEY`; the move to ES256/JWKS is a later phase and adds a *second* verifier rather than
widening this one.

Code: `libs/flask_core/flask_core/jwt_hardening.py` (shared primitives),
`flask_core/auth.py` (platform HS256), `flask_core/service_jwt.py` (service EdDSA),
`hub_api/services/sso_oidc.py` (IdP ID tokens). Standard: RFC 8725 (JWT BCP).

## The three verifiers

| Verifier (`verifier` label) | Algorithm (exactly one) | Token | Entry point |
|---|---|---|---|
| `platform_hs256` | `HS256` | user/session JWT, shared secret | `flask_core.auth.verify_jwt_token` |
| `service_eddsa` | `EdDSA` | per-service machine JWT, hub-api JWKS | `flask_core.service_jwt.ServiceJwtVerifier.verify` |
| `oidc_id_token` | `RS*`/`PS*`/`ES*`/`EdDSA` allow-list, one per token | external IdP ID token | `hub_api.services.sso_oidc` |

**One algorithm per verifier.** The header `alg` is compared (case-sensitively) with the
verifier's allow-list *before* any cryptography, and the same single algorithm is then passed to
the JWT library. A token cannot choose a different algorithm, and an HS512 token signed with the
correct secret is still refused.

## What is refused (platform tokens)

| Check | Rejection reason (`outcome` label) |
|---|---|
| `alg: none` in any letter case | `alg_none` |
| Header `alg` is not `HS256` (RS*, ES*, EdDSA, HS384/512, absent, non-string) | `alg_mismatch` |
| Header carries `jku`, `jwk`, `x5u`, `x5c` or `crit` (key-material / unknown-critical params) | `forbidden_header` |
| `kid` present but outside `[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}` | `bad_kid` |
| Not a three-segment JWS, undecodable / non-object / oversized (>16 KiB) header | `malformed` |
| Signature does not verify | `bad_signature` |
| `iss` or `aud` missing | `missing_claim` |
| `iss` / `aud` present but not the verifier's expected value | `bad_issuer` / `bad_audience` |
| Any of `sub iss aud iat exp scope tenant` missing | `missing_claim` |
| `sub` / `tenant` empty or not a string, or `scope` not a string | `invalid_claim` |
| `exp` passed (strict, no skew) / `iat` or `nbf` in the future beyond 30 s skew | `expired` / `immature` |
| Verifier has no secret configured (empty / unset) | `no_key` (logged CRITICAL) |

`scope` may be the empty string (no scopes granted) but the claim must be present.

### MED-5: `iss` / `aud` are enforced, not optional

Before Phase 0 a token with **no** `iss` or `aud` still verified (only present-but-wrong values
were rejected). Both are now mandatory and compared by the JWT library itself, so an `aud` list is
accepted when it contains the expected audience.

### The `global` tenant fallback is gone

`verify_jwt_token` used to default a token with no `tenant` claim to `DEFAULT_TENANT_SLUG`
(`global`) for tokens issued before 2026-11-26 (`TENANT_CLAIM_MIGRATION_CUTOFF`). That window is
**removed**: a token without a non-empty `tenant` is rejected whatever its `iat`.
`DEFAULT_TENANT_SLUG` still exists - single-tenant deployments *mint* tokens for it and
`tenancy.py` resolves it like any other tenant - it is just never a verification default.

### Rollout note

Tokens minted by `create_jwt_token` have carried every required claim (including `iss`/`aud`,
since 2026-09-01) and live at most 24 h, so no live session or service token is affected and no
re-mint tooling is needed. `create_jwt_token` also now refuses an empty `secret_key` (a token
signed with an empty key is forgeable), so a service whose secret is unset fails loudly at mint
time instead of issuing it.

## `kid` on minted tokens

`create_jwt_token` stamps `kid` into every HS256 header (`JWT_KID`, default `hs256-v1`; blank =
default; a malformed value stops the service at import). The HS256 verifier only *vets* `kid` and
tolerates its absence (tokens minted before this change live for up to 24 h); in the JWKS phase
`kid` selects the verification key. Bump `JWT_KID` when rotating the shared secret.

## Telemetry

OpenTelemetry API only; the destination is the standard `OTEL_EXPORTER_OTLP_*` configuration.

| Instrument | Type | Labels |
|---|---|---|
| `waddles_jwt_verifications_total` | counter | `verifier`, `alg`, `outcome` |
| `waddles_jwt_verification_seconds` | histogram (s) | `verifier`, `alg` |

`alg` is the lower-cased header algorithm from a closed set (`hs256 ... eddsa`) plus `none`,
`absent` and `other`, so an attacker-chosen `alg` can never create new label values. `outcome` is
`ok` or one of the reasons above (plus `unknown_kid`, `scope_denied`, `invalid`).

Watch before the ES256 cutover:

```
sum by (alg) (rate(waddles_jwt_verifications_total{outcome="ok"}[5m]))     # alg mix; hs256 drains, es256 rises
sum by (outcome) (rate(waddles_jwt_verifications_total{outcome!="ok"}[5m])) # rejection mix
waddles_jwt_verifications_total{alg=~"none|other"} > 0                      # probing - should be zero
```

A telemetry failure is logged (exception type only) and never changes the verdict.

## Logging

Rejections log one line from a closed vocabulary - `JWT rejected: verifier=<v> reason=<r>
alg=<a>` - at ERROR (alg/header/kid/signature/claim attacks), WARNING (expired, garbage) or
CRITICAL (`no_key`). The token, its claims, header values and the JWT library's error text are
**never** logged. Successful verifications log at DEBUG only.

## Follow-ups (not in this change)

- **Rust `svc_*` verifiers need the same Phase 0 hygiene**: pin one algorithm per verifier, reject
  `alg: none` and `jku`/`jwk`/`x5u`/`x5c`/`crit`, enforce `iss`/`aud`/required claims, drop any
  default-tenant fallback, and emit the same per-algorithm metric (identical metric and label
  names so the dashboards line up).
- Module-local Python verifiers that call `jwt.decode` directly with their own secret
  (`action/pushing/*_action_module`, `core/{engagement,video_proxy,reputation,workflow_core,
  browser_source_core}_module`, `libs/module_sdk/security/scoped_tokens.py`,
  `services/action-platforms`) already pin `algorithms=[...]` but do not use these primitives;
  migrate them onto `inspect_header` / `record_verification` module by module.
- Phase 1+: JWKS endpoint, ES256 minting with real `kid`s, a second (asymmetric) platform
  verifier, then retire HS256.
