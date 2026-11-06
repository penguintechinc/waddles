# AI knowledge sources -- authorization and token handling

Routes: `/api/v1/admin/ai-knowledge/sources` (list/create) and `/sources/<id>` (update/delete, `/reindex`).

## Authorization

| Source scope | Who may act | Foreign / missing id |
|---|---|---|
| `community_id` set | caller's tenant owns the community (`community_belongs_to_tenant`) | 404 |
| no community, `vendor_id` set | that vendor (`hub_users.id == sub`) | 404 |
| neither (global) | super admin (`hub_users.is_super_admin`, DB-authoritative) | 404 |

- Super admins pass every scope. Update/delete/reindex return **404, never 403**, so a foreign id is indistinguishable from a missing one.
- `GET` requires `communityId` (legacy `community_id` accepted) unless the caller is a super admin; a community outside the caller's tenant is 403.
- `POST` applies the same scope rule to the supplied `community_id`/`vendor_id` (403 on mismatch).

## Access tokens

- Request field is `token` (plaintext, TLS only). `encrypted_token` is a **deprecated** alias and is *also* treated as plaintext.
- Encrypted server-side with `github_sync_service.encrypt_token` (AES-256-GCM, key `GITHUB_SYNC_ENCRYPTION_KEY`); stored in `ai_knowledge_sources.encrypted_token`; decrypted only inside `index_source`. Never returned or logged. Missing key -> 500, nothing stored.
- On update, an empty `token` leaves the stored token unchanged.

## Migration

`0075_ai_knowledge_token_enc` encrypts rows that still hold plaintext (written by the Node-era code and the first Python port). Idempotent; needs `GITHUB_SYNC_ENCRYPTION_KEY` only when such rows exist; downgrade is a no-op by design.

## Required environment

`GITHUB_SYNC_ENCRYPTION_KEY` -- 64-char hex (`openssl rand -hex 32`), same key `github_sync_service` uses. Required by hub-api for token writes (500 without it) and by migration `0075` **only if** plaintext token rows exist; with none (or no table) the migration is a no-op. If rows exist and the key is missing/invalid, hub-api's startup migration aborts with: `GITHUB_SYNC_ENCRYPTION_KEY must be a 64-char hex string: ai_knowledge_sources holds plaintext tokens that must be encrypted before this migration can complete` -- set the key and restart.

As of this change the Helm chart (`k8s/helm/waddlebot`) does not define this variable for hub-api; supply it via the hub-api Secret/env before deploying to any environment with existing token rows.

## Search / suggest scoping

`POST .../ai-knowledge/search` and `/suggest` require `communityId` (tenant-checked, 403 otherwise) unless the caller is a super admin; results are filtered to that community in SQL.
