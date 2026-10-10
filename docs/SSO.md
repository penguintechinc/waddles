# Enterprise SSO (SAML 2.0, OIDC, Google)

Single sign-on for Waddles tenants, implemented in **hub-api** (`hub_api/services/sso_*.py`,
`hub_api/blueprints/v1/sso.py`, migration `0049_sso_connections`).

| Protocol | Tier | Feature flag (PostHog + license tier, both must pass) |
|---|---|---|
| SAML 2.0 | Enterprise | `waddles.auth.sso_saml` |
| OpenID Connect (any compliant IdP: Okta, Entra ID, Keycloak, Authentik, ...) | Enterprise | `waddles.auth.sso_saml` |
| Google OAuth2 / Workspace | Professional | `waddles.auth.sso_google` |

Both flags default **OFF**. Entitlement is evaluated by the real two-gate
`flask_core.feature_flags.feature_enabled` (PostHog flag **and** tenant tier from the catalog
in `flask_core.tier_catalog`) at connection create/update/enable, when listing login buttons,
at login start and again at login completion — a downgrade or flag-off stops logins
immediately. Disabling and deleting a connection is always allowed.

## How a login works

```
Browser            SPA              hub-api                         IdP
   | click button   |                  |                              |
   |--------------->| GET /auth/sso/options?tenant=acme              |
   |                |<-- [{id, displayName, protocol}] -------------|
   |                | GET /auth/sso/{id}/start                       |
   |                |<-- {redirectUrl} + Set-Cookie wb_sso_bind -----|   state in Redis (single use)
   |<-- navigate ---|                                                |
   |------------------------------------------------------------->  authenticate
   |<-- OIDC: GET /auth/sso/{id}/callback?code&state  (cookie sent)  |
   |<-- SAML: POST /auth/sso/{id}/acs  SAMLResponse+RelayState       |
   |                  validate (signature/ID token, audience, nonce/InResponseTo,
   |                  time window, replay) -> JIT user -> tenant session JWT
   |<-- 303 {frontend}/auth/callback?code=<60s single-use exchange code>
   |--------------->| POST /auth/exchange {code}  ->  {token} + HttpOnly session cookie
```

The session JWT is **never** placed in a URL: the redirect carries only a 60-second, single-use
exchange code, redeemed over the response body by the existing `POST /api/v1/auth/exchange`.
The minted JWT is a normal Waddles session (`tenant` = the connection's tenant, `*:read`,
no admin scopes).

## Deploying

1. **Migrate.** `0049_sso_connections` creates `sso_connections` and `sso_identities` (hub-api-only
   grants in `config/postgres/rbac-matrix.yaml`). Run the normal `db-migrate` hook.
2. **Key.** hub-api needs `SSO_ENCRYPTION_KEY` (64 lowercase hex chars). It derives, via
   HKDF-SHA256, (a) the AES-256-GCM key that encrypts stored client secrets (AAD = the
   connection's `public_id`) and (b) the HMAC key that binds each login flow to its browser.
   `templates/sso.yaml` provisions it with the same *keep / generate* mechanism as the other
   `autoProvisionedKeys` Secrets — **generated in alpha/local only**, kept across upgrades
   (`helm.sh/resource-policy: keep`), never rotated automatically. Unlike those keys it **never
   fails the release** elsewhere: SSO is entitlement-gated, so beta/gamma/production install
   fine without it, and SSO then fails loudly at request time (HTTP 503 `SSO_UNAVAILABLE`, log
   code `sso_key_missing`) — there is no fallback key and no plaintext mode. To enable SSO
   outside alpha/local, pre-create the Secret, or point an ExternalSecret/SealedSecret at it and
   set `sso.encryptionKey.externalSecret=true`:
   ```bash
   kubectl -n <ns> create secret generic waddlebot-sso-encryption-key \
     --from-literal=SSO_ENCRYPTION_KEY="$(openssl rand -hex 32)"
   ```
3. **State store.** Login state and the SAML replay cache live in the existing Valkey/Redis
   (`oauth_connection_state` pattern) so any hub-api replica can complete a flow another started.
4. **Outbound HTTPS.** hub-api fetches each OIDC IdP's discovery document, JWKS and token
   endpoint. Allow egress on 443 to your IdPs if you police hub-api egress. SAML needs no
   outbound call (the browser carries everything).
5. **Flags.** Turn on `waddles.auth.sso_saml` and/or `waddles.auth.sso_google` for the tenant in
   PostHog (self-hosted; alpha uses the in-cluster instance) and make sure the license tier
   covers it.

### Operator settings

| Helm value (`sso.*`) | Env var | Default | Meaning |
|---|---|---|---|
| `enabled` | — | `true` | `false` renders nothing and injects no SSO env |
| `encryptionKey.secretName` / `.secretKey` | `SSO_ENCRYPTION_KEY` | `waddlebot-sso-encryption-key` / `SSO_ENCRYPTION_KEY` | key Secret (optional ref) |
| `encryptionKey.externalSecret` | — | `false` | an external controller owns the Secret; chart neither creates nor requires it |
| `allowedPrivateHosts` | `SSO_ALLOWED_PRIVATE_HOSTS` | `[]` | exact hostnames of IdPs that legitimately resolve to private addresses (on-prem Keycloak/ADFS) |
| `stateTtlSeconds` | `SSO_STATE_TTL_SECONDS` | `600` (60–3600) | login-flow lifetime |
| `clockSkewSeconds` | `SSO_CLOCK_SKEW_SECONDS` | `120` (0–300) | tolerated IdP clock drift |
| `google.existingSecret` (+ `clientIdKey`, `clientSecretKey`) | `SSO_GOOGLE_CLIENT_ID`, `SSO_GOOGLE_CLIENT_SECRET` | empty | optional shared Google OAuth client (tenants otherwise bring their own) |
| — | `SSO_HTTP_TIMEOUT_SECONDS` | `10` (1–60) | per-request IdP timeout |

Malformed settings fail loudly (an error, not a silent default).

## Setting up a connection (tenant admin)

Requires scope `auth.sso:admin` (granted to the tenant `admin` bundle; global admins hold it via
`*:admin`). A connection is created as a **draft** — both SAML and OIDC need the connection's own
callback/ACS URL (which contains its generated id) to register the app at the IdP *before* the
IdP details exist. Whatever you supply is validated immediately; completeness is enforced when you
set `enabled: true`.

Every connection requires **`allowedDomains`** — the email domains your organisation owns. Only
those domains can log in (re-checked on every login, so removing a domain cuts off its users).
Public mailbox providers (gmail.com, outlook.com, ...) are refused.

### SAML 2.0

```bash
# 1. Create a draft -> response carries spEntityId, acsUrl, metadataUrl
curl -sX POST $HUB/api/v1/tenant/sso/connections -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"protocol":"saml","displayName":"Okta","allowedDomains":["acme.com"]}'

# 2. Give the IdP admin the metadataUrl (public, secret-free): GET /api/v1/auth/sso/{id}/metadata
#    or enter manually: Entity ID = spEntityId, ACS URL = acsUrl (HTTP-POST binding),
#    NameID = emailAddress (or persistent), sign the assertion (and/or the response) with SHA-256+.

# 3. Paste the IdP's metadata (or pass idpEntityId/idpSsoUrl/idpCertificates) and enable
curl -sX PATCH $HUB/api/v1/tenant/sso/connections/{id} -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"idpMetadataXml":"<EntityDescriptor ...>...</EntityDescriptor>","enabled":true}'
```

Requirements on the IdP side: SP-initiated flow, `HTTP-Redirect` SSO endpoint, **signed**
Response or Assertion (RSA/ECDSA with SHA-256/384/512), a non-transient NameID, `Audience` =
`spEntityId`, `Recipient`/`Destination` = `acsUrl`. Attribute names tried for the email:
`email`, `mail`, `emailAddress`, the WS-Fed/`urn:oid` equivalents (override with
`emailAttribute`; display name via `nameAttribute`). **Certificate rotation:** re-import the new
metadata (`PATCH` with `idpMetadataXml`) — certificates are pinned, the key embedded in a
message is never trusted.

### OpenID Connect

```bash
curl -sX POST $HUB/api/v1/tenant/sso/connections -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"protocol":"oidc","displayName":"Entra","allowedDomains":["acme.com"],
       "issuer":"https://login.microsoftonline.com/<tid>/v2.0","clientId":"...","clientSecret":"...",
       "enabled":true}'
```
Register the returned `callbackUrl` as the redirect URI at the IdP. Authorization Code flow with
PKCE (S256) and a per-flow nonce; the discovery document's `issuer` must equal the configured
one byte-for-byte. Omit `clientSecret` (or send `clearClientSecret: true`) for a public client.

### Google (Professional)

```bash
curl -sX POST $HUB/api/v1/tenant/sso/connections -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"protocol":"google","displayName":"Google","allowedDomains":["acme.com"],
       "clientId":"....apps.googleusercontent.com","clientSecret":"...","enabled":true}'
```
Logins must come from the tenant's Workspace domain (`hostedDomain`, defaulting to the single
allowed domain; the `hd` claim is checked server-side, not just hinted) with a verified email — a
personal Gmail account is refused. If the operator configured the shared Google client
(`sso.google.existingSecret`), omit `clientId`/`clientSecret` (or `usePlatformClient: true`).

### Admin API

| Method & path | Purpose |
|---|---|
| `GET /api/v1/tenant/sso/connections` | list the tenant's connections (no secrets; `entitled` shows current gate) |
| `POST /api/v1/tenant/sso/connections` | create (draft unless `enabled: true`) — 402 if not entitled |
| `GET /api/v1/tenant/sso/connections/{id}` | read one (other tenants' ids are 404) |
| `PATCH /api/v1/tenant/sso/connections/{id}` | partial update; enabling revalidates everything; `protocol` immutable |
| `DELETE /api/v1/tenant/sso/connections/{id}` | delete + drop its identity links; always allowed |

Public (pre-auth): `GET /api/v1/auth/sso/options?tenant=<slug>`, `GET .../{id}/start`,
`GET .../{id}/login`, `GET .../{id}/callback`, `POST .../{id}/acs`, `GET .../{id}/metadata`.
The OpenAPI spec is `openapi/v1.yaml` (served behind auth; the public document stays login-only).

## Frontend contract

1. `GET /api/v1/auth/sso/options?tenant=<slug>` → render one button per option.
2. `GET /api/v1/auth/sso/{id}/start` (sets the HttpOnly `wb_sso_bind` cookie) → navigate to
   `redirectUrl` — or simply link to `GET .../{id}/login`, which 302s straight there.
3. After the IdP, hub-api redirects to `<frontend>/auth/callback?code=…` → `POST /api/v1/auth/exchange`.
4. On failure it redirects to `<frontend>/login?error=<reason>`:

| `error` | Meaning |
|---|---|
| `sso_denied` | IdP denied / missing parameters / connection unavailable |
| `sso_session_mismatch` | state unknown, expired, reused, wrong connection, or flow not started in this browser |
| `sso_invalid_response` | signature/ID-token/audience/nonce/time/replay validation failed |
| `sso_idp_unavailable` | IdP unreachable or returned an error |
| `sso_not_entitled` | tenant no longer entitled (tier/flag) |
| `sso_domain_not_allowed` | email domain (or Google Workspace) not permitted |
| `sso_email_required` / `sso_email_unverified` | IdP supplied no / unverified email for a new account |
| `sso_account_conflict` | an existing account owns that email and is not linked — sign in normally and contact an admin |
| `sso_account_inactive` | linked account is deactivated |
| `sso_unavailable` | server-side misconfiguration (key missing, bad settings); see logs |

## Accounts and provisioning

* Users are matched by **(connection, IdP subject)** — OIDC `sub` / SAML `NameID` — stored in
  `sso_identities`, not by email. Same subject string from two IdPs never collides.
* First login **provisions** the user just-in-time (email must be present, verified, and in
  `allowedDomains`). SSO accounts have no local password.
* An existing `hub_users` row is **never adopted by email.** `hub_users` is global across tenants,
  so an email asserted by one tenant's IdP is not proof of ownership; a collision is refused
  (`sso_account_conflict`). (Authenticated "link my existing account" is a follow-up.)
* Provisioned users receive no admin scopes regardless of IdP claims; role/group mapping is a
  follow-up.
* Deleting a connection deletes its links; deleting a user cascades their links (erasure leaves no
  subject behind).

## Security model

| Concern | Control |
|---|---|
| Forged/replayed/mis-addressed SAML | pinned-cert RSA/ECDSA SHA-2 signature (SHA-1/DSA/HMAC refused); trust only the element `signxml` reports as signed; exactly one `Assertion`; `InResponseTo` bound to this flow; `Recipient`/`Destination`/`Audience`/time window enforced; assertion-ID replay cache; transient NameID refused |
| XML attacks | DOCTYPE/ENTITY rejected pre-parse, no entity/DTD/network, comments stripped, mixed-content NameID refused |
| OIDC token attacks | asymmetric algorithms only (`none`/HS* refused), `kid`-selected JWKS key, `iss`/`aud`/`azp`/`exp`/`iat`/`nbf`/nonce, PKCE S256, discovery issuer pinned |
| Login CSRF / fixation | flow state single-use in Redis (`GETDEL`) + HMAC browser-binder cookie (`SameSite=Lax`; `None` for the SAML ACS POST) |
| SSRF | https only; resolved address must be public; operator allowlist for private IdPs; no redirects; response-size/time limits; re-checked per request |
| Secrets | client secret AES-256-GCM at rest (AAD = connection id), never in responses/logs/audit; key via Secret ref only |
| Tenancy / IDOR | tenant from the verified JWT (admin) or the connection row (public); never from request input; other tenants' connections are 404 |
| Logs / traces | no email, subject, name, code, state, token, secret or protocol payload; exceptions logged by type, fixed code and frame-only traceback (third-party exception text is withheld); `signxml` DEBUG chatter (which prints the assertion) is pinned to INFO |

## Observability

* **Logs** (structured key=value, `connection=<public_id>`, user as `user_uuid` only): `sso.login.started|succeeded|failed|crashed`, `sso.connection.created|updated|disabled|deleted`, `sso.audit.write_failed`, `sso.login.identity_race`. Failures render `err_type`, `err_code`, a safe `err_msg` and the frame traceback in the message itself.
* **Metrics** (OTLP, bounded labels, no PII): `waddles_sso_login_total{protocol,outcome,reason}`, `waddles_sso_login_start_total`, `waddles_sso_login_duration_seconds`, `waddles_sso_idp_request_duration_seconds{protocol,operation}`, `waddles_sso_connection_changes_total`, `waddles_sso_saml_validation_failures_total{code}`.
* **Traces**: `sso.login.start`, `sso.oidc.discovery|token_exchange|jwks`, `sso.idp.*`, `sso.saml.validate`, `sso.login.provision`; errors record the exception *type* only.
* **Audit**: admin create/update/disable/delete write `audit_log` rows (ids and booleans only).

## Key rotation

`SSO_ENCRYPTION_KEY` rotation invalidates stored client secrets (decrypt fails closed:
`secret_decrypt`) and in-flight logins. Rotate by replacing the Secret, then re-saving each OIDC/Google
connection's `clientSecret`. Plan for a short maintenance window.

## Troubleshooting

| Symptom | Look for |
|---|---|
| Admin calls return 503 `SSO_UNAVAILABLE` | `err_code=sso_key_missing` / `bad_setting` / `sso_schema_missing` (migration not applied) |
| Login bounces with `sso_invalid_response` | `err_code=` in hub-api log: `saml_audience`, `saml_signature_invalid`, `saml_in_response_to`, `oidc_nonce_mismatch`, `oidc_unknown_kid`, ... |
| `sso_idp_unavailable` | `idp_url_blocked` (SSRF guard — add the host to `sso.allowedPrivateHosts` if it is a legitimate private IdP), `idp_http_status`, `idp_unreachable` |
| SAML always `sso_session_mismatch` | the browser did not send the binder cookie to the ACS — check third-party-cookie blocking and that the SPA/API are served over HTTPS |
| `sso_account_conflict` | an account with that email already exists; see "Accounts" |

## Known limitations (tracked follow-ups)

* Not supported, and refused loudly: encrypted assertions, IdP-initiated (unsolicited) SAML, SAML
  Single Logout, signed AuthnRequests, SAML POST-binding SSO endpoints.
* No authenticated "link existing account" flow, group/role mapping, DNS-verified domain claims, or
  SCIM — all additive on top of this design.
* One in-flight login per connection per browser (a second `start` replaces the binder cookie).
* No "SSO required" enforcement yet: a provisioned user can still set a local password through the existing
  password-reset flow. Enforcing SSO-only sign-in for a tenant's domains is an additive follow-up.
* The React login page does not render SSO buttons yet; the contract above is what it consumes.
