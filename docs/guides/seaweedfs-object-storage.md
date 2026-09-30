# Waddles SeaweedFS S3-Compatible Storage Setup

This document describes how to set up and use SeaweedFS (`chrislusf/seaweedfs`, Apache-2.0) as the S3-compatible object store for Waddles image management, bundle artifacts, and CDN functionality. SeaweedFS replaces MinIO (see `k8s/helm/waddlebot/README.md` for the chart-level migration notes) — its S3 gateway is AWS-S3-API-compatible, so the stock `aws-cli`/`boto3`/`mc` all work unmodified.

## Quick Start (Development)

### 1. Start the Development Environment

```bash
# Start all services including SeaweedFS
docker-compose up -d

# Check service status
docker-compose ps
```

### 2. Access Services

- **Waddles Hub**: http://localhost:8060
- **SeaweedFS S3 API**: http://localhost:8333
- **SeaweedFS Master UI**: http://localhost:9333

**SeaweedFS Credentials** (dev defaults, see `.env.example`):
- Access Key: `waddlebot`
- Secret Key: `waddlebot-dev-secret`

### 3. Test Image Upload

1. Log into the hub at http://localhost:8060
2. Navigate to **Images** from the menu
3. Upload a test image
4. Verify it's reachable via its public URL (`S3_PUBLIC_BASE_URL`)

## Architecture Overview

```
┌─────────────────┐                          ┌─────────────────┐
│   Waddles       │◄────────────────────────►│    SeaweedFS    │
│   Hub Module    │                          │   S3 Gateway    │
│                 │                          │                 │
│ - Image Upload  │                          │ - Object Store  │
│ - Bundle Store  │                          │ - SSE-S3 (KEK)  │
│ - Management    │                          │ - Master/Filer  │
└─────────────────┘                          └─────────────────┘
         │
┌─────────────────┐
│   PostgreSQL    │
│   Database      │
│                 │
│ - Image Meta    │
│ - User Data     │
│ - Communities   │
└─────────────────┘
```

## Storage Structure

SeaweedFS organizes objects in the same key layout MinIO used:

```
waddlebot-assets/
├── avatars/<uuid>.<ext>
├── community-logos/<uuid>.<ext>
├── community-banners/<uuid>.<ext>
├── bundles/<app_id>/<version>/<sha256>.wasm    # content-addressed bundle components
└── bundles/<app_id>/<version>/<sha256>.json    # bundle sidecar metadata

recordings/
└── <stream recordings from svc-streaming>
```

## Encryption at Rest (SSE-S3 KEK)

Outside alpha/local, the chart's `infrastructure.seaweedfs.encryption` fails Helm templating closed unless a KEK Secret exists first (`security.md` Encryption is mandatory, not opt-in):

1. Generate the key-encryption-key once per cluster:
   ```bash
   make generate-seaweedfs-sse-key KUBE_CONTEXT=dal2-beta [NAMESPACE=waddlebot]
   # or directly:
   ./scripts/generate-seaweedfs-sse-key.sh --context dal2-beta --namespace waddlebot \
     [--secret-name seaweedfs-sse-kek] [--key-name waddlebot-seaweedfs]
   ```
   This creates/updates a Secret (default `seaweedfs-sse-kek`) holding `WEED_S3_SSE_KEK`, mounted into the SeaweedFS container so `weed server` can decrypt/encrypt objects transparently.
2. Point the chart at it via `infrastructure.seaweedfs.encryption.secretName` (`--set` or an out-of-git values override / ExternalSecret).
3. The `seaweedfs-bucket-init` post-install/post-upgrade Job then applies `put-bucket-encryption` (SSE-S3/AES256) as the bucket's *default*, so objects written without an explicit `ServerSideEncryption` header are still encrypted at rest.

Alpha/local is the only tier allowed to leave `encryption.secretName` unset (documented default in `values-alpha.yaml`) — never disable `infrastructure.seaweedfs.encryption.enabled` to work around a missing key in beta/gamma/production.

## Least-Privilege Identities

SeaweedFS's S3 gateway takes a static `identities.json` (rendered at container start from `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`) rather than MinIO's separate root/IAM-policy model. The chart currently provisions a single `waddlebot` identity with `Read, Write, List, Tagging, Admin` — the same blast radius as the old MinIO root credential. Splitting this into per-consumer identities (e.g. a read-only identity for the bundle executor's poller, a write-only identity for upload paths) is tracked as follow-up work once a consumer actually needs it; don't add a second identity speculatively.

## Bucket Init Hook

`seaweedfs-bucket-init` (`k8s/helm/waddlebot/templates/infrastructure/seaweedfs.yaml`) is a Helm `post-install,post-upgrade` hook Job, `before-hook-creation,hook-succeeded` delete policy (Job specs are immutable in-place, so a plain Job breaks every `helm upgrade` once it exists). It waits for the SeaweedFS Deployment/Service/PVC to exist, then idempotently creates `waddlebot-assets` and `recordings`, and applies default SSE-S3 encryption when `encryption.autoEncryptBucket` is set. Uses the stock `amazon/aws-cli` image against the S3 gateway endpoint — no SeaweedFS-specific client.

## Configuration

### Environment Variables

**Hub/Portal Configuration**:
```bash
# Enable S3 storage
S3_STORAGE_ENABLED=true
S3_BUCKET_NAME=waddlebot-assets
S3_REGION=us-east-1

# SeaweedFS Configuration (for local development)
S3_ENDPOINT_URL=http://infra-seaweedfs:8333
S3_ACCESS_KEY_ID=waddlebot
S3_SECRET_ACCESS_KEY=waddlebot-dev-secret
S3_FORCE_PATH_STYLE=true    # SeaweedFS requires path-style addressing

# Public URLs
S3_PUBLIC_BASE_URL=http://localhost:8333/waddlebot-assets
S3_CDN_BASE_URL=http://localhost/images
```

**Production Configuration** (real AWS S3, no SeaweedFS):
```bash
S3_STORAGE_ENABLED=true
S3_BUCKET_NAME=waddlebot-prod-assets
S3_REGION=us-west-2
# S3_ENDPOINT_URL=""  # Leave empty for AWS S3
S3_ACCESS_KEY_ID=your_aws_access_key
S3_SECRET_ACCESS_KEY=your_aws_secret_key

S3_PUBLIC_BASE_URL=https://s3.us-west-2.amazonaws.com/waddlebot-prod-assets
S3_CDN_BASE_URL=https://cdn.waddlebot.com
```

## Backup and Restore

SeaweedFS has no built-in `mc mirror` equivalent CLI, but since its S3 gateway is AWS-S3-compatible, the stock `aws-cli`/`rclone` work unmodified:

```bash
# Backup: sync every object out of the bucket to local disk
aws --endpoint-url http://localhost:8333 s3 sync s3://waddlebot-assets ./backup/waddlebot-assets/

# Restore: sync local disk back into a (fresh) bucket
aws --endpoint-url http://localhost:8333 s3 sync ./backup/waddlebot-assets/ s3://waddlebot-assets

# Cross-cluster/DR: sync straight bucket-to-bucket via two --endpoint-url invocations,
# or through the same local staging directory above.

# Database backup (image/bundle metadata, independent of object bytes)
docker-compose exec infra-postgres pg_dump -U waddlebot waddlebot > backup.sql
```

Volume-level backup (the underlying PVC/`seaweedfs-data` Docker volume) is also valid since SeaweedFS stores its raw volume files there — take a filesystem/PVC snapshot the same way you would for `infra-postgres`.

## Alpha Migration Note

**MinIO data on alpha cannot be migrated — this is a fresh volume, not an in-place upgrade.** MinIO's own container images (`minio/minio:*`) were pulled from Docker Hub and are no longer available to re-pull, so there is no supported path to spin MinIO back up to read out existing alpha bucket contents before cutting over. Alpha's `minio-data`/`infra-minio-pvc` volume is treated as disposable (alpha is documented as ephemeral/re-buildable in `docs/KUBERNETES.md`):

1. Delete the old `infra-minio-pvc` (or bare Docker volume `waddlebot_minio-data`) — do not attempt to attach it to the new SeaweedFS container, its on-disk format is MinIO-specific and unreadable by `weed server`.
2. Deploy SeaweedFS with its own fresh `infra-seaweedfs-pvc` / `seaweedfs-data` volume — empty by design.
3. Re-seed any alpha-only content (test avatars, sample bundle components) via the normal upload flows or `make seed-mock-data`.
4. Beta/gamma/production were never populated with real user data behind this migration, so no equivalent step is required there beyond the standard bucket-init Job creating fresh buckets.

## Features

### Image Upload and Processing

1. **Multi-Format Support**: JPEG, PNG, GIF, WebP, SVG
2. **Automatic Optimization**: Quality compression and format conversion
3. **Thumbnail Generation**: Multiple sizes (64x64, 128x128, 256x256, 512x512)
4. **Deduplication**: SHA256 hash-based duplicate detection
5. **Validation**: File size, format, and dimensions checking

### CDN and Caching

1. **nginx Proxy**: Static file serving with caching headers
2. **Browser Caching**: 1-year cache for images
3. **CORS Support**: Cross-origin access for web applications
4. **Gzip Compression**: Automatic compression for text assets

### Security and Access Control

1. **Public Read Access**: Images are publicly accessible via CDN URLs
2. **Upload Authentication**: Only authenticated users can upload
3. **Ownership Validation**: Users can only delete their own images
4. **Community Permissions**: Community owners can manage community images
5. **Encryption at Rest**: SSE-S3 via `WEED_S3_SSE_KEK` — see Encryption at Rest above

## API Endpoints

### Image Management
- `GET /images` - Image gallery and management interface
- `POST /images/upload` - Upload new image
- `DELETE /images/delete/<path>` - Delete image (with ownership check)
- `GET /images/info/<path>` - Get image metadata

### CDN Serving
- `GET /cdn/images/<path>` - Serve image (fallback for local storage)
- `GET /images/*` - Direct nginx proxy to SeaweedFS (via nginx config)

### Admin/API
- `POST /api/images/presigned-upload` - Generate presigned upload URL
- `GET /api/images/storage-status` - Get storage service health status

## Development Workflow

### 1. Local Development Setup

```bash
# Clone repository
git clone <repo-url>
cd Waddles

# Start services
docker-compose up -d

# Check logs
docker-compose logs -f hub-api
```

### 2. Testing Image Upload

```bash
# Upload via curl (requires authentication token)
curl -X POST http://localhost:8060/api/v1/images/upload \
  -H "Authorization: Bearer <token>" \
  -F "image_file=@test.jpg" \
  -F "image_type=avatar"

# Check SeaweedFS master UI
open http://localhost:9333
```

### 3. Direct SeaweedFS Access

```bash
# The S3 gateway is AWS-S3-API-compatible -- use aws-cli, no vendor client needed
export AWS_ACCESS_KEY_ID=waddlebot
export AWS_SECRET_ACCESS_KEY=waddlebot-dev-secret

# List buckets and objects
aws --endpoint-url http://localhost:8333 s3 ls
aws --endpoint-url http://localhost:8333 s3 ls s3://waddlebot-assets/avatars/

# Upload file directly
aws --endpoint-url http://localhost:8333 s3 cp test.jpg s3://waddlebot-assets/avatars/test.jpg
```

## Production Deployment

### AWS S3 Setup (real S3, no SeaweedFS)

1. **Create S3 Bucket**:
```bash
aws s3 mb s3://waddlebot-prod-assets --region us-west-2
```

2. **Set Bucket Policy**:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::waddlebot-prod-assets/images/*"
    }
  ]
}
```

3. **Create IAM User**:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::waddlebot-prod-assets",
        "arn:aws:s3:::waddlebot-prod-assets/*"
      ]
    }
  ]
}
```

4. **Setup CloudFront CDN**:
   - Origin: S3 bucket
   - Behaviors: Cache images for 1 year
   - Custom domain: cdn.waddlebot.com

### Other S3-Compatible Services

**DigitalOcean Spaces**:
```bash
S3_ENDPOINT_URL=https://nyc3.digitaloceanspaces.com
S3_REGION=nyc3
S3_BUCKET_NAME=waddlebot-assets
```

**Wasabi**:
```bash
S3_ENDPOINT_URL=https://s3.wasabisys.com
S3_REGION=us-east-1
S3_BUCKET_NAME=waddlebot-assets
```

**Backblaze B2**:
```bash
S3_ENDPOINT_URL=https://s3.us-west-000.backblazeb2.com
S3_REGION=us-west-000
S3_BUCKET_NAME=waddlebot-assets
```

## Monitoring and Maintenance

### Health Checks

```bash
# Check storage service health
curl http://localhost:8060/api/images/storage-status

# Check SeaweedFS master status
curl http://localhost:9333/cluster/status
```

### Logs and Debugging

```bash
# Hub module logs
docker-compose logs -f hub-api

# SeaweedFS logs
docker-compose logs -f infra-seaweedfs

# Check all service health
docker-compose ps
```

## Troubleshooting

### Common Issues

**1. Storage service not connecting**:
- Check SeaweedFS container is running: `docker-compose ps infra-seaweedfs`
- Verify credentials and endpoint URL
- Confirm `S3_FORCE_PATH_STYLE=true` — SeaweedFS rejects virtual-hosted-style addressing
- Check network connectivity between containers

**2. Images not displaying**:
- Verify bucket public read policy
- Check CORS configuration in nginx
- Confirm CDN URL configuration

**3. Upload failures**:
- Check file size limits
- Verify allowed file extensions
- Review portal logs for detailed errors

**4. Performance issues**:
- Enable nginx caching
- Use CDN for production
- Optimize image sizes and formats

### Debug Commands

```bash
# Test SeaweedFS connectivity from hub container
docker-compose exec hub-api wget -qO- http://infra-seaweedfs:9333/cluster/status

# Check hub health
curl http://localhost:8060/health

# Verify bucket contents
aws --endpoint-url http://localhost:8333 s3 ls s3://waddlebot-assets --recursive
```

## Best Practices

1. **Security**:
   - Use strong access keys in production
   - Keep `infrastructure.seaweedfs.encryption` enabled everywhere outside alpha
   - Split identities once a consumer needs less than full Read/Write/List/Admin
   - Monitor access logs

2. **Performance**:
   - Use CDN for global distribution
   - Implement proper caching headers
   - Optimize image sizes and formats
   - Use WebP format when possible

3. **Cost Optimization**:
   - Implement lifecycle policies
   - Clean up unused images

4. **Backup and Recovery**:
   - Regular `aws s3 sync` backups (see Backup and Restore above)
   - Database backup for metadata
   - Test recovery procedures

This setup provides a robust, scalable image and bundle-artifact storage solution for Waddles that works seamlessly in both development and production environments.
