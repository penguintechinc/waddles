# Enterprise External KMS / BYOK (Bring Your Own Key)

Customer-managed keys for Waddles' at-rest encryption, as the **Enterprise-tier upsell on top of the
platform-managed baseline -- never a substitute for it.** With nothing configured, every tenant and
every bucket is already encrypted at rest with platform-managed keys; BYOK only changes *who holds the
key that wraps the data keys*.

| | Platform baseline (all tiers, zero config) | External KMS / BYOK (Enterprise) |
|---|---|---|
| Per-tenant data keys (DB/secret field encryption) | DEK wrapped by the platform KEK (`TENANT_KEK_HEX`) | DEK wrapped by **your** AWS KMS / Google Cloud KMS / Azure Key Vault key |
| Object storage (SeaweedFS) | SSE-S3 (`AES256`) under `WEED_S3_SSE_KEK` | SSE-KMS (`aws:kms`) under the **operator's** AWS KMS / Google Cloud KMS key |
| Licence | none | `compliance.external_kms` (Enterprise tier + PostHog flag, both required) |
| Failure mode | n/a | **fails closed, loudly** -- never a silent fallback to the platform key or to plaintext |

```
 data field --AES-256-GCM(data subkey, AAD=tenant|table|column|row|dek_version)--> ciphertext
 data subkey = HKDF(tenant DEK)              (cached in-process 10 min; the raw DEK is never cached)
 tenant DEK  --wrapped by-->  KEK = platform key (default)
                                  | customer KMS key (Enterprise) -- the KEK never leaves the KMS
 object      --SSE-KMS-->     data key wrapped by the operator's KMS key (SeaweedFS does this)
```

Design: `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md` (Sec4 key lifecycle,
Sec6 BYOK). Code: `hub_api/services/envelope/` (see its `README.md`), `hub_api/blueprints/v1/tenant_kms.py`,
`hub_api/services/object_storage_kms.py`, chart `k8s/helm/waddlebot/templates/{kms.yaml,_kms.tpl}`.

## Two features, one switch

| Feature | Who configures | Scope | Providers |
|---|---|---|---|
| **Per-tenant keys** (`kms.tenantKeys`) | each tenant admin, over the API | one key per tenant | AWS KMS, Google Cloud KMS, Azure Key Vault |
| **Object storage** (`kms.objectStorage`) | the operator, in Helm values | the deployment's buckets | AWS KMS, Google Cloud KMS (SeaweedFS ships no native Azure provider) |

Both are **off by default** and share the master switch `kms.enabled`. A sub-feature requested without
the master switch (or the master switch with nothing to configure) **fails `helm template`/`install`**
rather than being silently ignored.

## Part 1 -- Per-tenant customer-managed keys

### 1. Operator: enable providers

```yaml
kms:
  enabled: true
  tenantKeys:
    enabled: true
    providers: [aws_kms, gcp_kms, azure_key_vault]   # any subset; unlisted providers are refused
    aws:
      platformPrincipal: "arn:aws:iam::<waddles-account>:role/waddles-hub-api"  # shown to customers
    azure:
      credentialsSecret: waddles-kms-azure     # REQUIRED for azure_key_vault (see below)
    # gcp.credentialsSecret: optional; empty = GKE Workload Identity / metadata server
```

Provider credentials are **`existingSecret` references only** -- create the Secrets out of band; no
credential value ever lives in `values.yaml`. Secret key names are the env var names hub-api reads:

| Provider | Platform identity Waddles uses | Secret keys | Env (non-secret, from the `-kms` ConfigMap) |
|---|---|---|---|
| AWS | ambient boto3 chain -- **IRSA / Pod Identity preferred**; optional static pair | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (`aws.credentialsSecret`, only without IRSA) | `ENVELOPE_AWS_PLATFORM_PRINCIPAL`, `ENVELOPE_AWS_KMS_ENDPOINT_URL`, `ENVELOPE_AWS_STS_ENDPOINT_URL` |
| GCP | service-account key **or** GKE Workload Identity / metadata server | `ENVELOPE_GCP_CREDENTIALS_JSON` (`gcp.credentialsSecret`; omit for Workload Identity) | `ENVELOPE_GCP_PLATFORM_PRINCIPAL`, `ENVELOPE_GCP_KMS_ENDPOINT` |
| Azure | one **multi-tenant Entra application** customers consent to | `ENVELOPE_AZURE_CLIENT_ID`, `ENVELOPE_AZURE_CLIENT_SECRET` (`azure.credentialsSecret`, required) | `ENVELOPE_AZURE_AUTHORITY` |

Other env: `ENVELOPE_KMS_PROVIDERS` (set by the chart), `ENVELOPE_KMS_TIMEOUT_S` (default 10),
`ENVELOPE_DEK_CACHE_TTL_S` (600, max 3600), `ENVELOPE_STALE_GRACE_S` (900, max 3600),
`ENVELOPE_DENIED_BACKOFF_S` (30), `ENVELOPE_TRANSIENT_BACKOFF_S` (5), `ENVELOPE_USAGE_CAP` (2^30). Out-of-range
values refuse to start -- a typo cannot switch a safety property off. A provider that is enabled with
missing/malformed credentials **fails hub-api's startup**, not a tenant's first request.

### 2. Customer: onboard a key

Call `PUT /api/v1/tenant/<slug>/kms` (scope `compliance.kms:admin`, Enterprise entitlement). The response
carries an **`externalId`** -- a server-generated 192-bit token (lowercase hex) that is the confused-deputy
guard for all three providers -- and `GET .../kms` lists `platformPrincipals`, the Waddles identity to
trust. Then do your half at the provider, and `POST .../kms/activate`.

#### AWS KMS

Key: a **symmetric `ENCRYPT_DECRYPT`** key, referenced by **key ARN** (aliases are rejected -- an alias can
be repointed). Partitions `aws` and `aws-us-gov`. Waddles reaches it only by **assuming a role in your
account** (`sts:AssumeRole` with your ExternalId); it never calls KMS directly with its own identity.

```jsonc
// Role trust policy -- the ExternalId closes the confused-deputy hole
{ "Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"AWS": "<platformPrincipals.aws_kms>"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "<externalId>"}} }] }
// Role permissions -- exactly this key, exactly these actions
{ "Effect": "Allow", "Action": ["kms:Encrypt", "kms:Decrypt", "kms:DescribeKey"], "Resource": "<key ARN>" }
```

```json
PUT /api/v1/tenant/acme/kms
{ "provider": "aws_kms",
  "keyRef": "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
  "principal": "arn:aws:iam::111122223333:role/waddles-byok" }
```

Every call binds the KMS `EncryptionContext` `{waddles_purpose, waddles_tenant_id}`, so you may pin IAM
conditions on `kms:EncryptionContext:waddles_purpose`.

#### Google Cloud KMS

Key: a symmetric `ENCRYPT_DECRYPT` CryptoKey (`GOOGLE_SYMMETRIC_ENCRYPTION`), referenced by its resource
name **without** a version (Cloud KMS picks the right version on decrypt, so you can rotate freely).
`principal` is unused. Prove control of the key by labelling it with the ExternalId, and grant the platform
service account:

```bash
gcloud kms keys add-iam-policy-binding waddles --keyring ring --location us-east1 \
  --member "serviceAccount:<platformPrincipals.gcp_kms>" --role roles/cloudkms.cryptoKeyEncrypterDecrypter
gcloud kms keys update waddles --keyring ring --location us-east1 --update-labels waddles-external-id=<externalId>
```

```json
{ "provider": "gcp_kms",
  "keyRef": "projects/cust-proj-1/locations/us-east1/keyRings/ring/cryptoKeys/waddles" }
```

The context is bound as `additionalAuthenticatedData`.

#### Azure Key Vault / Managed HSM

Key: an **RSA >= 2048-bit** key permitting `wrapKey`/`unwrapKey`, referenced by its URL **without** a
version: `https://<vault>.vault.azure.net/keys/<name>` (or `<hsm>.managedhsm.azure.net`). `principal` is
**your Entra directory (tenant) id** (a GUID). Consent to Waddles' multi-tenant application in your
directory, assign it **Key Vault Crypto User** on the key, and tag the key with the ExternalId:

```bash
az ad sp create --id <platformPrincipals.azure_key_vault>                 # admin consent
az keyvault key create --vault-name V --name waddles --kty RSA --size 3072 \
  --ops wrapKey unwrapKey --tags waddles-external-id=<externalId>
az role assignment create --assignee-object-id <sp object id> --role "Key Vault Crypto User" --scope <key id>
```

```json
{ "provider": "azure_key_vault", "keyRef": "https://contoso.vault.azure.net/keys/waddles",
  "principal": "99999999-8888-7777-6666-555555555555" }
```

Key Vault's RSA wrap has no associated-data input, so Waddles seals `DEK || SHA-256(context)` and verifies
the digest on unwrap; the wrapped value also records the **key version** that wrapped it, so key rotation
never strands old data. Sovereign clouds (`*.azure.cn`, `usgovcloudapi`) are not supported.

### 3. Lifecycle

| Call | Entitlement | What happens |
|---|---|---|
| `GET /kms` | none | config, `status`, `externalId`, no-secret DEK summary (version, kek kind/ref, usage), `platformPrincipals` |
| `PUT /kms` | Enterprise | validates strictly, stores the config as `pending`; **moves no key material** |
| `POST /kms/activate` | Enterprise | preflight (key shape, **proof-of-control**, wrap/unwrap probe) -> re-wraps **every** DEK version onto your key -> `active` only when all moved; a partial result stays `pending` and is safe to re-run |
| `DELETE /kms` | **never gated** | exit ramp: re-wraps every DEK back to the platform KEK, then drops the config; if the key is already revoked it reports what is stuck (`409 REWRAP_INCOMPLETE`) and keeps the config |

DEKs themselves never change on activation or exit, so **no data is re-encrypted** and previously written
ciphertext stays readable. Switching to a new key: `PUT` the new key (the tenant is `pending`; new key
material is refused with `kms_pending` rather than falling back), then `activate`. The ExternalId is stable
across edits. A provider cannot be swapped while DEKs are wrapped by the current one -- disable first.

### 4. Failure behaviour (the contract)

| Event | Behaviour |
|---|---|
| You revoke the grant / disable or delete the key (**access denied**) | cache **evicted at once**, no TTL grace; reads and writes fail with `TENANT_KEY_UNAVAILABLE` (`kms_access_denied`); config flagged `revoked`; ERROR log + metric (`alert=tenant-kms-revoked`); **never** falls back to the platform key. Restoring access heals the flag. |
| Provider outage / timeout / 5xx / throttle | within the 10-min TTL nothing changes; after it, **new** data is refused (no ciphertext under a key we cannot currently authorise) while **existing** data stays readable from the stale cache for a bounded grace (+15 min), then fails closed. Not flagged revoked. |
| **Waddles'** own credential for the provider is bad (e.g. rotated Entra secret) | surfaced as `kms_platform_credentials` (`alert=kms-platform-credentials`), **never** as your revocation |
| One tenant's key is down | zero effect on any other tenant (per-tenant lock, backoff, cache) |
| Licence lapses | new BYOK key material / configuration refused (`403 EXTERNAL_KMS_NOT_ENTITLED`); **existing data stays readable** and the exit ramp keeps working |
| Platform KEK missing | `PlatformKekError` -- loud, never a derived/empty key |

Error codes: `EXTERNAL_KMS_NOT_ENTITLED` 403, `INVALID_KMS_CONFIG` 422, `KMS_ACCESS_DENIED` 422,
`KMS_UNAVAILABLE` 503, `KMS_REJECTED` 502, `TENANT_KEY_UNAVAILABLE` 503, `REWRAP_INCOMPLETE` 409,
`PLATFORM_KEK_UNAVAILABLE` 503. Messages are fixed strings: provider text (which can echo ARNs and
principals) is never returned or logged, and no response ever contains wrapped or plaintext key bytes.

### 5. Observability

OTel (OTLP endpoint via the standard env vars): `waddles_envelope_kms_call_duration_seconds` (histogram:
provider/operation/outcome), `waddles_envelope_kms_failures_total` (kek_kind/reason/tenant),
`waddles_envelope_operations_total`, `waddles_envelope_dek_cache_total`, `waddles_envelope_rewrap_rows_total`;
spans `envelope.kms.{wrap,unwrap,describe}`. Log events are PII-free structured records
(`envelope.kms.access_denied`, `envelope.kms.unavailable`, `envelope.rewrap.done`, ...). Every configure /
activate / disable writes an `audit_log` row (`target_type=tenant_kms`; provider and counts only).

## Part 2 -- Object storage (SeaweedFS) under the operator's key

```yaml
kms:
  enabled: true
  objectStorage:
    mode: kms                  # off | kms | drain
    provider: aws_kms          # aws_kms | gcp_kms
    keyId: arn:aws:kms:us-east-1:111122223333:key/...      # or alias/..., or a GCP CryptoKey name
    entitlementTenant: system  # whose Enterprise entitlement gates `kms` mode
    aws:
      region: us-east-1
      credentialsSecret: {name: waddles-kms-s3}            # keys AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
    # gcp: {projectId: my-project, credentialsSecret: {name: waddles-kms-gcp, key: credentials.json}}
```

What the chart renders (`templates/infrastructure/seaweedfs.yaml` hooks into `_kms.tpl`): a `kms` section in
SeaweedFS's S3 config -- provider fields **flat** under the provider (a nested `"config"` object is silently
ignored by SeaweedFS 4.48; this was verified, not assumed) -- with credentials expanded from the Secret's env
vars at container start, and the bucket-init hook's `put-bucket-encryption` switched from `AES256` to
`aws:kms` with your `KMSMasterKeyID`. The platform baseline stays wired (`WEED_S3_SSE_KEK`), and the chart
refuses to render with `infrastructure.seaweedfs.encryption.enabled=false`.

| `mode` | Provider loaded | Bucket default | hub-api uploads |
|---|---|---|---|
| `off` (default) | no | `AES256` | `AES256` |
| `kms` | yes | `aws:kms` + your key | `aws:kms` + your key -- **refused loudly** unless `entitlementTenant` holds the entitlement |
| `drain` | yes | back to `AES256` | `AES256` (never gated) |

**Exit ramp.** `kms` -> `drain` -> re-encrypt -> `off`. Setting `off` while objects still sit under KMS would
make them unreadable, so go through `drain`: new writes return to the baseline while old KMS objects remain
readable, then re-encrypt them in place and only then set `off`:

```bash
aws --endpoint-url http://seaweedfs:8333 s3 cp s3://waddlebot-assets/ s3://waddlebot-assets/ \
  --recursive --metadata-directive REPLACE --sse AES256     # repeat per bucket
```

(Verified against the real pinned image by `make test-seaweedfs-sse-kms`: after the self-copy the object is
`AES256` and readable with the KMS denied.)

**Caveats -- read these.**
- **The licence boundary.** Helm cannot ask the licence server, and the bucket default is applied by the
  chart's Admin-identity hook, so the Rust writers (svc-presentation, svc-streaming, the bundle seeder)
  inherit the KMS bucket default without a per-write check. hub-api verifies the entitlement **at startup**
  (an unentitled `kms` mode logs `object_storage.kms.not_entitled` at ERROR) and **on each of its own
  writes** (cached 60 s; refused with `ExternalKmsNotEntitledError`, never downgraded to AES256).
  Treat `mode: kms` as an Enterprise-only setting.
- **Revocation is not instantaneous for objects.** SeaweedFS caches decrypted data keys; revoking the KMS
  key stops *new* SSE-KMS writes immediately (they fail with an S3 5xx) but reads of already-cached keys can
  continue until the cache turns over or the pod restarts. Per-tenant keys (Part 1) evict immediately.
- **Verification depth differs by provider.** The AWS path is exercised end to end against the real pinned
  SeaweedFS image (`make test-seaweedfs-sse-kms`). For Google Cloud KMS the rendered provider config
  (`type: gcp`, `project_id`, `credentials_file`) is verified to be accepted and loaded by that image, but
  SeaweedFS talks to Cloud KMS over gRPC, so a full encrypt/decrypt round trip against Cloud KMS is not part
  of the automated suite -- validate it once in a staging project before relying on it.
- **Azure Key Vault is not available for object storage** (no native SeaweedFS provider). The chart fails
  the render with a clear message if you ask for it.
- **Network.** hub-api and SeaweedFS need egress to your KMS endpoints: AWS `kms.<region>.amazonaws.com`
  and `sts.<region>.amazonaws.com`; Google `cloudkms.googleapis.com` and (service-account-key auth)
  `oauth2.googleapis.com`; Azure `login.microsoftonline.com` and `<vault>.vault.azure.net`. The chart adds
  no egress policy of its own -- allow-list these in your cluster baseline.

## Verification

```bash
cd hub_api && python3 -m pytest tests/envelope -q          # ~450 tests; socket-level mock KMS, real crypto
python3 -m pytest ../k8s/helm/waddlebot/tests/test_kms_render.py -q                     # chart (helm CLI)
make test-seaweedfs-sse-kms     # opt-in: chart-rendered config against the real pinned `weed` image
```

The mocks are real HTTP servers on loopback speaking the genuine AWS (SigV4/JSON-1.1/STS), Google (RS256
JWT-bearer + REST) and Azure (Entra + RSA-OAEP-256) wire protocols; only the provider API is faked. The
key store tests run the **production migration** (`0054_tenant_external_kms`) in a real Postgres.

## Not in scope / follow-ups

- Moving the **platform** KEK itself into a KMS (HYOK for the baseline). An on-prem operator gets BYOK for
  their own tenant (`default`/`system`) through the same tenant API.
- A dedicated Postgres role/connection and short-retention backup policy for the `keystore` schema (design
  Sec4) -- the repository accepts a connection on that role without code change.
- Reconciling with the open DEK-broker PR (#442): the `keystore.tenant_encryption_keys` DDL here is a
  superset-compatible `IF NOT EXISTS` of its table, so the two migrations are order-independent.
- Adopting the envelope for the existing plaintext/bespoke-encrypted columns (design Sec7 phases) -- this
  PR delivers the key hierarchy, the BYOK wiring and the API; column migrations are separate increments.
