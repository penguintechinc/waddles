# Edge-Reshape Chunk 2, Phase A: Connection Store Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing provider-connection store so it can hold external-HTTP / AWS-Lambda / GCP-Function connections (structured secrets, sealed at rest with row-bound AAD), close the two security findings on it (HIGH-2 static-shared-secret token vending, M4 no-AAD ciphertext), and ship one shared Rust resolver client that svc-action (B1) and svc-ingest (B2) call to fetch a connection's endpoint + creds per send/connect, never cached.

**Architecture:** hub-api already owns `platform_connections` (+ `community_connection_access` grants) behind `ProviderConnectionService.resolve()` and a machine-JWT internal route (`connections:resolve`, `system` tenant). Phase A (1) adds an AAD-bound, versioned seal primitive and routes every reader through it; (2) adds a `connection_kind` int-enum + `public_id` + `sealed_secret` to `platform_connections` and three static-secret adapters registered in the existing `AdapterRegistry`; (3) makes the resolve token **community-bound** (issuer embeds a `community` claim; route rejects a mismatch) and SPIFFE-ready (accepts an allow-listed X.509-SVID peer identity as first-class); (4) cuts the legacy `X-Service-Key` token endpoint over to the same guard; (5) adds a Rust `connection_resolver` crate modeled on `HubScopeSource`. B1/B2 consume only the crate's public interface.

**Tech Stack:** Python 3.13 + Quart + penguin-dal + alembic + `cryptography` (hub-api, `libs/flask_core`); Rust (Tokio, reqwest =0.12.28 rustls, `core/service_auth`) for the resolver crate; Helm (`k8s/helm/waddlebot`).

**Spec:** `docs/superpowers/specs/2026-10-23-edge-reshape-gateway-dispatcher-design.md` (§6.3 transport/connection library, §6.4 PII scope table, §10 Q-A connection store). Recon: memory `edge-reshape-chunk2-recon`.

## Global Constraints

- **penguin-spine is pinned (rev `ce638ec`, `=0.2.0`) — DO NOT bump, no `:gateway` stage. Do NOT rename the `waddles.ingest.*` app_id namespace.** This plan touches no envelope/spine/`StageEnvelope` code and no svc-ingest→svc-gateway rename.
- **Security-sensitive: Python 3.13 or Rust only, never Go.**
- **Service-to-service auth = SPIFFE-ready + OIDC machine JWT (Ed25519, short-lived, signed). NO static shared secret on any connection-secret path** — `X-Service-Key` is removed from the token endpoint with no fallback. Community-bound resolve tokens live ≤ 300 s (well under the 1 h ceiling). Claims here carry only numeric ids (non-sensitive) → JWS suffices; if a future claim carries sensitive data, sign-then-encrypt (JWS inside JWE, RFC 8725 §3.4), never the reverse.
- **Credentials are NEVER cached on the data plane** — fetched per send/connect; the resolver crate has no credential cache and `ResolvedConnection` is not `Clone`. (Caching the machine *bearer JWT* is allowed — it is not a connection credential.)
- **Entity identity:** numeric PK internal; `public_id` UUID across every boundary that is not the data-plane internal path; int enum codes for kind/secret-scheme (`connection_kind SMALLINT`), never strings. Connections are referenced by numeric `connection_id` + numeric `community_id` on the data-plane internal path (spec Identity/keying; recon "numeric connection id"). The pre-existing string columns (`resource_type`, `status`) are legacy — do not extend them as the new type authority.
- **Every response through an explicit schema/DTO** (`@validate_response` on a Pydantic/dataclass model); never a raw row / `**row.__dict__`. Explicit column lists on every `platform_connections` select (already the store convention).
- **penguin-dal for DB; per-service DB accounts** — data-plane roles keep `privileges: []` on these tables (`config/postgres/rbac-matrix.yaml`); the data plane reaches secrets only through the resolve endpoint.
- **PII:** connection secrets are not PII, but connection records, logs, spans, metric labels never carry a raw secret, endpoint query string, username, or message text — numeric ids + provider code + outcome only. The resolver client never detokenizes and has no PII scope (scope-NONE adapters, spec §6.4).
- **No static key material in a distributed build; secrets never CLI args; masked in logs.**
- **Python regex (ReDoS):** every validation regex is `re.compile`d at module level and used with `.fullmatch()`, never `.match()` (Python `$` matches before a trailing `\n`).
- **90%+ coverage; real-path tests (live Postgres, live Quart app, real Ed25519 issuer, real Rust↔hub-api round trip) at the service boundary; no-stubs / fail-loud** — an unconfigured verifier, unusable key, or unknown kind is a loud error, never a fallback. No `.unwrap()`/`.expect()` outside tests in Rust; `cargo fmt`/`clippy`/`deny` clean; exact `=` pins, no PRC crates.
- **OTel logs + metrics + traces** on every new path (hub-api: the existing `provider_connections/telemetry.py` span/counter helpers; Rust: `tracing` + a `ResolverMetrics` trait like `ResolverMetrics` in `scope_resolver.rs`); OTLP from `OTEL_*` env; a dead exporter never fails a resolve. Python logs via `log_exc_safe`/`log_failure` (type only, never message text from a crypto/secret failure).
- **Base branch for execution:** fresh worktree off `origin/release/v3.0.X` tip `cfa003fe` (the orchestrator checkout is stale). Re-verify every path/line below with Read/Grep first. Alembic: chain the new revision off the head at execution time (`0062_app_commands_registry` at plan time); run `bash scripts/ci/check-alembic-single-head.sh`.
- Every new feature path is behind a PostHog flag defaulted OFF: `waddles.integrations.static_connections` (gates creating the 3 new kinds via the admin API; Professional tier like `provider_connections`). The security fixes (AAD, HIGH-2) are **not** flag-gated — core mechanisms; kill-switch pattern not applicable (a rollback to no-AAD / static key is the defect).

## Review Focus

- **Cross-community / cross-row substitution:** a ciphertext copied to another row, or a community-A-bound token used to resolve community B's connection, must fail closed. (Task 1 `ciphertext_swapped_between_rows_fails_open`; Task 3+4 `token_bound_to_a_cannot_resolve_b`.)
- **Legacy no-AAD rows after the AAD cutover:** every reader (`credential_resolver`, `community_connections`, provider `resolve`, `test_platform_connection`) must go through the one versioned open — a stray direct `decrypt_value(` call strands or mis-decrypts v0 rows. (Task 1 `no_direct_decrypt_value_outside_crypto_modules` grep-test + `reseal_reports_examined_count`.)
- **Static shared secret still accepted anywhere:** `X-Service-Key` (or missing/blank verifier → open) must not unlock a token on any path, including the legacy `/connections/<provider>/token` route. (Task 5 `service_key_header_alone_is_401` + `unconfigured_verifier_is_503_not_open`.)
- **Secret leakage of the new structured secret:** `Debug`/`Display`/serde of Rust secret types, hub-api logs/spans/error bodies, and a data-plane cache. (Task 7 `debug_output_redacts_every_secret_field`, `two_resolves_make_two_hub_calls`; Task 4 `resolve_error_bodies_carry_no_secret`.)
- **Hostile endpoint/field input at write time:** `http://`, `userinfo@host`, private/loopback/link-local IP literals, trailing `\n`, oversize/odd-unicode region/function names, unknown `connection_kind`. (Task 2 `endpoint_validation_rejects_*` table incl. `"https://ok.example\n"`.)

---

### Task 1: AAD-bound, versioned seal primitive (M4) + reseal job

**Files:**
- Modify: `hub_api/services/platform_integrations_crypto.py` (`encrypt_token`/`decrypt_value` gain `aad`), `hub_api/services/provider_connections/vault.py` (`TokenVault.seal/open` gain `aad`), `hub_api/services/community_connections.py` (`_sync_upsert`, `get_decrypted_tokens`, `_sync_store_refreshed`), `hub_api/services/provider_connections/service.py` (every `seal`/`open` call: `:193,:346,:508,:519,:531,:572`), `hub_api/services/credential_resolver.py` (`:174,:282` decrypt sites)
- Create: `alembic/versions/0063_seal_aad_version.py` (re-point `down_revision` at head), `scripts/reseal_connections.py` + Makefile target `reseal-connections`
- Test: `hub_api/tests/test_platform_integrations_crypto.py`, `hub_api/tests/provider_connections/test_vault.py`, `hub_api/tests/provider_connections/test_at_rest_and_secrecy.py`, new `hub_api/tests/test_seal_callers_guard.py`

**Interfaces:**
- Consumes: existing wire format `base64(iv(12)||ct||tag)`, key `CREDENTIAL_ENCRYPTION_KEY`.
- Produces:
  - `encrypt_token(plaintext: str, *, aad: bytes | None = None) -> str` / `decrypt_value(encrypted_b64: str, *, aad: bytes | None = None) -> str` (AAD passed straight to `AESGCM`; wire format unchanged).
  - `def connection_aad(*, community_id: int, provider: str) -> bytes` → `f"{community_id}:{provider}".encode()` — **M4 AAD for `platform_integrations` community_oauth rows (exactly `community_id:provider`).**
  - `def platform_connection_aad(*, public_id: UUID, platform: str) -> bytes` → `f"conn:{public_id}:{platform}".encode()` — **ruling:** `platform_connections` rows are tenant-owned and shared across communities via grants, so `community_id` is not a stable row identity there; the immutable `public_id` is.
  - `TokenVault.seal(plaintext: str, *, aad: bytes) -> str`, `TokenVault.open(ciphertext: str, *, aad: bytes | None, seal_version: int) -> str` (`seal_version` 0 = legacy no-AAD, 1 = AAD-bound; `open` ignores `aad` when 0).
  - Columns: `platform_integrations.seal_version SMALLINT NOT NULL DEFAULT 0`, `platform_connections.seal_version SMALLINT NOT NULL DEFAULT 0` (all new writes = 1).
  - `scripts/reseal_connections.py` → prints `examined=N resealed=M failed=K`, exit non-zero if `examined == 0` on a non-empty table or `failed > 0`.

- [ ] **Step 1: Write failing tests**

```python
# regression: M4 — ciphertext bound to its row
def test_aad_roundtrip(): ...                       # encrypt(aad=b"7:twitch") -> decrypt(aad=b"7:twitch") == plaintext
def test_wrong_aad_fails_closed(): ...              # decrypt(aad=b"8:twitch") raises PlatformCredentialCryptoError
def test_missing_aad_on_v1_fails_closed(): ...      # decrypt(aad=None) of an AAD ciphertext raises
async def ciphertext_swapped_between_rows_fails_open(pg): ...  # swap access_token of community 1 & 2 rows in platform_integrations; get_decrypted_tokens raises PlatformCredentialCryptoError for BOTH (name keeps spec phrase: asserts it does NOT return plaintext)
async def legacy_v0_row_still_opens_then_reseals_on_resolve(pg): ...  # seal_version=0 row opens with aad ignored; after resolve it is seal_version=1 and open(aad=...) succeeds
async def new_writes_are_version_1(pg): ...         # upsert_connection + provider create -> seal_version == 1
def test_vault_open_v0_ignores_aad(): ...
def no_direct_decrypt_value_outside_crypto_modules(): ...  # test_seal_callers_guard.py: AST/grep hub_api/ — only platform_integrations_crypto.py, vault.py (+ tests) may call decrypt_value/encrypt_token; otherwise FAIL; asserts scanned_file_count > 0
async def reseal_reports_examined_count(pg): ...    # 3 v0 rows -> examined=3 resealed=3; zero-row table with rows expected -> non-zero exit
async def reseal_tamper_row_counts_failed(pg): ...  # corrupt one row -> failed=1, others still resealed, exit != 0
```

- [ ] **Step 2: Run to verify fail** — `cd hub_api && pytest tests/test_platform_integrations_crypto.py tests/provider_connections/test_vault.py tests/test_seal_callers_guard.py -q` → FAIL (`aad` kwarg unknown).
- [ ] **Step 3: Implement** — add `aad` pass-through in `platform_integrations_crypto.py`; add `connection_aad`/`platform_connection_aad`; change `PlatformKeyVault.seal/open` signatures; thread `aad` + `seal_version` through the six `service.py` call sites and the three `community_connections.py` functions (selects add `seal_version` to the explicit column list; writes set `seal_version=1`); `credential_resolver.py` opens via the vault (route its direct `decrypt_value` calls through `PlatformKeyVault.open` with the row's `seal_version`).
- [ ] **Step 4: Write migration `0063_seal_aad_version.py`** — `ALTER TABLE ... ADD COLUMN IF NOT EXISTS seal_version SMALLINT NOT NULL DEFAULT 0` on both tables (metadata-only), `COMMENT ON COLUMN` explaining 0=legacy/1=AAD; matching `downgrade()`; add an `alembic/tests/test_0063_*.py` upgrade/downgrade test.
- [ ] **Step 5: Write `scripts/reseal_connections.py`** — batches by id (`LIMIT 200`), opens v0, re-seals under AAD in the same transaction (`SELECT ... FOR UPDATE`), prints the examined/resealed/failed counts, `set -euo pipefail`-equivalent exit codes; add `reseal-connections` Makefile target.
- [ ] **Step 6: Run** `make test` hub-api crypto/provider/community-connection suites + `alembic` tests → PASS; confirm the guard test fails once when a stray `decrypt_value(` is added (temporary edit, revert).
- [ ] **Step 7: Commit** — `fix(hub-api): bind connection ciphertext to its row with AAD (M4) + versioned reseal`

**Deliverable:** every connection secret written is AAD-sealed; v0 rows still open and are re-sealed lazily + by `make reseal-connections`; no reader bypasses the versioned open.

---

### Task 2: Connection kinds + static-secret adapters (store extension)

**Files:**
- Create: `alembic/versions/0064_connection_kinds.py` (chain after Task 1's revision), `hub_api/services/provider_connections/static_secret.py` (adapter family + secret/endpoint validators), `hub_api/services/provider_connections/kinds.py` (enum + codes)
- Modify: `hub_api/services/provider_connections/registry.py` (3 registry lines), `hub_api/services/provider_connections/store.py` (explicit column list + `sealed_secret`, `public_id`, `connection_kind`), `hub_api/services/provider_connections/service.py` (create/replace path for static kinds), `hub_api/blueprints/v1/provider_connections.py` (flag-gate the 3 providers; DTO gains `kind`, `publicId`)
- Test: `hub_api/tests/provider_connections/test_static_secret_adapters.py`, `test_store.py`, `test_api.py`, `alembic/tests/test_0064_connection_kinds.py`

**Interfaces:**
- Consumes: `ProviderAdapter` Protocol / `AdapterSpec` (`adapter.py:72,:156`), `AdapterRegistry.register`, Task 1 `platform_connection_aad`.
- Produces:
  - **`ConnectionKind` int enum** — `PLATFORM_OAUTH = 1`, `EXTERNAL_HTTP = 2`, `AWS_LAMBDA = 3`, `GCP_FUNCTION = 4`; `ConnectionKind.from_code(int) -> ConnectionKind` raises `UnknownConnectionKind`.
  - **`SecretScheme` int enum** — `BEARER = 1`, `BASIC = 2`, `API_KEY_HEADER = 3`, `AWS_ACCESS_KEY = 4`, `GCP_SERVICE_ACCOUNT = 5`.
  - Table columns on `platform_connections`: `public_id UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE`, `connection_kind SMALLINT NOT NULL` (backfilled `1` for every existing row; `CHECK (connection_kind BETWEEN 1 AND 4)`), `sealed_secret TEXT NULL` (AES-GCM of a JSON secret document, AAD = `platform_connection_aad`), and `resource_type` CHECK extended with `'external_http','aws_lambda','gcp_function'`. For static kinds: `platform` = provider id (`external_http`/`aws_lambda`/`gcp_function`), `resource_id = public_id::text`, non-secret endpoint facts in `resource_metadata` (`url` | `region`+`function` | `project`+`region`+`function`), `access_token` NULL.
  - Adapters `ExternalHttpAdapter`, `AwsLambdaAdapter`, `GcpFunctionAdapter` (each `ProviderAdapter`, `spec.refreshable=False`, `capabilities={"dispatch"}`); `validate_credentials` is **structural only** (no outbound call at write time — the host policy / SSRF DNS-pin is B1's send-time concern); `refresh` raises `RefreshNotConfigured`.
  - `validate_endpoint_url(raw: str) -> str` (https only, no userinfo, no fragment, host not an IP literal in private/loopback/link-local/CGNAT ranges, ≤2048 chars, `fullmatch` against a module-level pattern) and `validate_secret_document(scheme: SecretScheme, doc: Mapping[str, Any]) -> dict[str, str]` (exact required keys per scheme, max lengths, no extra keys).

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("bad", ["http://x.example", "https://u:p@x.example", "https://127.0.0.1/x",
    "https://10.0.0.5/x", "https://[::1]/x", "https://169.254.169.254/", "https://ok.example\n",
    "https://ok.example/#frag", "https://" + "a"*2100 + ".example"])
def endpoint_validation_rejects_hostile_urls(bad): ...     # raises InvalidEndpoint
def endpoint_validation_accepts_https_dns_name(): ...
@pytest.mark.parametrize("region", ["us-east-1\n", "US_EAST", "", "x"*40])
def aws_region_validation_rejects(region): ...
def secret_document_rejects_unknown_and_missing_keys(): ...  # per SecretScheme
def connection_kind_from_unknown_code_raises(): ...
async def create_static_connection_seals_secret_with_row_bound_aad(pg): ...  # sealed_secret != plaintext JSON; opens only with its own public_id AAD
async def static_connection_secret_never_in_admin_dto_or_list(pg): ...       # asserts exact DTO field set (regression field-set test)
async def migration_backfills_kind_1_for_existing_rows(pg): ...
async def creating_static_kind_requires_flag(client): ...                    # flag OFF -> 402/404 per existing gating style
async def unknown_provider_still_unsupported(client): ...
```

- [ ] **Step 2: Run to verify fail** — `pytest hub_api/tests/provider_connections -q -k "static or kind or endpoint"` → FAIL.
- [ ] **Step 3: Write migration `0064`** — add the three columns, `connection_kind` backfill (`UPDATE ... SET connection_kind=1` then `SET NOT NULL`), replace the `resource_type` CHECK (drop/recreate by name from 0035), `CREATE UNIQUE INDEX ... (public_id)`; downgrade removes static-kind rows' guard (refuse downgrade if any `connection_kind <> 1` rows exist — fail loud) then drops columns. Alembic test: single head, up/down, backfill.
- [ ] **Step 4: Implement `kinds.py` + `static_secret.py`** (module-level compiled patterns, `.fullmatch()`); register the 3 adapters in `registry.py`; extend `store.py` explicit column lists and `insert_connection` for `connection_kind`/`sealed_secret`; `service.py` create path seals the JSON secret doc with `platform_connection_aad(public_id=...)` (public_id generated app-side before insert so the AAD is known at seal time).
- [ ] **Step 5: Wire API** — flag `waddles.integrations.static_connections` (default OFF) on create for the 3 providers; DTO schema gains `kind: int`, `publicId: str`; no secret field in any admin response.
- [ ] **Step 6: Run** hub-api provider_connections suite + alembic tests → PASS; `spectral lint openapi/v1.yaml` after regenerating the spec.
- [ ] **Step 7: Commit** — `feat(hub-api): extend connection store with external/lambda/gcp kinds + sealed structured secrets`

**Deliverable:** an admin can create a flag-gated static-secret connection of any of the 3 kinds; the secret is sealed at rest (row-bound AAD) and never returned by any admin route.

---

### Task 3: Community-bound, SPIFFE-ready machine auth (issuer + guard) — HIGH-2 part 1

**Files:**
- Modify: `libs/flask_core/flask_core/service_jwt.py` (`ServiceJwtIssuer.issue_with_claims` extra claims; `ServiceIdentity` scope policy), `hub_api/blueprints/service_jwt_bp.py` (accept `community_id`/`connection_id` in body), `hub_api/services/machine_auth.py` (`bind_community` option + SVID path), hub-api config for `SPIFFE_PEER_ID_HEADER`
- Test: `libs/flask_core/tests/test_service_jwt_bound.py`, `hub_api/tests/test_service_jwt_token_endpoint.py`, new `hub_api/tests/test_machine_auth_bound.py`

**Interfaces:**
- Consumes: `ServiceJwtIssuer.issue_with_claims(service_id, scope, *, ttl_seconds)` (`service_jwt.py:287`), `SYSTEM_TENANT`, `machine_authenticated(required_scope, *, unconfigured_event, rejected_event, wrong_tenant_event)`.
- Produces:
  - `ServiceJwtIssuer.issue_with_claims(service_id, scope, *, ttl_seconds, extra_claims: Mapping[str, int] | None = None)` — only allow-listed integer claim names `community`, `conn`; any other key raises `ValueError`. `BOUND_TOKEN_TTL_SECONDS = 300`.
  - `POST /internal/service-token` body `{"scope": "connections:resolve", "community_id": <int>, "connection_id": <int|omitted>}`: for scope `connections:resolve`, `community_id` is **required** (400 otherwise); other scopes ignore/reject the fields. New scope **`connections:list`** (unbound, `system` tenant, never returns secrets).
  - `machine_authenticated(..., bind_community: bool = False)` → when True, handler kwarg `community_id` must equal `int(claims["community"])` (and `connection_id` equal `claims["conn"]` when that claim is present), else **403**; missing `community` claim on a `bind_community` route → 401.
  - SPIFFE-first path: `machine_authenticated` also accepts a verified X.509-SVID peer identity, read from a header named by `SPIFFE_PEER_ID_HEADER` **only when** `SPIFFE_PEER_TRUST=true` (the request arrived on the mTLS listener, header stripped at the edge) — maps `spiffe://penguintech.io/<env>/<service>` through the same allow-listed `ServiceIdentity` (scope policy + tenant), with the binding supplied by the `X-Connection-Community` request parameter checked against the route's `community_id`. Default OFF → header ignored. Any malformed/unknown SPIFFE id → 401.

- [ ] **Step 1: Write failing tests**

```python
def issue_bound_token_carries_community_claim(): ...          # claims["community"]==7, ttl<=300
def issue_rejects_unknown_extra_claim(): ...                  # extra_claims={"tenant": 1} -> ValueError
async def token_endpoint_requires_community_for_resolve_scope(client): ...   # 400
async def token_endpoint_unbound_list_scope_never_has_community(client): ...
async def token_bound_to_a_cannot_resolve_b(client): ...      # route community_id=B with token community=A -> 403
async def conn_claim_mismatch_is_403(client): ...
async def missing_community_claim_on_bound_route_is_401(client): ...
async def wrong_tenant_still_403(client): ...                 # existing behavior preserved
async def spiffe_header_ignored_when_peer_trust_off(client): ...  # header alone -> 401
async def spiffe_identity_accepted_when_peer_trust_on(client): ...  # allow-listed id -> 200 path; unknown id -> 401
async def unconfigured_verifier_is_503(client): ...
```

- [ ] **Step 2: Run to verify fail** — `pytest libs/flask_core/tests/test_service_jwt_bound.py hub_api/tests/test_machine_auth_bound.py -q` → FAIL.
- [ ] **Step 3: Implement issuer extra-claims + token endpoint binding** (allow-list of claim names, ttl clamp, audit-log `jti/exp/tenant/community` — ids only).
- [ ] **Step 4: Implement `bind_community` in `machine_authenticated`**; failures use the existing generic bodies (never say which check failed).
- [ ] **Step 5: Implement SVID path** behind `SPIFFE_PEER_TRUST`; fail closed on any parse error; add telemetry counter `machine_auth_total{mechanism="jwt|svid",outcome}`.
- [ ] **Step 6: Run** the three suites → PASS.
- [ ] **Step 7: Commit** — `feat(auth): community-bound machine JWT + SVID-ready guard for connection resolve (HIGH-2)`

**Deliverable:** a resolve token is bound to one community (and optionally one connection); the guard rejects any mismatch and accepts an allow-listed SVID identity without any static secret.

---

### Task 4: Resolve v2 — static kinds, bound guard, `connections:list` split

**Files:**
- Modify: `hub_api/blueprints/v1/provider_connections.py` (`internal_resolve`, `internal_list`, DTOs), `hub_api/services/provider_connections/service.py` (`resolve` returns structured secret for static kinds), `openapi/v1.yaml`
- Test: `hub_api/tests/provider_connections/test_api.py`, `test_dataplane.py`, `test_isolation.py`, `test_chart_service_identity_grant.py`

**Interfaces:**
- Consumes: Task 2 kinds/`sealed_secret`; Task 3 `bind_community=True`.
- Produces — **wire contract B1/B2 depend on** (`POST /api/v1/internal/communities/<community_id>/provider-connections/<connection_id>/resolve`, scope `connections:resolve`, `bind_community=True`, `Cache-Control: no-store`):

```json
{ "success": true, "connectionId": 12, "communityId": 7, "provider": "aws_lambda", "kind": 3,
  "accessToken": null, "expiresAt": null,
  "endpoint": { "url": null, "region": "us-east-1", "function": "arn-or-name", "project": null },
  "secret": { "scheme": 4, "fields": { "accessKeyId": "...", "secretAccessKey": "...", "sessionToken": null } },
  "metadata": { } }
```
  `kind=1` rows keep `accessToken` (existing contract unchanged), `secret=null`. `GET .../provider-connections` (list) now requires scope `connections:list` (unbound; no secrets; items gain `kind`). Errors: 404 not found / not granted to *this* community, 409 `{"error":"not_usable","reason":"pending_approval|revoked_grant|disabled|connection_expired|..."}`, 401/403 per guard, 503 vault unavailable — bodies carry fixed codes only.

- [ ] **Step 1: Write failing tests**

```python
async def resolve_static_kind_returns_structured_secret(client, pg): ...    # exact field set asserted for each of 3 kinds
async def resolve_oauth_kind_contract_unchanged(client, pg): ...            # twitch response identical to golden fixture
async def resolve_requires_grant_for_this_community(client, pg): ...        # connection granted to A only; token bound B -> 404 (not 403: no existence oracle)
async def resolve_disabled_or_pending_grant_is_409_with_reason(client, pg): ...
async def resolve_error_bodies_carry_no_secret(client, pg): ...             # vault corrupt/unavailable -> fixed code, no ciphertext/plaintext/key text
async def resolve_response_is_no_store(client, pg): ...
async def list_requires_list_scope_resolve_scope_is_401(client): ...
async def list_never_returns_secret_fields(client, pg): ...
async def twitch_binding_metadata_has_channel_and_bot_login(client, pg): ...   # list AND resolve for a twitch row: metadata["channel"] == stored resource_metadata["channel_login"], metadata["bot_login"] == stored["bot_login"]; both non-empty
async def twitch_binding_missing_channel_or_bot_login_is_excluded_loudly(client, pg): ...  # row lacking either fact is not listed as usable; hub logs connection_id + missing key (no values); never a partial binding
async def metadata_keys_are_allowlisted_per_provider(client, pg): ...   # keys outside the contract table below never serialised
async def resolve_writes_golden_fixture(...): ...                           # writes hub_api/tests/fixtures/resolve_v2_{oauth,http,lambda,gcp}.json — consumed verbatim by the Rust crate's contract test (Task 7)
```

- [ ] **Step 2: Run to verify fail** — `pytest hub_api/tests/provider_connections -q` → FAIL.
- [ ] **Step 3a: Metadata contract (owned by this task)** — `BindingSummary.metadata` / resolve `metadata` is a flat `string->string` projection of `resource_metadata`, allow-listed per provider; B2 depends on the twitch row:

| provider (kind) | keys guaranteed on every usable binding | source (`resource_metadata`) |
|---|---|---|
| `twitch` (1) | `channel` (channel login to join), `bot_login` (bot nick), plus passthrough `channel_login`, `channel_id` | `channel_login` (aliased to `channel` at the DTO layer, stored key unchanged), `bot_login` |
| `external_http` (2) | `url` host only is in `endpoint`; metadata empty | n/a |
| `aws_lambda` (3) / `gcp_function` (4) | none (facts ride in `endpoint`) | n/a |

  Implement the alias in the DTO builder (`_dataplane_dto` / `_resolved_dto`), not in storage; list/resolve skip-and-log a twitch row missing either key.
- [ ] **Step 3: Implement** — static-kind branch in `service.resolve` (open `sealed_secret` via vault with `platform_connection_aad`, parse JSON, map to `secret.fields` camelCase per scheme; no refresh path; `expiresAt` from the row); route decorators `machine_authenticated("connections:resolve", bind_community=True, ...)` and `("connections:list", ...)`; `@validate_response` DTOs; regenerate `openapi/v1.yaml`.
- [ ] **Step 4: Run** provider_connections suite + spectral → PASS. Commit golden fixtures.
- [ ] **Step 5: Commit** — `feat(hub-api): resolve v2 returns endpoint+structured secret, community-bound, list scope split`

**Deliverable:** a live hub-api resolves a static-secret connection for the bound community only, with a stable, golden-fixture-pinned wire contract.

---

### Task 5: Cut the legacy `X-Service-Key` token endpoint over (HIGH-2 part 2)

**Files:**
- Modify: `hub_api/blueprints/v1/community_connections.py` (`internal_get_token`, `:338-406`), `libs/waddle_transports/waddle_transports/community_credentials.py` (client: mint a community-bound machine JWT instead of sending the service key), `openapi/v1.yaml`
- Test: `hub_api/tests/test_community_connections_api.py`, `libs/waddle_transports/tests/test_community_credentials.py`

**Interfaces:**
- Consumes: Task 3 `machine_authenticated(..., bind_community=True)`; scope `connections:resolve`.
- Produces: `GET /api/v1/internal/communities/<id>/connections/<provider>/token` guarded by machine JWT (scope `connections:resolve`, `system` tenant, bound to `<id>`); `is_valid_service_key` is no longer imported by this module; `waddle_transports.community_credentials` obtains its bearer from `MachineJwtClient`-equivalent Python helper (bootstrap against `/internal/service-token` with `community_id`).

- [ ] **Step 1: Write failing tests**

```python
async def service_key_header_alone_is_401(client): ...             # regression: HIGH-2 — valid X-Service-Key, no Bearer -> 401
async def valid_service_key_plus_bearer_for_other_community_is_403(client): ...
async def unconfigured_verifier_is_503_not_open(client): ...
async def token_for_matching_community_returns_tokens_and_refreshes(client, pg): ...   # existing refresh-skew behavior retained
async def endpoint_response_is_no_store(client): ...
def no_service_key_import_in_community_connections(): ...          # grep guard; asserts scanned file exists
async def waddle_transports_client_sends_bearer_not_service_key(): ...   # real aiohttp/httpx round trip against the live Quart test app
```

- [ ] **Step 2: Run to verify fail** — `pytest hub_api/tests/test_community_connections_api.py libs/waddle_transports -q` → FAIL.
- [ ] **Step 3: Implement** the guard swap (delete the `is_valid_service_key` branch — no dual-accept, no fallback); keep body/refresh logic; update `community_credentials.py` + its callers' config (drop `SERVICE_API_KEY` from that path).
- [ ] **Step 4: Enumerate callers** — `git grep -n "connections/.*/token\|community_credentials"`; each one either already uses the helper or is updated in this task; list them in the PR body.
- [ ] **Step 5: Run** suites → PASS; confirm the 401 test fails against the pre-change code (revert-check).
- [ ] **Step 6: Commit** — `fix(hub-api): replace X-Service-Key on connection token endpoint with community-bound machine JWT (HIGH-2)`

**Deliverable:** no connection-secret route in hub-api accepts a static shared secret; the one legacy consumer authenticates with a community-bound machine JWT.

---

### Task 6: Rust community-bound token source (`core/service_auth`)

**Files:**
- Modify: `core/service_auth/src/lib.rs`, `core/service_auth/README.md`
- Test: inline `#[cfg(test)]` in `lib.rs` + `core/service_auth/src/test_support.rs`

**Interfaces:**
- Consumes: `MachineJwtClient` mint flow (`lib.rs:628-720`: bootstrap with projected SA token, `POST {token_endpoint}` `{scope}`), `HttpTimeouts`, `CLOCK_SKEW_SECONDS`.
- Produces:

```rust
/// Mints and caches community-bound machine JWTs (bearer cache only; never connection secrets).
pub struct BoundMachineJwtClient { /* token_endpoint, sa_token_path, scope, http, LRU<(i64,Option<i64>), CachedToken> (cap 256) */ }
impl BoundMachineJwtClient {
    pub fn with_timeouts(token_endpoint: impl Into<String>, sa_token_path: impl Into<String>,
        scope: impl Into<String>, timeouts: HttpTimeouts) -> Result<Self, ServiceAuthError>;
    /// Bearer bound to exactly (community_id, optional connection_id); refreshed within CLOCK_SKEW_SECONDS of exp.
    pub async fn get_token_for(&self, community_id: i64, connection_id: Option<i64>) -> Result<Arc<str>, ServiceAuthError>;
}
```
  The existing unbound `MachineJwtClient` is unchanged (still used for `connections:list`).

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn token_request_body_carries_community_and_connection() {}      // fake token endpoint asserts {"scope":..,"community_id":7,"connection_id":12}
#[tokio::test] async fn distinct_communities_get_distinct_cached_tokens() {}            // 2 mints, not 1
#[tokio::test] async fn same_binding_hits_cache_until_skew() {}                         // 1 mint for N calls; tokio::time paused expiry -> re-mint
#[tokio::test] async fn bootstrap_rejected_maps_to_error_not_panic() {}
#[tokio::test] async fn lru_evicts_beyond_capacity() {}                                  // cap 256, 257th binding evicts oldest; map len <= 256
#[test] fn debug_never_prints_token() {}
```

- [ ] **Step 2: Run to verify fail** — `cargo test -p service_auth bound` (from `core/service_auth`) → FAIL.
- [ ] **Step 3: Implement** `BoundMachineJwtClient` (single-flight per key via a `Mutex<HashMap<key, Arc<Mutex<()>>>>` or `tokio::sync::OnceCell` pattern already used in `scope_resolver.rs`); `tracing` events carry `community_id` only, never the token.
- [ ] **Step 4: Run** `cargo fmt --check && cargo clippy -- -D warnings && cargo deny check && cargo test` → PASS.
- [ ] **Step 5: Commit** — `feat(service_auth): community-bound machine JWT client`

**Deliverable:** `BoundMachineJwtClient::get_token_for(community, conn)` mints/caches bound bearers against the Task 3 endpoint.

---

### Task 7: Rust `connection_resolver` crate (the B1/B2 contract)

**Files:**
- Create: `core/connection_resolver/{Cargo.toml,deny.toml,README.md,src/lib.rs,src/types.rs,src/client.rs,src/error.rs,src/secret.rs,tests/contract.rs}` (new crate rather than extending `core/hub_client`: that crate is tonic/gRPC (`IdentityService`/`KeyService`); the connection endpoints are REST and the established precedent is `HubScopeSource` over `reqwest` — no `tonic-prost-build` weight for consumers)
- Test: `core/connection_resolver/src/*` inline tests; `tests/contract.rs` consumes `hub_api/tests/fixtures/resolve_v2_*.json` via `include_str!("../../../hub_api/tests/fixtures/…")`

**Interfaces:**
- Consumes: Task 4 wire contract + golden fixtures; Task 6 `BoundMachineJwtClient`; `service_auth::MachineJwtClient` (unbound, `connections:list`); `check_transport` rule (plaintext `http://` only in alpha — copy the policy from `scope_resolver.rs:~460`; do not edit svc_ingest in Phase A).
- Produces (public API, exact):

```rust
pub struct ConnectionRef { pub community_id: i64, pub connection_id: i64 }       // numeric ids only
#[derive(Clone, Copy, PartialEq, Eq, Debug)] #[repr(i16)]
pub enum ConnectionKind { PlatformOauth = 1, ExternalHttp = 2, AwsLambda = 3, GcpFunction = 4 }
impl ConnectionKind { pub fn from_code(c: i16) -> Result<Self, ResolveError>; pub fn code(self) -> i16 }

pub struct SecretString(/* zeroize-on-drop, Debug = "[REDACTED]", no Display/Serialize */);
impl SecretString { pub fn expose(&self) -> &str }

pub struct Endpoint { pub url: Option<String>, pub region: Option<String>, pub function: Option<String>, pub project: Option<String> }
pub enum ConnectionSecret {                                                       // from wire secret.scheme int
    Bearer { token: SecretString },                                              // 1
    Basic { username: SecretString, password: SecretString },                    // 2
    ApiKeyHeader { header: String, value: SecretString },                        // 3
    AwsAccessKey { access_key_id: SecretString, secret_access_key: SecretString, session_token: Option<SecretString> }, // 4
    GcpServiceAccount { client_email: String, private_key_pem: SecretString, token_uri: Option<String> },               // 5
    OAuthAccessToken { access_token: SecretString },                             // kind 1 (accessToken field)
}
pub struct ResolvedConnection {                                                  // NOT Clone; Debug redacts secret
    pub connection_id: i64, pub community_id: i64, pub provider: String, pub kind: ConnectionKind,
    pub endpoint: Endpoint, pub secret: ConnectionSecret, pub expires_at: Option<i64> /* epoch secs */, }

pub struct BindingSummary { pub community_id: i64, pub connection_id: i64, pub provider: String, pub kind: ConnectionKind, pub metadata: std::collections::BTreeMap<String, String> }
pub struct BindingPage { pub items: Vec<BindingSummary>, pub next_after: Option<i64> }

pub enum ResolveError { NotFound, NotUsable(NotUsableReason), Auth, Transient(&'static str), CircuitOpen, Malformed(&'static str) }
pub enum NotUsableReason { PendingApproval, RevokedGrant, Disabled, ConnectionExpired, Other }
impl ResolveError { pub fn is_transient(&self) -> bool }   // Transient | CircuitOpen only; Auth/NotFound/NotUsable/Malformed are never retried

pub struct ResolverConfig { pub hub_api_url: String, pub environment: String, pub token_endpoint: String, pub sa_token_path: String,
    pub request_timeout: Duration /*3s*/, pub connect_timeout: Duration /*2s*/, pub breaker_threshold: u32 /*5*/, pub breaker_cooldown: Duration /*10s*/ }
pub trait ResolverMetrics: Send + Sync { fn resolve(&self, kind: ConnectionKind, outcome: &'static str, latency: Duration); fn list(&self, outcome: &'static str); }

#[derive(Clone)] pub struct ConnectionResolver { /* Arc inner: reqwest::Client, BoundMachineJwtClient, MachineJwtClient, breaker */ }
impl ConnectionResolver {
    pub fn new(cfg: ResolverConfig, metrics: Arc<dyn ResolverMetrics>) -> Result<Self, ResolveError>;   // check_transport enforced here
    pub async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError>;           // one authenticated hub call per invocation, no cache
    pub async fn list_bindings(&self, provider: &str, capability: Option<&str>, after: i64, limit: u32) -> Result<BindingPage, ResolveError>;
}
/// Test seam for B1/B2 unit tests (real-path tests use the real client against hub-api).
pub trait ConnectionResolve: Send + Sync { async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError>; async fn list_bindings(&self, provider: &str, capability: Option<&str>, after: i64, limit: u32) -> Result<BindingPage, ResolveError>; }
impl ConnectionResolve for ConnectionResolver { /* delegates */ }
```

- [ ] **Step 1: Write failing tests**

```rust
#[test] fn debug_output_redacts_every_secret_field() {}                           // format!("{:?}") of each ConnectionSecret + ResolvedConnection contains none of the plaintext values
#[tokio::test] async fn two_resolves_make_two_hub_calls() {}                       // local fake hub counts hits == 2 (no credential cache)
#[tokio::test] async fn resolve_sends_bound_bearer_for_that_community() {}         // fake hub asserts Authorization bearer minted for (community,conn)
#[tokio::test] async fn contract_parses_every_golden_fixture() {}                  // resolve_v2_{oauth,http,lambda,gcp}.json -> expected enum variants + endpoint fields (tests/contract.rs)
#[test] fn unknown_kind_or_scheme_code_is_malformed() {}                           // code 9 -> Malformed, never a default
#[tokio::test] async fn status_mapping_table() {}                                  // 404->NotFound, 409+reason->NotUsable(reason), 401/403->Auth, 5xx/timeout->Transient, bad JSON->Malformed
#[tokio::test] async fn auth_and_notfound_are_not_transient() {}                   // is_transient() false; breaker neutral
#[tokio::test(start_paused = true)] async fn breaker_opens_after_threshold_then_half_opens() {}
#[test] fn plaintext_http_hub_url_refused_outside_alpha() {}                       // new() -> Err; alpha ok; https ok
#[tokio::test] async fn list_bindings_uses_unbound_list_scope_and_paginates() {}
#[tokio::test] async fn resolve_error_never_contains_response_body() {}            // hub returns body with secret-looking text on 500 -> Display/Debug of error free of it
#[tokio::test] async fn metrics_emitted_once_per_resolve_without_secret_labels() {}
```

- [ ] **Step 2: Run to verify fail** — `cargo test --manifest-path core/connection_resolver/Cargo.toml` → FAIL (crate missing).
- [ ] **Step 3: Scaffold crate** — `Cargo.toml` (edition 2021, `rust-version` matching `hub_client`, exact `=` pins: `reqwest =0.12.28` rustls/json, `tokio =1.53.3`, `serde`, `serde_json`, `tracing =0.1.44`, `zeroize` exact pin, `service_auth = { path = "../service_auth", version = "=…" }`), `deny.toml` copied from `core/hub_client/deny.toml`; no PRC crates.
- [ ] **Step 4: Implement** types/secret/error (`Debug` manual impls; `SecretString` zeroize on drop), wire DTO structs `#[serde(rename_all="camelCase", deny_unknown_fields = false)]` mapped to the public types with explicit `from_code` validation; `client.rs` (`POST`/`GET` per Task 4, bearer via `BoundMachineJwtClient::get_token_for` / `MachineJwtClient::get_token`, `tracing` span `connection_resolve` with `community_id`,`connection_id`,`kind`,`outcome` only, circuit breaker per Task-7 config).
- [ ] **Step 5: Run** `cargo fmt --check && cargo clippy -- -D warnings && cargo deny check && cargo llvm-cov --fail-under-lines 90` → PASS (single test pass; do not also run plain `cargo test` in CI — CI-diet finding).
- [ ] **Step 6: Commit** — `feat(connection_resolver): shared per-send connection resolver client`

**Deliverable:** a published, tested crate whose public API above is the only thing B1/B2 touch; contract-pinned to hub-api's golden fixtures.

---

### Task 8: Chart grants + live end-to-end (Rust client ↔ real hub-api ↔ Postgres)

**Files:**
- Modify: `k8s/helm/waddlebot/values.yaml` (`serviceJwt.identities.svc-action` add `allowedScopes: ["connections:resolve"]`, tenant `system`; `svc-ingest` gains `"connections:list"`), `k8s/helm/waddlebot/templates/_service_jwt.tpl`, `service-jwt-rbac.yaml`, `values-alpha.yaml`, `config/postgres/rbac-matrix.yaml` (verify no data-plane role gained table privileges), `docs/PROVIDER_CONNECTIONS.md`
- Create: `core/connection_resolver/tests/e2e_live_hub.rs` (`#[ignore]` by default, run by `make test-connection-resolver-live`), `scripts/ci/connection-resolver-live.sh`
- Test: `hub_api/tests/provider_connections/test_chart_service_identity_grant.py`, helm render test

**Interfaces:**
- Consumes: Tasks 1–7.
- Produces: svc-action identity may mint only community-bound `connections:resolve` (no `connections:list`); svc-ingest may mint `connections:list` + bound `connections:resolve`; both rendered only when their respective feature value is enabled (default false) — same gating as the existing `dynamicConnections` grant. Make target `test-connection-resolver-live`.

- [ ] **Step 1: Write failing tests**

```python
def svc_action_identity_has_only_resolve_scope(render): ...        # helm template -> allowedScopes == ["connections:resolve"]
def svc_ingest_identity_gets_list_scope_when_enabled(render): ...
def identities_absent_when_feature_disabled(render): ...
def no_data_plane_role_has_table_privileges_on_platform_connections(): ...   # parses rbac-matrix.yaml; asserts roles_checked > 0
```
```rust
#[tokio::test] #[ignore] async fn live_resolve_each_kind_roundtrip() {}                 // seeds 3 static connections via hub-api admin API, resolves via ConnectionResolver, asserts decrypted fields equal what was stored
#[tokio::test] #[ignore] async fn live_cross_community_resolve_is_not_found() {}        // grant to A only; resolve as B -> ResolveError::NotFound
#[tokio::test] #[ignore] async fn live_tampered_ciphertext_surfaces_fixed_error() {}    // flip a byte in sealed_secret -> hub 5xx/fixed code -> ResolveError::Transient/Malformed, no secret text
#[tokio::test] #[ignore] async fn live_legacy_service_key_is_rejected() {}              // X-Service-Key against both routes -> Auth
```

- [ ] **Step 2: Run to verify fail** — `helm template … | pytest`/`make test-chart` → FAIL.
- [ ] **Step 3: Implement chart + values changes**; update `docs/PROVIDER_CONNECTIONS.md` (new kinds, scopes, AAD, X-Service-Key removal, reseal runbook: run `make reseal-connections` after deploy, expect `failed=0`).
- [ ] **Step 4: Implement the live harness** (`scripts/ci/connection-resolver-live.sh` with `set -euo pipefail`: starts Postgres + hub-api test app, runs the ignored tests, prints counts of tests run — zero run = fail; telemetry assertions: ≥1 log record and ≥1 metric data point from the resolver, per `testing.md`).
- [ ] **Step 5: Run** `make test-chart`, `make test-connection-resolver-live` → PASS with counts printed; run cross-arch note: amd64-only for now (`amd64-only-for-now`).
- [ ] **Step 6: Run full Phase A gate** — `make lint test-security` + hub-api `pytest` + `cargo` for `service_auth`/`connection_resolver`, `no-stubs` gate clean.
- [ ] **Step 7: Commit** — `feat(chart): grant bound resolve/list scopes + live connection-resolver e2e`

**Deliverable:** end-to-end proof, against live hub-api + Postgres, that the Rust client resolves each kind per call, for the bound community only, with no static shared secret anywhere.

---

## Self-Review

- **Spec coverage:** Q-A hub-owned store/sealed/numeric+UUID (T1,T2) · per-send authenticated hub call, never cached (T4,T7) · HIGH-2 SPIFFE-ready + bound (T3,T5,T8) · M4 AAD (T1) · Rust client for B1/B2 (T6,T7) · scope-NONE adapters: client has no detokenize path and no PII scope (T7) · Global Constraints (no spine bump, no `:gateway`, no `waddles.ingest.*` rename) reproduced. The Chunk-1a `route_destination`/`Destination` and `svc_ingest/config.rs` TWITCH_IRC_* seams are deliberately **not** modified — B1/B2 own them (flag `EGRESS_VIA_DISPATCHER_FLAG` untouched).
- **Rulings recorded in-plan:** (a) AAD for `platform_integrations` = `community_id:provider` exactly; for shared tenant-owned `platform_connections` = `conn:{public_id}:{platform}` (community id is not a stable row identity there); (b) new crate not a `hub_client` extension (REST vs gRPC); (c) scope split `connections:list` (unbound) vs `connections:resolve` (community-bound); (d) legacy string columns kept, new type authority is `connection_kind` int; (e) no dual-accept for `X-Service-Key`; (f) JWS-only on resolve token (ids only).
- **Placeholder scan:** no TBD/TODO; every step names exact files/signatures/tests. Test bodies are `...`/`{}` stubs *in the plan only* as assertion specs — implementers write real bodies (no-stubs gate applies to shipped code).
- **Type consistency:** `ConnectionKind` codes 1–4 identical in migration CHECK, Python enum, Rust enum, wire `kind`; `SecretScheme` 1–5 identical in Python enum and Rust `from_code`; `ConnectionRef{community_id,connection_id}` matches route params `<community_id>/<connection_id>`; `seal_version` 0/1 consistent T1↔T2↔T4.
- **Review Focus:** all five pinned to named tests (T1, T3/T4, T5, T7/T4, T2).
- **Proportion:** 8 tasks; migrations 0063/0064 re-pointed at execution-time head.
