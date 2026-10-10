# Waddles Credentials Rotation Checklist

**CRITICAL SECURITY**: This document lists all credentials that require rotation in production environments.

## Database Credentials

### PostgreSQL (owner) and per-service roles

> **Per-service roles (H-1 / H-3):** workloads no longer use the owner credential. Each connects as its
> own least-privilege role (`PW_<ROLE>` keys in `<release>-db-credentials`); the owner password lives in
> `<release>-db-admin`. Rotation, burned-credential list, and upgrade steps:
> [DATABASE_CREDENTIALS.md](DATABASE_CREDENTIALS.md). The repo-public `*_dev_changeme` / `dev123` values
> are **burned** -- never reuse them.

- **Environment Variable**: `POSTGRES_PASSWORD` (owner; `<release>-db-admin`)
- **Default Value**: REMOVED (was: `waddlebot_secret`)
- **Locations to Update**:
  - `.env` file (LOCAL ONLY - not in git)
  - `docker-compose.yml` - expects value from environment (local dev only)
  - Helm Secrets: `k8s/helm/waddlebot/templates/secrets.yaml` (templated value from values.yaml)
- **Container Instances**: All services using DATABASE_URL
- **Rotation Procedure**:
  1. Generate new secure password (min 32 chars, mixed case, numbers, symbols)
  2. Update .env file (local dev) or K8s secrets (production)
  3. Update PostgreSQL user password: `ALTER USER waddlebot WITH PASSWORD 'new_password';`
  4. Verify all services can connect
  5. Document rotation date in AUDIT log

### Redis
- **Environment Variable**: `REDIS_PASSWORD`
- **Default Value**: REMOVED (was: `waddlebot_redis`)
- **Locations to Update**:
  - `.env` file (LOCAL ONLY - not in git)
  - `docker-compose.yml` - expects value from environment (local dev only)
  - Helm Secrets: `k8s/helm/waddlebot/templates/secrets.yaml`
- **Container Instances**: redis, labels-core, identity-core, analytics-core, security-core, workflow-core
- **Rotation Procedure**:
  1. Generate new secure password
  2. Update .env or K8s secrets
  3. Update Redis config: `CONFIG SET requirepass new_password`
  4. Verify all services can authenticate
  5. Document rotation date in AUDIT log

## API & Service Credentials

### JWT Secret
- **Environment Variable**: `JWT_SECRET`
- **Default Value**: REMOVED (was: `waddlebot_jwt_secret`)
- **Used By**: hub_module (JWT signing/verification)
- **Locations to Update**:
  - `.env` file
  - `docker-compose.yml` (hub service)
  - K8s Secrets
  - Helm values.yaml
- **Impact**: Invalidates all existing JWT tokens on rotation
- **Rotation Procedure**:
  1. Generate new 256-bit secret (min 64 chars)
  2. Update all secret stores
  3. Restart hub service
  4. Users will need to re-authenticate
  5. Document rotation date in AUDIT log

### Module Secret Key
- **Environment Variable**: `MODULE_SECRET_KEY`
- **Used By**: All action modules (discord-action, slack-action, twitch-action, youtube-action, lambda-action, gcp-functions-action, openwhisk-action)
- **Purpose**: Inter-module authentication and message signing
- **Locations to Update**:
  - `.env` file
  - `docker-compose.yml` (all action modules)
  - K8s Secrets
  - Helm values.yaml
- **Rotation Procedure**:
  1. Generate new 256-bit secret
  2. Update all secret stores
  3. Restart all action modules
  4. Verify module-to-module communication
  5. Document rotation date in AUDIT log

### Service API Key
- **Environment Variable**: `SERVICE_API_KEY`
- **Used By**: loyalty-interaction module
- **Locations to Update**:
  - `.env` file
  - `docker-compose.yml`
  - K8s Secrets
  - Helm values.yaml
- **Rotation Procedure**:
  1. Generate new API key
  2. Update all secret stores
  3. Restart affected services
  4. Update any external systems using this key
  5. Document rotation date in AUDIT log

## Storage Credentials

### SeaweedFS (S3-Compatible)
- **Environment Variables**:
  - `S3_ACCESS_KEY_ID` (default was: `waddlebot`)
  - `S3_SECRET_ACCESS_KEY` (default was: `waddlebot-dev-secret`)
- **Used By**: seaweedfs service, hub_module, video_proxy_module (S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY)
- **Locations to Update**:
  - `.env` file
  - `docker-compose.yml`
  - K8s Secrets
  - Helm values.yaml
- **Master UI Access**: SeaweedFS master UI at localhost:9333 (or configured domain)
- **Rotation Procedure**:
  1. Generate new secure credentials
  2. Update .env or K8s secrets (regenerates the `s3-identities.json` config on restart)
  3. Restart seaweedfs and hub services
  4. Verify via the SeaweedFS master UI
  5. Create new access keys if integrating with external systems
  6. Document rotation date in AUDIT log

## Third-Party Integrations

### Platform OAuth Credentials

#### Twitch
- **Environment Variables**:
  - `TWITCH_CLIENT_ID`
  - `TWITCH_CLIENT_SECRET`
  - `TWITCH_WEBHOOK_SECRET`
- **Used By**: twitch-collector, twitch-action, hub_module
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: Twitch Developer Console
- **Requires**: Re-registration of webhooks
- **Impact**: All Twitch integrations pause during rotation

#### Discord
- **Environment Variables**:
  - `DISCORD_BOT_TOKEN`
  - `DISCORD_CLIENT_ID`
  - `DISCORD_CLIENT_SECRET`
  - `DISCORD_APPLICATION_ID`
- **Used By**: discord-collector, discord-action, hub_module
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: Discord Developer Portal
- **Impact**: Bot offline during rotation

#### Slack
- **Environment Variables**:
  - `SLACK_BOT_TOKEN`
  - `SLACK_SIGNING_SECRET`
  - `SLACK_CLIENT_ID`
  - `SLACK_CLIENT_SECRET`
- **Used By**: slack-collector, slack-action, hub_module
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: Slack App Configuration
- **Impact**: Slack integration offline during rotation

#### YouTube
- **Environment Variables**:
  - `YOUTUBE_API_KEY`
  - `YOUTUBE_CLIENT_ID`
  - `YOUTUBE_CLIENT_SECRET`
  - `YOUTUBE_WEBHOOK_CALLBACK_URL`
- **Used By**: youtube-live-collector, youtube-action, youtube-music
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: Google Cloud Console
- **Requires**: Update webhook callback URL in Google Cloud
- **Impact**: YouTube integrations offline

#### Spotify
- **Environment Variables**:
  - `SPOTIFY_CLIENT_ID`
  - `SPOTIFY_CLIENT_SECRET`
- **Used By**: spotify-interaction, hub_module
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: Spotify Developer Dashboard
- **Requires**: Update redirect URIs
- **Impact**: Spotify features offline

#### Kick
- **Environment Variables**:
  - `KICK_CLIENT_ID`
  - `KICK_CLIENT_SECRET`
  - `KICK_WEBHOOK_SECRET`
- **Used By**: kick-collector
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: Kick Developer Console
- **Requires**: Re-register webhooks
- **Impact**: Kick collector offline

### Cloud Provider Credentials

#### AWS Lambda
- **Environment Variables**:
  - `AWS_ACCESS_KEY_ID`
  - `AWS_SECRET_ACCESS_KEY`
  - `AWS_REGION`
  - `AWS_LAMBDA_ROLE_ARN`
- **Used By**: lambda-action module
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: AWS IAM Console
- **Best Practice**: Create new access key before deleting old
- **Requires**: Update all service integrations

#### GCP Cloud Functions
- **Environment Variables**:
  - `GCP_PROJECT_ID`
  - `GCP_REGION`
  - `GCP_SERVICE_ACCOUNT_KEY`
  - `GCP_SERVICE_ACCOUNT_EMAIL`
- **Used By**: gcp-functions-action module
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: GCP Console
- **Format**: Service account key is typically a JSON file (base64 in K8s)
- **Requires**: Create new service account key

### OpenWhisk (Apache OpenWhisk)
- **Environment Variables**:
  - `OPENWHISK_API_HOST`
  - `OPENWHISK_AUTH_KEY` (default was hardcoded)
  - `OPENWHISK_NAMESPACE`
  - `OPENWHISK_INSECURE`
- **Used By**: openwhisk-action module
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: OpenWhisk deployment admin
- **Format**: `uuid:password` format
- **Requires**: Create new authorization key in OpenWhisk

### WaddleAI Integration
- **Environment Variables**:
  - `WADDLEAI_BASE_URL`
  - `WADDLEAI_API_KEY`
- **Used By**: ai-interaction module, hub_module
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: WaddleAI system
- **Impact**: AI features offline during rotation

### OpenAI API Key
- **Environment Variable**: `OPENAI_API_KEY`
- **Used By**: ai_interaction_module (when AI_PROVIDER=openai)
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: OpenAI API Dashboard
- **Requires**: Update any dependent systems

## License Management

### License Key
- **Environment Variable**: `LICENSE_KEY`
- **Format**: `PENG-XXXX-XXXX-XXXX-XXXX-ABCD`
- **Used By**: workflow-core module (when RELEASE_MODE=true)
- **Locations to Update**: .env, docker-compose.yml, K8s secrets, Helm values
- **Rotation Source**: PenguinTech License Server (https://license.penguintech.io)
- **Requires**: License validation from PenguinTech

## Rotation Best Practices

### Pre-Rotation Checklist
- [ ] Document current credential versions and rotation dates
- [ ] Notify all stakeholders of planned maintenance
- [ ] Schedule rotation during low-traffic periods
- [ ] Back up current secrets
- [ ] Prepare rollback plan
- [ ] Test new credentials in staging environment first
- [ ] Gather new credentials from all third-party sources

### Rotation Steps
1. **Create new credentials** at source (platform dashboards, cloud consoles, etc.)
2. **Update local .env file** (for development)
3. **Update docker-compose.yml** if needed (for Docker local testing)
4. **Update K8s secrets** in both manifests and Helm templates
5. **Stage changes** and verify in development environment
6. **Deploy to staging** and perform integration testing
7. **Deploy to production** during maintenance window
8. **Verify all services** are healthy after rotation
9. **Invalidate old credentials** at source
10. **Document rotation** in AUDIT log with timestamp and technician
11. **Notify team** of successful rotation

### Audit Logging
All credential rotations must be logged in structured audit logs:
```
[timestamp] AUDIT waddlebot:X.Y.Z credential_rotation community=all action=rotate_credential
result=SUCCESS details="POSTGRES_PASSWORD rotated" technician=username
```

## Environment-Specific Notes

### Development (.env file)
- Credentials are local and not committed to git
- Use simple/weak credentials for local testing
- Rotate before moving to staging
- File is ignored by .gitignore

### Docker Compose (docker-compose.yml)
- NEVER commit actual credentials
- Use environment variable references: `${CREDENTIAL_NAME}`
- No default fallback values (removed: `:-default_value` pattern)
- All credentials must be provided via .env or environment

### Kubernetes (K8s Secrets via Helm)
- **Helm** (`k8s/helm/waddlebot/templates/secrets.yaml`), the only deploy path — Helm is the sole
  Kubernetes deployment mechanism for alpha through production:
  - Values are templated from `values.yaml` / `values-{env}.yaml`
  - Supply real secret values via your cluster's secrets mechanism (Vault, Sealed Secrets, or
    External Secrets Operator) — never plaintext in a values file
  - Deploy via `helm install`/`helm upgrade`
  - Deploy via `helm install/upgrade`

## Compliance & Security

### Storage
- Credentials in .env: LOCAL ONLY, never in git
- Credentials in K8s: Use sealed secrets or external secret management (HashiCorp Vault, AWS Secrets Manager)
- Production credentials: Managed by secure vaults, not in code

### Access Control
- Only authorized personnel can rotate credentials
- All rotation activities logged
- Credentials transmitted over encrypted channels only
- No credentials in CI/CD logs or error messages

### Monitoring
- Monitor failed authentication attempts
- Alert on unexpected credential usage
- Log all successful rotations
- Audit trail for compliance

## Related Documentation
- See [`docs/reference/development-rules.md`](reference/development-rules.md) for general security practices
- See [`docs/reference/environment-variables.md`](reference/environment-variables.md) for complete environment variable reference
- See [`docs/SECRETS_SETUP.md`](SECRETS_SETUP.md) for the license server and secrets-injection setup

---

**Last Updated**: 2025-12-09
**Status**: All hardcoded credentials REMOVED
**Next Action**: Provide secure credential injection mechanism (K8s Secrets, Vault, etc.)
