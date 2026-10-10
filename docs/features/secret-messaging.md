# Secret Messaging (One-Time Secrets)

Send a message that its recipient can read exactly once, in the Waddles web UI. The sender's chat command carries only a link, never the message text. Tracked as issue #684.

> **Status.** The backend API (PR #718) and the `/secret` web page (PR #721) are built but not all on `release/v3.0.X` yet. The `!secret` chat command is **not implemented**: no `!secret` bundle exists in the repo, so today a service or bundle caller creates the secret through the API. Everything below describes the API and page as built; the feature is OFF by default.

## How it works

1. A caller with scope `secret_messaging:create` calls `POST /api/v1/one-time-secrets` with the target user's UUID and the message. The response returns a link token **once**.
2. The caller delivers a link of the form `/secret#TOKEN` to the recipient (the backend is designed for a `!secret` bundle to do this; that bundle is not built yet).
3. The recipient opens the link, logs in, and clicks **Reveal secret**. The page calls `POST /api/v1/one-time-secrets/pull`.
4. The server returns the message and deletes it. A second pull returns 410.

## Requirements

| Dependency | Detail |
|---|---|
| Flag | `waddles.secret-messaging`, default OFF. With it off the API returns 404 `FEATURE_DISABLED` and the page is not shown. |
| Encryption key | `ONE_TIME_SECRET_ENCRYPTION_KEY` on hub-api: 64 hex characters (AES-256-GCM). Separate from the bundle-secret and RCON keys. |
| Helm | `autoProvisionedKeys.oneTimeSecret` generates the key into the Secret `waddlebot-ots-encryption-key` on alpha and local only. It is generated once and never rotated. On any other tier the chart fails unless you pre-create that Secret or set `externalSecret: true`. |
| User identity | Targets are `hub_users.uuid` values, never usernames. Requires the `hub_users.uuid` column (PR #434). |
| Database | Migration `0045_one_time_secrets` creates the `one_time_secrets` table. |
| Scopes | `secret_messaging:create` to create, `secret_messaging:pull` to pull. Tenant comes from the JWT only. |

## Create a secret

```http
POST /api/v1/one-time-secrets
Content-Type: application/json

{"communityId": 12, "targetUserUuid": "UUID-OF-RECIPIENT", "message": "text", "ttlSeconds": 86400}
```

| Field | Rule |
|---|---|
| `communityId` | Must belong to the caller's tenant, else 404. |
| `targetUserUuid` | Must match a known user, else 404. |
| `message` | 1 to 4000 characters. |
| `ttlSeconds` | 60 to 604800 (7 days). Default 86400 (24 hours). |

Success is `201` with `secretId`, `token` and `expiresAt`. Store or send the token immediately; it cannot be retrieved again.

## Pull a secret

```http
POST /api/v1/one-time-secrets/pull
Content-Type: application/json

{"token": "TOKEN"}
```

The token goes in the request body, never in a URL or query string, so it stays out of access logs. Responses carry `Cache-Control: no-store`.

| Status | Meaning |
|---|---|
| 200 | `message` returned; the secret is deleted. |
| 403 | The caller is not the linked recipient (or the tenant differs). The secret is **not** consumed. |
| 410 | Unknown token, already pulled, or expired. |
| 404 | Flag is off. |
| 500 `SECRET_DECRYPT_FAILED` | Decryption failed. The secret is still consumed. |

## The `/secret` page

| Behavior | Detail |
|---|---|
| Link | `/secret#TOKEN` on the hub web UI. The token rides in the URL fragment, so it is not sent to the server on page load. |
| Login | The recipient must be logged in as the target. Logged out, the fragment is kept and the user is asked to log in. Login does not redirect back, so reopen the link after logging in. |
| Reveal | Once logged in, the token is read and removed from the address bar. The user must click **Reveal secret**, because the pull is destructive. |
| After reveal | The message shows with a **Copy** button. It is held in page memory only, never stored or logged. |
| Errors | `You are not the intended recipient of this secret.` (403). `This secret was already viewed or has expired.` (410). A network failure shows a retry button. With no token: `No secret link detected. Open the link you were sent.` |

## Security properties

- The message is stored as AES-256-GCM ciphertext bound to its row id. The link token is stored only as a SHA-256 hash, so a database read yields neither.
- The pull claims the row atomically, then deletes it, so two concurrent pulls cannot both succeed.
- Expired rows are purged on each create.
- Logs contain community id, operation, a masked secret id and exception type, never the message, token or a username.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| API returns 404 `FEATURE_DISABLED` | Flag is off for the tenant. | Enable `waddles.secret-messaging`. |
| hub-api errors about `ONE_TIME_SECRET_ENCRYPTION_KEY` | Key missing or not 64 hex characters. | Provide the key through the chart Secret. |
| `helm` fails on `autoProvisionedKeys.oneTimeSecret` | Non-alpha tier with no pre-created Secret. | Pre-create the Secret or set `externalSecret: true`. |
| Recipient gets 403 | Logged in as a different user. | Log in as the intended recipient and retry; the secret is still there. |

## Source

`hub_api/blueprints/v1/one_time_secrets.py`, `hub_api/services/one_time_secret_service.py`, `hub_api/services/one_time_secret_crypto.py`, `alembic/versions/0045_one_time_secrets.py`, `admin/hub_module/frontend/src/pages/secret/SecretPullPage.tsx`, `k8s/helm/waddlebot/templates/auto-provisioned-secrets.yaml`.
