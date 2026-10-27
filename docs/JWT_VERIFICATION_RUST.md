# JWT Verification — Rust Verifiers (Phase 0 Hardening)

The Rust half of the H-2 Phase 0 / MED-5 hardening. The contract — one algorithm per verifier,
`alg: none` and key-material headers refused, `iss`/`aud`/required claims enforced, no
default-tenant fallback, one shared metric stream — is defined for Python in
[`JWT_VERIFICATION.md`](JWT_VERIFICATION.md) (PR #796, `libs/flask_core/flask_core/jwt_hardening.py`).
This page is the **Rust section** of that contract: which Rust verifiers exist, how each one
meets it, and where it deliberately differs. Standard: RFC 8725 (JWT BCP). Minting is unchanged.

## The three Rust verifiers

| Verifier (`verifier` label) | Algorithm (exactly one) | Token | Code | Used by |
|---|---|---|---|---|
| `service_eddsa` | `EdDSA` | per-service machine JWT, hub-api JWKS | `core/service_auth` → `verify` | `svc_presentation` (overlay PUSH via `overlay_auth`), `egress_proxy` (caller auth) |
| `platform_hs256` | `HS256` | user/session platform JWT, shared secret | `core/svc_streaming/src/http/auth.rs` | `svc_streaming` `/api/v1/*` |
| `egress_assertion` | `EdDSA` | single-use egress grant (Rust-only) | `core/egress_assertion` → `verify_with_key`, `core/egress_proxy/src/assertion.rs` | `egress_proxy` |

`svc_ingest`, `svc_process` and `svc_action` contain **no** JWT verifier (none depends on
`jsonwebtoken`): they only *mint* a machine JWT (`service_auth::MachineJwtClient`) for outbound
hub-api calls. `svc_presentation` has no verifier of its own either — its PUSH guard is
`overlay_auth::require_push_credential`, which calls `service_auth::verify`, so it inherits
everything below.

## What is refused

Reason strings (the `outcome` metric label) are **identical** to the Python table.

| Check | Rejection reason | `service_eddsa` | `platform_hs256` | `egress_assertion` |
|---|---|---|---|---|
| `alg: none`, any letter case | `alg_none` | yes | yes | yes |
| Header `alg` not the verifier's one algorithm (HS384/512, RS\*, ES\*, `eddsa`, absent, non-string) | `alg_mismatch` | yes | yes | yes |
| Header carries `jku`, `jwk`, `x5u`, `x5c` or `crit` | `forbidden_header` | yes | yes | yes |
| `kid` outside `[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}` | `bad_kid` | yes (never reaches the trust bundle) | yes | yes (never reaches the trust bundle) |
| Not a 3-segment JWS, undecodable / non-object / >16 KiB header | `malformed` | yes | yes | yes |
| `kid` absent or unknown to the trust bundle | `unknown_kid` | yes | n/a (HS256 has one key) | yes |
| Empty or unset signing secret | `no_key` (logged `severity=critical`) | n/a (key comes from the JWKS) | yes | n/a |
| Empty configured audience / issuer (nothing to compare against) | `bad_audience` / `bad_issuer` | yes | yes | n/a |
| Signature does not verify | `bad_signature` | yes | yes | yes |
| `iss` / `aud` missing | `missing_claim` | yes | yes | n/a (no `iss`/`aud`, see below) |
| `iss` / `aud` wrong (an `aud` list is accepted if it contains the expected value) | `bad_issuer` / `bad_audience` | yes | yes | n/a |
| A required claim missing | `missing_claim` | `exp iat nbf iss aud sub scope jti` | `sub iss aud iat exp scope tenant` | all ten assertion fields |
| `sub`/`tenant`/`jti` empty, `scope` not a string, any claim mistyped | `invalid_claim` | `sub jti scope` + `tenant` if present | `sub tenant scope teams roles` | `sub tenant jti` + types |
| `exp` passed | `expired` | 30 s skew | **strict** (skew on `iat`/`nbf` only) | 5 s skew |
| `iat` (or `nbf`) in the future beyond the skew | `immature` | 30 s | 30 s | 5 s |
| Signature valid, wrong `scope` | `scope_denied` | yes | — (handlers decide scope) | — |
| Assertion lifetime above the ceiling | `invalid_claim` | — | — | yes (`exp - iat > max_ttl`) |

### Where Rust deliberately differs from Python

- **`service_eddsa` does not require `tenant`** — exactly as the Python machine-JWT verifier
  (`ServiceJwtVerifier.verify`): hub-api stamps `tenant` only for identities bound to one, and
  non-tenant-aware scopes such as `egress:connect` are minted without it. When `tenant` *is* present
  it must be a non-empty string (`invalid_claim`).
- **`platform_hs256` answers 403 (not 401) for a validly signed token without a usable tenant** —
  the long-standing svc-streaming tenant-isolation behaviour (`rules/security.md`). It is still
  counted as `missing_claim` / `invalid_claim`. Everything else is a generic `401 invalid token`;
  the response body no longer echoes the JWT library's error text.
- **`egress_assertion` has no `iss`/`aud`/`scope`.** It is a per-call grant bound to its signer
  through `sub`, which `egress_proxy` compares with the machine JWT it authenticated on the same
  connection (`SubMismatch`), plus single-use `jti` replay protection. `sub`, `tenant` and `jti`
  must be non-empty — there is no default-tenant fallback anywhere in the Rust verifiers.
- **`aud` as a list** is accepted when it contains the expected audience, and the returned
  `Claims.aud` / `ServiceClaims.aud` is that expected audience.
- **No JWT verifier in Rust falls back to a default tenant.** The `"global"` literals that remain in
  `core/` are not verification defaults: `svc_ingest`'s `RUNNER_TENANT_SLUG` default,
  and the tenant-wide spine activation scope `svc_process` / `svc_action` hardcode (`TODO(M3+)`
  in `svc_action/src/lib.rs`) — both are stream/runner scoping, derived from no token.

## Telemetry

OpenTelemetry **API** only (`opentelemetry` 0.32, `metrics` feature); the destination is the
standard `OTEL_EXPORTER_OTLP_*` configuration each service's telemetry bootstrap already installs
as the global meter provider. With no provider installed the instruments are no-ops and
verification is unaffected — a telemetry failure can never change a verdict.

| Instrument | Type | Labels |
|---|---|---|
| `waddles_jwt_verifications_total` | counter, unit `{verification}` | `verifier`, `alg`, `outcome` |
| `waddles_jwt_verification_seconds` | histogram, unit `s` | `verifier`, `alg` |

Names, units and label names are identical to the Python instruments, so one dashboard covers
both. `alg` is the lower-cased header algorithm from the same closed set (`hs256 … eddsa`, `none`,
`absent`, `other`) and `outcome` is `ok` or one of the reasons above, so an attacker-chosen `alg`
can never create a new label value. `verifier` adds one Rust-only value, `egress_assertion`.

Histogram buckets are explicit and second-scaled
(`0.00001 … 2.5`, see `LATENCY_BOUNDARIES_SECONDS`): the OTel defaults are millisecond-scaled and
would put every sub-millisecond verification into the first bucket. **Follow-up for Python:** the
`flask_core` histogram currently uses the SDK defaults; set the same boundaries there
(`explicit_bucket_boundaries_advisory`) so the merged series share buckets.

Counting rules: exactly one sample per verification. A request without a bearer token is not a
verification. `egress_proxy::assertion::verify` (the best-effort *audit-context* path in
`proxy::handle_inner`) reports nothing — the same token is verified authoritatively once by
`verify_from_header`, and counting both would inflate the stream.

The Prometheus `/metrics` scrape surface (`:9090`) does **not** carry these instruments; OTLP is
the single transport, matching Python.

## Logging

Rejections log one structured event from a closed vocabulary — `JWT rejected` with `verifier`,
`reason`, `alg` — at ERROR (alg / header / kid / signature / claim attacks), WARN (expired,
garbage) or ERROR with `severity=critical` (`no_key`). Successful verifications log at DEBUG only.
The token, its claims, header values, key material and the JWT library's error text are **never**
logged: every logging and metric entry point takes `&'static str`, so a dynamic string cannot be
passed by accident, and regression tests assert marker strings planted in the token never reach the
log output.

## Layout

```
core/service_auth/src/jwt_hardening.rs   canonical Rust implementation (header vetting, vocab,
                                         JwtMetrics, report_outcome); service_eddsa verifier in lib.rs
core/service_auth/src/test_support.rs    in-memory metric capture (cfg(test) / `test-support` feature)
core/egress_assertion/src/lib.rs         verify_with_key: pinned EdDSA, claim shape, closed `reason`s
core/egress_proxy/src/assertion.rs       header vetting + kid lookup + report (verifier=egress_assertion)
core/svc_streaming/src/http/auth.rs      platform_hs256 verifier
core/svc_streaming/src/http/auth/hardening.rs   byte-for-byte MIRROR of jwt_hardening.rs
```

`svc_streaming` mirrors the module instead of depending on `service_auth` because its Docker build
context is its own directory (a path dependency would need a context change and pull `reqwest` and a
second `jsonwebtoken` major into the binary). Drift is guarded: a unit test in `svc_streaming`
compares the production code of the two files and fails on any difference, and the golden vectors
are the same tests in both. **Follow-up:** publish one implementation as a penguin-libs crate and
delete the copy.

## Verifying

```bash
# per crate (each is its own workspace, run from its directory)
cargo clippy --all-targets --locked -- -D warnings && cargo fmt --check && cargo deny check
cargo test --locked                       # includes the global-meter-provider integration tests
cargo llvm-cov --locked --fail-under-lines 90
```

`egress_proxy/tests/jwt_metrics.rs` and `svc_streaming/tests/jwt_metrics.rs` install a capturing
OTel provider as the process-wide one and drive the public entry points (and, for svc-streaming, the
real router), asserting the exact label names, unit and one-sample-per-verification rule.

## Known gaps (not in this change)

- `JwksTrustBundle` refreshes the JWKS on **every** unknown `kid`. The `kid` charset check now stops
  hostile values, but a flood of well-formed random `kid`s still triggers a hub-api fetch per
  request; it needs a negative cache / minimum refresh interval.
- `svc_streaming`'s `X-Service-Key` check (`ServiceKey`) compares with `!=` (not constant time) and
  accepts an empty header when `SERVICE_API_KEY` is configured as the empty string. Not a JWT path,
  so deliberately left out of this change; flagged in the PR for a follow-up.
- `JWT_JWKS_URL` (RS256) in `svc_streaming` is parsed but not wired; when it is, it becomes a second
  verifier with its own `verifier` label rather than widening `platform_hs256`.
- Phase 1+ (JWKS endpoint, ES256 minting, retire HS256) is unchanged from the Python document.
