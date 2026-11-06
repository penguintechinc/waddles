# Bundle object storage (`storage` host interface)

Status: implementing (2026-11-06). Stacked on `feature/bundle-table-access-grants`
(reuses its approval / audit / re-approval-on-widening machinery).
Flag: `waddles.bundle-object-storage` (default OFF; unwired -> `not_implemented`).

## 1. Model

- ONE S3-compatible bucket (default: the bundled SeaweedFS, Sec 8). A PREFIX
  ("folder") per bundle: `<owner_app_id>/<tenant>/<community>/<object-key>`.
- Own prefix: AUTO-granted (read/write/list/delete) when the flag is on; no
  manifest entry, no approval. Always further scoped by tenant+community.
- Other bundles' prefixes: only via manifest `storage_access` + global-admin
  approval (same bold-red warning flow as `table_access`) -- Sec 3.
- TAGS on top -- UNIVERSAL INVARIANT (every cross-prefix grant, any producer, no
  special cases): an accessing bundle sees ONLY objects tagged `app-bundle:<its
  app_id>`. Prefix permission alone NEVER exposes untagged objects or objects
  tagged for other bundles. The OWNER of a prefix sees all of its own objects.
  Enforced in ONE place host-side (`StorageHost::visible`, reached via
  `resolve` + `require_visible`) for get / head / list / presign-get / delete
  (and the put-overwrite check); the tag check runs AFTER the grant check;
  tag-hidden == not-found (no existence oracle).
- Sharing = explicit: a producer (media harness or any service/app bundle) must
  tag EACH object for EACH consumer with the OWNER-ONLY `tag-add` / `tag-remove`
  host calls (audited, Allowed and Denied). Non-owners cannot add/remove tags.
- SYSTEM TAGS (immutable, host-written on every put from the VERIFIED scope; a
  guest can never set/change any `waddles-*` tag): `waddles-tenant=<id>`,
  `waddles-community=<id>`. EVERY op (owners included -- defense in depth against
  prefix-path mistakes) requires both to match the caller's own tenant/community;
  an object missing them is hidden. `waddles-request-id=<id>` is provenance only
  (audit/correlation), NOT an access input in v3.0. Object TTL and per-request
  isolation are deferred (not built).
- Bundles NEVER hold S3 credentials. All access is host-mediated; the host
  signs. Tenant/community come from the invocation scope, never guest args.

## 2. Host API contract (WIT `waddle:bundle/storage`, `wit/waddle-bundle/storage.wit`, version `1.0.0`)

```wit
interface storage {
  variant error { not-granted, not-found, invalid(string), too-large(u64),
                  quota-exceeded, rate-limited, backend(string), not-implemented }
  record object-info { key: string, size: u64, content-type: string }
  record put-options { content-type: string, tags: list<string> }
  record list-page { objects: list<object-info>, next-cursor: option<string> }
  /// owner = prefix owner's app_id; none = the caller's own prefix.
  put: func(owner: option<string>, key: string, body: list<u8>, opts: put-options) -> result<object-info, error>;
  get: func(owner: option<string>, key: string) -> result<tuple<object-info, list<u8>>, error>;
  list: func(owner: option<string>, key-prefix: string, cursor: option<string>, limit: u32) -> result<list-page, error>;
  delete: func(owner: option<string>, key: string) -> result<_, error>;
  presign-get: func(owner: option<string>, key: string, ttl-seconds: u32) -> result<string, error>;
  head: func(owner: option<string>, key: string) -> result<object-info, error>;
  /// OWNER-ONLY + audited: share/unshare one of your objects with `consumer`
  /// (adds/removes tag `app-bundle:<consumer>`); non-owner -> not-granted.
  tag-add:    func(owner: option<string>, key: string, consumer: string) -> result<_, error>;
  tag-remove: func(owner: option<string>, key: string, consumer: string) -> result<_, error>;
}
```
(The authoritative file is `wit/waddle-bundle/storage.wit`; it uses `%list` as the escaped name.)

Producer contract (media harness, `feature/harness-media-rust`): call
`put(none, "<output-key>", bytes, {content-type, tags: ["app-bundle:<requester_app_id>"]})`.
The harness writes under its OWN prefix, then calls
`tag-add(none, key, requester_app_id)` (or passes the tag in `put-options.tags`);
the requester (an app bundle holding an approved
`storage_access: [{prefix_owner: "<harness app_id>", ops: [read,list]}]`)
then reads with `get(some(harness_app_id), key)` and sees only objects tagged
`app-bundle:<requester>`. Rules: tags must match `^[a-z0-9-]{1,32}:[A-Za-z0-9._-]{1,128}$`,
<= 8 per object, accepted ONLY when writing to the caller's own prefix (a
cross-prefix writer's object is auto-tagged `app-bundle:<writer>`, no free tags).
`presign-get` TTL is clamped to 1..=300s (default max 300), scoped to ONE object key, GET only.
Host-side Rust API (`core/bundle_host_storage`): `StorageHost::{put,get,head,list,delete,presign_get,tag_add,tag_remove}(&StorageScope, owner: Option<&str>, ..)`; `StorageScope{app_id,tenant,community,grants,enabled,request_id}` is built from the verified invocation scope, never guest args. Backends: `S3Backend` (SeaweedFS/any S3; tags ride as user-metadata `waddles-tags`), `MemoryBackend` (tests).

## 3. Manifest + approval

```yaml
storage_access:
  - prefix_owner: waddles.harness.media      # another bundle's app_id; never self
    ops: [read, list]                        # subset of read|list|write|delete
    reason: "Fetch generated clips"          # 1-280 chars, mandatory
```
- Folds into dangerous permission ids `storage.<op>:<prefix_owner>` (never
  auto-granted -- not by the core seeder either; seeder filters `storage.*`).
  `read` covers get + presign-get. Service bundles MAY be prefix owners; both
  kinds may request.
- Rides approval -> `app_permission_requests` -> `community_permission_grants` ->
  gate, `permission_hash` re-consent and revocation unchanged. Summary carries
  `storageAccess: [{prefixOwner, ops, reason}]` (hashed); widening (new owner/op)
  changes the hash -> `classify_diff` = `widened` -> grant of new ids SUSPENDED
  until re-approved (grants are exact-id). UI renders the same BOLD RED block.
- Activation by a community admin requires `acknowledgedStorageAccess`.

## 4. Host enforcement order (`core/bundle_host_storage`, all fail-closed)

1. Flag on. 2. Key validation: <=512B, <=16 segments, each `[A-Za-z0-9._-]`,
no empty / `.` / `..` segment, no leading `/`, no `\`, no `%` (rejects every
percent-encoded separator), no control/NUL. 3. Authorize: own prefix -> allow;
else exact `storage.<op>:<owner>` grant must exist (gate snapshot). 4. Scope:
object key = `<owner>/<tenant>/<community>/<key>` built host-side. 5. Tag filter
(cross-prefix only) on get/list/presign/delete. 6. Limits: body <= 25 MiB, content-type
allowlist, per-scope quota (default 512 MiB, 5000 objects, computed from the
backend listing -- authoritative, no separate counter), per-(bundle,community)
rate limit (default 20 ops/s per replica). 7. Audit EVERY cross-prefix access
(allowed and denied) via `AuditSink`; metrics
`waddles_bundle_storage_{op_duration_seconds,denied_total{reason},bytes_total{dir},quota_rejections_total}`.
Tag-hidden == not-found (no existence oracle).

## 5. Delivery (PR stacked on feature/bundle-table-access-grants)
Spec -> crate + gate family + Python manifest/summary (registers a
`storage_prefix` kind in `bundle_access_grants`, so never-auto-grant, hashed
summary `storageAccess`, widening diff and the bold-red webui block come from the
table-access registry) + WIT -> helm. Executor linker wiring for the `storage`
import follows the table-access `platform-table` linker (same dispatch point);
until then the capability is not linked into any world a bundle can import.

## 8. Helm: bundled SeaweedFS by default, external opt-in
`bundleStorage.mode: bundled` (default, every values file) -> bucket
`waddles-bundle-storage` created idempotently by the existing `seaweedfs-bucket-init`
hook Job with SSE-S3 default encryption (existing `infrastructure.seaweedfs.encryption`,
KEK from existingSecret); dedicated bucket-scoped S3 identity, credentials from the chart
Secret. `mode: external` -> `bundleStorage.external.{endpoint,bucket,region,existingSecret}`;
no bundled bucket created; render fails if endpoint/existingSecret missing. Credentials are
only ever mounted into the host stage pods, never literals, never bundles.
