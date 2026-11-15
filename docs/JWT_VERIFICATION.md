# JWT Verification (ES256, Secrets T10)

How every Python service verifies a JWT, what it refuses, and what it emits. This is **Phase 0**
of the H-2 asymmetric-JWT migration plus the MED-5 permissive-claims finding. **Cutover done
(Secrets T10): platform JWTs are ES256**, signed by hub-api with a key sealed in `platform_vault`
and verified with public keys selected by `kid` (`flask_core.jwt_keyring`). The shared HS256
secret no longer exists and no HS* algorithm is accepted anywhere (hard cutover, no dual-accept
window; users re-login once). See `PLATFORM_VAULT.md`.

Code: `libs/flask_core/flask_core/jwt_hardening.py` (shared primitives),
`flask_core/auth.py` + `flask_core/jwt_keyring.py` (platform ES256), `flask_core/service_jwt.py` (service ES256),
`hub_api/services/sso_oidc.py` (IdP ID tokens). Standard: RFC 8725 (JWT BCP).

## The three verifiers

| Verifier (`verifier` label) | Algorithm (exactly one) | Token | Entry point |
|---|---|---|---|
| `platform_es256` | `ES256` | user/session JWT, platform-vault key by `kid` | `flask_core.auth.verify_jwt_token` |
| `service_eddsa` | `ES256` (+ legacy `EdDSA` only while `SERVICE_JWT_ACCEPT_LEGACY_EDDSA=true`) | per-service machine JWT, hub-api JWKS | `flask_core.service_jwt.ServiceJwtVerifier.verify` |
| `oidc_id_token` | `RS*`/`PS*`/`ES*`/`EdDSA` allow-list, one per token | external IdP ID token | `hub_api.services.sso_oidc` |

**One algorithm per verifier.** The header `alg` is compared (case-sensitively) with the
verifier's allow-list *before* any cryptography, and the same single algorithm is then passed to
the JWT library. A token cannot choose a different algorithm, and an HS512 token signed with the
correct secret is still refused.

## What is refused (platform tokens)

| Check | Rejection reason (`outcome` label) |
|---|---|
| `alg: none` in any letter case | `alg_none` |
| Header `alg` is not `ES256` (HS256/384/512, RS*, EdDSA, absent, non-string) | `alg_mismatch` |
| `kid` missing | `bad_kid` |
| `kid` unknown, retired or past its verify-only window | `unknown_kid` |
| No key ring installed (deployment bug) | `no_key` |
| Header carries `jku`, `jwk`, `x5u`, `x5c` or `crit` (key-material / unknown-critical params) | `forbidden_header` |
| `kid` present but outside `[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}` | `bad_kid` |
| Not a three-segment JWS, undecodable / non-object / oversized (>16 KiB) header | `malformed` |
| Signature does not verify | `bad_signature` |
| `iss` or `aud` missing | `missing_claim` |
| `iss` / `aud` present but not the verifier's expected value | `bad_issuer` / `bad_audience` |
| Any of `sub iss aud iat exp scope tenant` missing | `missing_claim` |
| `sub` / `tenant` empty or not a string, or `scope` not a string | `invalid_claim` |
| `exp` passed (strict, no skew) / `iat` or `nbf` in the future beyond 30 s skew | `expired` / `immature` |
| Lived longer than 1 h (`exp - iat > 3600 + 30 s skew`) | `lifetime_exceeded` (ERROR) |
| `jti` / `sid` present but not `[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}`, or `sub` longer than 128 chars | `invalid_claim` |
| Revoked (by `jti`, login-session `sid`, or per-user `iat` cutoff) | `revoked` (WARNING) |
| Revocation store unreachable beyond its grace window (fail closed) | `revocation_unavailable` (ERROR) |
| Verifier has no secret configured (empty / unset) | `no_key` (logged CRITICAL) |

`scope` may be the empty string (no scopes granted) but the claim must be present.

The last four rows are session revocation and the short-TTL ceiling -- see
[Session Revocation](TOKEN_REVOCATION.md). Revocation is checked **last**, so only an otherwise
valid token costs a store lookup.

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
since 2026-09-01), so no live session or service token was affected by the Phase 0 hardening.
`create_jwt_token` also refuses an empty `secret_key` (a token signed with an empty key is
forgeable), so a service whose secret is unset fails loudly at mint time instead of issuing it.

The short-TTL change that followed (access tokens now live 15 minutes, nothing may live over 1 h)
*does* refuse the 24 h tokens minted before it: every browser session signs in once more at
deploy time. Service tokens are minted fresh on every call (`expiration_hours=1`) and are
unaffected.

## `kid` on minted tokens

`create_jwt_token` stamps the active signing key's `kid` (`k` + 16 hex of the public key's SHA-256)
into every header; the caller cannot choose it. The verifier REQUIRES `kid` and uses it, and only
it, to pick the verification key (rotated-out keys stay verify-only until `verify_until`). A key
rotation is a vault operation, not an env change.

## No unverified decodes

hub-api's mint (`ServiceAuthService.MintServiceToken`, gRPC) used to re-parse its own freshly minted token with
`verify_signature=False` to read `jti`/`exp` for the audit log. It now takes them from
`ServiceJwtIssuer.issue_with_claims()` (the claims that were just signed), so no
signature-disabled `jwt.decode` remains in hub-api or `flask_core`. Keep it that way: a decode
that does not verify must never feed an authorization decision.

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

Revocation adds its own instruments (`waddles_jwt_revocation_checks_total`, `..._lookup_seconds`,
...); see [Session Revocation](TOKEN_REVOCATION.md#observability).

A telemetry failure is logged (exception type only) and never changes the verdict.

## Logging

Rejections log one line from a closed vocabulary - `JWT rejected: verifier=<v> reason=<r>
alg=<a>` - at ERROR (alg/header/kid/signature/claim attacks), WARNING (expired, garbage) or
CRITICAL (`no_key`). The token, its claims, header values and the JWT library's error text are
**never** logged. Successful verifications log at DEBUG only.

## Follow-ups (not in this change)

- **Rust `svc_*` verifiers need the revocation check** (paired follow-up, contract in
  [Session Revocation](TOKEN_REVOCATION.md#rust-verifiers-paired-follow-up-not-built-here)): the
  same Valkey keys, the 1 h lifetime ceiling and the same outage policy.
- **Rust `svc_*` verifiers need the same Phase 0 hygiene**: pin one algorithm per verifier, reject
  `alg: none` and `jku`/`jwk`/`x5u`/`x5c`/`crit`, enforce `iss`/`aud`/required claims, drop any
  default-tenant fallback, and emit the same per-algorithm metric (identical metric and label
  names so the dashboards line up).
- Module-local Python verifiers that call `jwt.decode` directly with their own secret
  (`action/pushing/*_action_module`, `core/{engagement,video_proxy,reputation,workflow_core,
  browser_source_core}_module`, `libs/module_sdk/security/scoped_tokens.py`,
  `services/action-platforms`) already pin `algorithms=[...]` but do not use these primitives;
  migrate them onto `inspect_header` / `record_verification` module by module.
- Done (T10): ES256 minting with real `kid`s; HS256 retired with no fallback.

## Service JWT ES256 cut-over (EdDSA legacy window)

Service (machine) JWTs are signed **ES256** (ECDSA P-256 / SHA-256). hub-api's JWKS publishes
`{"kty":"EC","crv":"P-256","kid","x","y","alg":"ES256"}`; rotate by adding a new `kid`, flipping
`SERVICE_JWT_ACTIVE_KID`, and dropping the old key after its longest-lived token expires.

| Setting | Default | Effect |
|---|---|---|
| `SERVICE_JWT_ACCEPT_LEGACY_EDDSA` (chart `serviceJwt.acceptLegacyEddsa`) | `false` | `true` additionally accepts `EdDSA` tokens verified against Ed25519 (`OKP`) keys. Bounded rollout window only -- set back to `false` afterwards. Never affects issuance. |

Rules enforced by every verifier (Python `flask_core.service_jwt`, Rust `core/service_auth`,
`core/egress_assertion`, `core/egress_proxy`, `core/svc_presentation`):

- `alg` must be `ES256` (or `EdDSA` while the flag is on); `none`, `HS*`, `RS*`, `ES384`, ... are `alg_mismatch`.
- The key resolved by `kid` must be of the family `alg` claims (an `EdDSA` token naming a P-256 `kid` is refused).
- The flag is parsed strictly (`true`/`false`); anything else fails startup.
- With the flag off, `OKP` entries in the served JWKS are skipped with a warning (hub-api may still publish them mid-rollout).
- Operator key generation: `KID=<kid> NAMESPACE=<ns> make generate-service-jwt-key`
  (`openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -pkeyopt ec_param_enc:named_curve`).
  Metric label `verifier=service_eddsa` is kept for dashboard continuity; the `alg` label reads `es256`/`eddsa`.
