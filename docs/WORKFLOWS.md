# Waddles GitHub Actions Workflows Documentation

Complete technical reference for Waddles's GitHub Actions CI/CD workflows, covering automated builds, security scanning, version management, and deployment pipelines for 24+ microservices.

---

## Table of Contents

- [Overview](#overview)
- [Workflow Architecture](#workflow-architecture)
- [Core Workflows](#core-workflows)
- [Module Build Workflows](#module-build-workflows)
- [Version Management](#version-management)
- [Security Scanning](#security-scanning)
- [Container Registry](#container-registry)
- [Deployment Workflows](#deployment-workflows)
- [Triggering Workflows](#triggering-workflows)
- [Workflow Dependencies](#workflow-dependencies)
- [Troubleshooting](#troubleshooting)

---

## Overview

Waddles uses a sophisticated multi-workflow CI/CD pipeline that automates:

- **Container builds** for 24+ microservices modules
- **Multi-language security scanning** (Python, JavaScript, Go, Kotlin)
- **Version management** via `.version` file monitoring
- **Multi-platform builds** (linux/amd64, linux/arm64)
- **Automated deployments** to Kubernetes clusters
- **Release management** with GitHub Releases

**Current Version**: 0.2.0 (stored in `.version` file at repository root)

**Total Workflow Files**: 35+
- 1 main CI/CD pipeline
- 24+ individual module builds
- 5 desktop/mobile builds
- 5+ infrastructure workflows

---

## Workflow Architecture

### Workflow File Structure

```
.github/workflows/
├── ci-cd.yml                          # Main CI/CD pipeline
├── build-container.yml                # Reusable container build template
├── version-release.yml                # Version release automation
├── containers.yml                     # Container orchestration
│
├── build-router.yml                   # Processing modules
├── build-twitch.yml                   # Trigger/Receiver modules
├── build-discord.yml
├── build-slack.yml
├── build-youtube-live.yml
│
├── build-identity-core.yml            # Core modules (10 files)
├── build-labels-core.yml
├── build-browser-source-core.yml
├── build-community.yml
├── build-reputation.yml
├── build-security-core.yml
├── build-ai-researcher.yml
├── build-analytics-core.yml
├── build-workflow-core.yml
├── build-hub.yml
│
├── build-ai-interaction.yml           # Action/Interactive modules (9 files)
├── build-alias-interaction.yml
├── build-shoutout-interaction.yml
├── build-inventory-interaction.yml
├── build-calendar-interaction.yml
├── build-memories-interaction.yml
├── build-youtube-music-interaction.yml
├── build-spotify-interaction.yml
├── build-loyalty-interaction.yml
│
├── build-svc-ingest.yml               # Rust data-plane (3 files, `-rust` images)
├── build-svc-process.yml
├── build-svc-action.yml
│
├── android.yml                        # Android validation
└── pr-validation.yml                  # Pull request and merge-queue tests
```

### Workflow Trigger Strategy

**Path-based Triggers**: Each workflow monitors specific paths
```yaml
on:
  push:
    paths:
      - 'processing/router_module/**'
      - 'libs/flask_core/**'
      - '.version'
      - '.github/workflows/build-router.yml'
```

**Branch-based Triggers**: Different behavior per branch
- `main`: Production-ready builds with beta/release tags
- `develop`: Development builds with alpha tags
- `feature/**`, `bugfix/**`: Feature branch builds with alpha tags

**Manual Triggers**: All workflows support `workflow_dispatch` for manual execution

---

## Core Workflows

### 1. Main CI/CD Pipeline (`ci-cd.yml`)

The comprehensive pipeline that runs security checks, builds containers, and deploys to production.

**Triggers**:
- Push to `main` or `develop` branches
- Pull requests to `main` or `develop`
- Manual workflow dispatch
- Changes to `.version` file

**Jobs Sequence**:

1. **Security & Code Quality** (runs first, parallel checks)
   ```yaml
   jobs:
     security:
       - Trivy filesystem scan
       - CodeQL analysis (Go, Java, JavaScript)
       - pip-audit (Python dependencies)
       - npm audit (Node.js dependencies)
       - Dependency Review (PRs only, fails on high severity)
   ```

2. **Container Builds** (after security passes)
   ```yaml
   containers:
     strategy:
       matrix:
         service: [router_module, marketplace_module, ...]
     - Build multi-platform images (amd64, arm64)
     - Push to GitHub Container Registry
     - Run container security scans
   ```

3. **Android App Build** (parallel with containers)
   ```yaml
   android:
     - Setup JDK 17 and Android SDK
     - Run tests and lint
     - Build debug and release APKs
     - Upload artifacts
   ```

4. **Pull Request Validation**
   ```yaml
   pr-validation:
     - Python unit tests
     - Marketplace refund authorization tests
     - Hub frontend tests and build
     - Website build
   ```

5. **Performance Tests** (main branch only)
   ```yaml
   performance:
     - Go benchmarks
     - Upload results
   ```

6. **Deployment** (main branch only, after all builds)
   ```yaml
   deploy:
     - Deploy to staging
     - Run smoke tests
     - Deploy to production
   ```

7. **Kubernetes Deployment** (main branch only)
   ```yaml
   deploy-k8s:
     - Install kubectl and Helm
     - Configure kubeconfig from secrets
     - Deploy with Helm
     - Verify rollout
     - Run smoke tests
   ```

**Environment Variables**:
```yaml
env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}
```

**Required Secrets**:
- `GITHUB_TOKEN` (automatic)
- `KUBE_CONFIG_DATA` (base64-encoded kubeconfig)
- `K8S_NAMESPACE` (optional, defaults to 'waddlebot')
- `KEYSTORE_PASSWORD` (Android builds)
- `KEY_PASSWORD` (Android builds)

---

### 2. Reusable Container Build Template (`build-container.yml`)

A reusable workflow template used by all module builds to ensure consistency.

**Purpose**: Shared logic for building, tagging, and pushing container images

**Inputs**:
```yaml
inputs:
  module_path:
    required: true
    type: string
    description: 'Path to module directory (e.g., processing/router_module)'
  module_name:
    required: true
    type: string
    description: 'Container name (e.g., router)'
  module_port:
    required: true
    type: string
    description: 'Module port (e.g., 8000)'
```

**Version Determination Logic**:

| Branch Context              | `.version` Changed | Tag Suffix | Latest Tag |
|----------------------------|--------------------|------------|------------|
| main + .version changed    | YES                | (none)     | YES        |
| main + regular commit      | NO                 | -beta      | NO         |
| develop + any commit       | NO/YES             | -alpha     | NO         |
| feature/* + any commit     | NO/YES             | -alpha     | NO         |
| bugfix/* + any commit      | NO/YES             | -alpha     | NO         |

**Jobs**:

1. **Determine Version** - Reads `.version` file and sets tag suffix
   ```bash
   VERSION=$(cat .version | tr -d '[:space:]')
   # Check if .version file changed
   if git diff HEAD^ HEAD --name-only | grep -q "^.version$"; then
     IS_RELEASE="true"
   fi
   ```

2. **Build and Push** - Builds multi-platform image
   ```yaml
   - Set up Docker Buildx
   - Log in to Container Registry
   - Extract metadata and tags
   - Build and push Docker image
     platforms: linux/amd64,linux/arm64
     cache-from: type=gha,scope=${{ inputs.module_name }}
   ```

**Build Features**:
- Multi-platform support (AMD64, ARM64)
- Docker Buildx for performance
- Per-module GitHub Actions cache scopes
- Build arguments (MODULE_NAME, MODULE_PORT)
- Automatic tagging based on version and branch

**Example Output**:
```
Image: ghcr.io/owner/waddlebot/router
Tags: v0.2.0, latest
Version: v0.2.0
```

---

### 3. Version Release Workflow (`version-release.yml`)

Automates GitHub Release creation when `.version` file changes on main branch.

**Triggers**:
```yaml
on:
  push:
    branches: [main]
    paths: ['.version']
```

**Workflow Steps**:

1. **Check Version File**
   ```bash
   VERSION=$(cat .version | tr -d '[:space:]')
   SEMVER=$(echo "$VERSION" | cut -d'.' -f1-3)
   ```

2. **Verify Not Default Version**
   - Skips if version is `0.0.0`
   - Prevents accidental releases

3. **Check Release Doesn't Exist**
   ```bash
   gh release view "v$VERSION" > /dev/null 2>&1
   ```

4. **Generate Release Notes**
   ```markdown
   ## Pre-Release v0.2.0

   **Version Details:**
   - Semantic Version: `0.2.0`
   - Full Version: `0.2.0`
   - Commit: `abc123...`
   - Branch: `main`

   ### Changes
   See commit history for detailed changes since last release.
   ```

5. **Create Pre-Release**
   ```bash
   gh release create "v$VERSION" \
     --title "v$VERSION" \
     --notes-file release_notes.md \
     --prerelease \
     --target ${{ github.sha }}
   ```

**Release Triggers Downstream**:
- All 24+ module workflows rebuild with release tags
- Containers tagged as `v0.2.0` AND `latest`
- Deployment workflows can reference stable release

---

### 4. Container Orchestration (`containers.yml`)

Advanced workflow with change detection and matrix builds for all modules.

**Change Detection**:
```yaml
detect-changes:
  - Uses dorny/paths-filter@v2
  - Detects which modules changed
  - Only builds affected modules
  - Reduces build time
```

**Module Categories**:

1. **Core Modules**: router, marketplace, identity, labels, security, credentials
2. **Collector Modules**: twitch, discord, slack
3. **Interaction Modules**: ai, inventory, labels, alias, shoutout

**Matrix Build Strategy**:
```yaml
strategy:
  matrix:
    include:
      - module: router_module
        name: router
        port: 8000
        condition: ${{ needs.detect-changes.outputs.router == 'true' }}
```

**Container Tests**:
```bash
# Test built image
docker run --rm --name test-router -d -p 8000:8000 ghcr.io/owner/waddlebot/router:latest
sleep 10

# Health check
curl -f http://localhost:8000/health

# Stop container
docker stop test-router
```

**Integration Tests**:
```yaml
integration-tests:
  - Start services with docker-compose
  - Wait for services to be ready
  - Test health endpoints
  - Cleanup
```

**Security Scanning**:
```yaml
security-scan:
  strategy:
    matrix:
      image: [router, marketplace, portal, ...]
  - Run Trivy scanner on each image
  - Upload SARIF results to GitHub Security
```

---

## Module Build Workflows

### Pattern for All Module Builds

Each of 24+ modules has a dedicated workflow file following this pattern:

**File**: `.github/workflows/build-{module-name}.yml`

**Example**: `build-router.yml`
```yaml
name: Build router

on:
  push:
    branches: [ main, develop, 'feature/**', 'bugfix/**' ]
    paths:
      - 'processing/router_module/**'
      - 'libs/flask_core/**'
      - '.version'
      - '.github/workflows/build-router.yml'
      - '.github/workflows/build-container.yml'
  pull_request:
    branches: [ main, develop ]
    paths:
      - 'processing/router_module/**'
      - 'libs/flask_core/**'
  workflow_dispatch:

jobs:
  build:
    name: Build router Container
    uses: ./.github/workflows/build-container.yml
    with:
      module_path: 'processing/router_module'
      module_name: 'router'
      module_port: '8000'
    secrets: inherit
```

### Module Categories

**Processing Modules** (1):
- `build-router.yml` - Central request routing (port 8000)

**Trigger/Receiver Modules** (4):
- `build-twitch.yml` - Twitch EventSub webhooks (port 8010)
- `build-discord.yml` - Discord bot events (port 8011)
- `build-slack.yml` - Slack event handling (port 8012)
- `build-youtube-live.yml` - YouTube LiveChat (port 8013)

**Core Modules** (10):
- `build-workflow-core.yml` - Workflow automation (port 8070)
- `build-identity-core.yml` - Cross-platform identity (port 8050)
- `build-labels-core.yml` - Label/tag system (port 8051)
- `build-browser-source-core.yml` - OBS browser source (port 8052)
- `build-community.yml` - Community management (port 8053)
- `build-reputation.yml` - User reputation (port 8054)
- `build-security-core.yml` - Content moderation (port 8055)
- `build-ai-researcher.yml` - AI research tools (port 8056)
- `build-analytics-core.yml` - Analytics (port 8057)
- `build-hub.yml` - Admin portal (port 8060)

**Action/Interactive Modules** (9):
- `build-ai-interaction.yml` - AI responses (port 8005)
- `build-alias-interaction.yml` - Command aliases (port 8006)
- `build-shoutout-interaction.yml` - User shoutouts (port 8007)
- `build-inventory-interaction.yml` - Inventory management (port 8008)
- `build-calendar-interaction.yml` - Event scheduling (port 8009)
- `build-memories-interaction.yml` - Memories/quotes (port 8014)
- `build-youtube-music-interaction.yml` - YouTube Music (port 8015)
- `build-spotify-interaction.yml` - Spotify integration (port 8016)
- `build-loyalty-interaction.yml` - Loyalty points (port 8017)

**Rust Data-Plane Modules** (3) — `-rust`-suffixed images published alongside
the still-deployed Python alpha builds; see `core/<module>/Dockerfile.rust`:
- `build-svc-ingest.yml` - `svc-ingest-rust` (port 8200)
- `build-svc-process.yml` - `svc-process-rust` (port 8201)
- `build-svc-action.yml` - `svc-action-rust` (port 8202)

---

## Version Management

### .version File

**Location**: `/home/penguin/code/Waddles/.version`

**Format**: `vMajor.Minor.Patch` (e.g., `0.2.0`)

**Current Version**: `0.2.0`

### Version Update Methods

**Using update-version.sh script**:
```bash
# Increment build epoch (0.2.0 -> 0.2.0+1)
./scripts/version/update-version.sh

# Increment patch (0.2.0 -> 0.2.1)
./scripts/version/update-version.sh patch

# Increment minor (0.2.0 -> 0.3.0)
./scripts/version/update-version.sh minor

# Increment major (0.2.0 -> 1.0.0)
./scripts/version/update-version.sh major
```

**Manual Update**:
```bash
# Navigate to repository root
cd /home/penguin/code/Waddles

# Edit version file
echo "0.3.0" > .version

# Verify update
cat .version

# Commit and push
git add .version
git commit -m "Release v0.3.0"
git push origin main
```

### Version Release Process

**Complete flow when .version changes on main**:

1. **Commit Version Change**
   ```bash
   git add .version
   git commit -m "Release v0.3.0"
   git push origin main
   ```

2. **version-release.yml Workflow Triggers**
   - Detects `.version` file change
   - Creates GitHub Release with tag `v0.3.0`
   - Marks as pre-release
   - Generates automated release notes

3. **All Module Workflows Rebuild**
   - Each `build-{module}.yml` detects `.version` change
   - Triggers reusable `build-container.yml`
   - Version determination logic sees release
   - Containers tagged as `v0.3.0` AND `latest`

4. **Container Registry Updated**
   ```
   ghcr.io/owner/waddlebot/router:v0.3.0
   ghcr.io/owner/waddlebot/router:latest
   ... (24+ modules)
   ```

### Semantic Versioning Format

```
vMajor.Minor.Patch.Epoch64
├─ Major: Breaking changes, API changes
├─ Minor: Significant new features
├─ Patch: Bug fixes, security patches
└─ Epoch64: Unix timestamp at build time (container labels)
```

**Version History**:
- `0.1.0`: Initial microservices migration
- `0.2.0`: Core modules implementation (current)
- Future `1.0.0`: Production release

---

## Security Scanning

### Multi-Language Security Strategy

Waddles implements comprehensive security scanning across all supported languages.

### Python Security

**Tools**:
- `pip-audit`: Dependency vulnerability scanning
- `bandit`: Code security issue detection
- `mypy`: Type checking (strict mode)
- `flake8`, `black`, `isort`: Code quality

**CI/CD Integration**:
```yaml
- name: Run pip-audit
  run: |
    pip install pip-audit
    find . -name requirements.txt -exec pip-audit -r {} \; || true

- name: Run bandit
  run: |
    pip install bandit
    bandit -r core/ action/ processing/ admin/
```

**Local Execution**:
```bash
# Run bandit for security issues
bandit -r core/ action/ processing/ admin/

# Run pip-audit for dependency vulnerabilities
pip-audit --fix

# Type checking with mypy
mypy --strict core/ action/
```

### JavaScript Security

**Tools**:
- `npm audit`: Dependency vulnerability scanning
- ESLint with security plugins
- Prettier for code formatting

**CI/CD Integration**:
```yaml
- name: Run npm audit
  run: |
    cd admin/hub_module/backend && npm audit --production --audit-level=moderate || true
    cd ../frontend && npm audit --production --audit-level=moderate || true
```

**Local Execution**:
```bash
# NPM audit
npm audit --production --audit-level=moderate

# Fix vulnerabilities
npm audit fix

# ESLint security checks
npm run lint
```

### Go Security

**Tools**:
- `gosec`: Go security checker
- `golangci-lint`: Comprehensive Go linter

**CI/CD Integration**:
```yaml
- name: Run Go vet
  run: go vet ./...
  working-directory: Premium/Desktop

- name: Run Go fmt check
  run: |
    if [ "$(gofmt -s -l . | wc -l)" -gt 0 ]; then
      echo "Code is not formatted properly"
      gofmt -s -l .
      exit 1
    fi
```

**Local Execution**:
```bash
# gosec for security issues
gosec ./...

# golangci-lint
golangci-lint run
```

### Kotlin Security

**Tools**:
- ktlint: Kotlin linter
- detekt: Code quality and security

**CI/CD Integration**:
```yaml
- name: Run Android lint
  run: ./gradlew lint
  working-directory: Premium/Android
```

**Local Execution**:
```bash
# Kotlin linting via ktlint
ktlint src/

# Detekt for code quality
detekt -i src/
```

### Container Security

**Trivy Scanner**:

**Filesystem Scan**:
```yaml
- name: Run Trivy vulnerability scanner
  uses: aquasecurity/trivy-action@0.28.0
  with:
    scan-type: 'fs'
    scan-ref: '.'
    format: 'sarif'
    output: 'trivy-results.sarif'
```

**Container Image Scan**:
```yaml
- name: Run container security scan
  uses: aquasecurity/trivy-action@0.28.0
  with:
    image-ref: ghcr.io/owner/waddlebot/router:latest
    format: 'sarif'
    output: 'trivy-router.sarif'
```

**SARIF Upload** (creates GitHub Security alerts):
```yaml
- name: Upload Trivy scan results
  uses: github/codeql-action/upload-sarif@v2
  if: always()
  with:
    sarif_file: 'trivy-results.sarif'
```

### CodeQL Analysis

**Supported Languages**: Go, Java, JavaScript

**CI/CD Integration**:
```yaml
- name: Run CodeQL Analysis
  uses: github/codeql-action/analyze@v2
  with:
    languages: go, java, javascript
```

**Features**:
- Detects code vulnerabilities
- Creates security alerts in GitHub Security tab
- Automatic remediation suggestions

### Dependency Review

**PR-Only Check** (blocks PRs with high-severity vulnerabilities):
```yaml
- name: Dependency Review
  uses: actions/dependency-review-action@v4
  if: github.event_name == 'pull_request'
  with:
    fail-on-severity: high
```

**Features**:
- Reviews dependency changes in PRs
- Blocks merge if high/critical vulnerabilities detected
- Provides remediation guidance

---

## Container Registry

### GitHub Container Registry (GHCR)

**Registry URL**: `ghcr.io/{owner}/waddlebot/{module}`

**Authentication**:
```yaml
- name: Log in to Container Registry
  uses: docker/login-action@v3
  with:
    registry: ghcr.io
    username: ${{ github.actor }}
    password: ${{ secrets.GITHUB_TOKEN }}
```

### Image Naming Convention

**Format**: `ghcr.io/{owner}/waddlebot/{module-name}:{tag}`

**Examples**:
```
ghcr.io/penguintechinc/waddlebot/router:v0.2.0
ghcr.io/penguintechinc/waddlebot/router:latest
ghcr.io/penguintechinc/waddlebot/ai-interaction:v0.2.0-beta
ghcr.io/penguintechinc/waddlebot/discord:v0.2.0-alpha
```

### Tag Strategy by Build Type

**Feature Branch Push** (e.g., `feature/new-command`):
```
ghcr.io/penguintechinc/waddlebot/router:v0.2.0-alpha
```

**Develop Branch Push**:
```
ghcr.io/penguintechinc/waddlebot/router:v0.2.0-alpha
```

**Main Branch Push** (regular commit, no `.version` change):
```
ghcr.io/penguintechinc/waddlebot/router:v0.2.0-beta
```

**Version Release** (`.version` changed on main):
```
ghcr.io/penguintechinc/waddlebot/router:v0.2.0
ghcr.io/penguintechinc/waddlebot/router:latest
```

### Multi-Platform Builds

**Platforms**:
- `linux/amd64` (Intel/AMD x86_64)
- `linux/arm64` (ARM-based systems, Apple Silicon)

**Build Configuration**:
```yaml
- name: Build and push Docker image
  uses: docker/build-push-action@v5
  with:
    platforms: linux/amd64,linux/arm64
    cache-from: type=gha,scope=${{ inputs.module_name }}
    cache-to: type=gha,mode=max,scope=${{ inputs.module_name }}
```

**Benefits**:
- Single manifest supports both architectures
- Automatic platform selection during `docker pull`
- Optimized for cloud providers (AWS Graviton, GCP Tau)

### Build Caching

**GitHub Actions Cache**:
```yaml
cache-from: type=gha,scope=router
cache-to: type=gha,mode=max,scope=router
```

**Per-Module Scopes**: Each module has isolated cache
- Prevents cache pollution between modules
- Faster builds (reuses layers)
- Automatic cache expiration (7 days unused)

---

## Deployment Workflows

### Helm Chart Kind E2E (`helm-chart-e2e.yml`)

**Triggers**: PRs touching `k8s/helm/**` or chart-relevant Dockerfiles; `workflow_dispatch`

Installs/upgrades `k8s/helm/waddlebot` on an ephemeral kind cluster with
dummy platform credentials (`values-ci.yaml`) -- catches hook-ordering,
missing-binary, and live-object bugs `helm lint`/`helm template` can't see.
Shares kind/Helm install + image build steps with the live workflow below
via `.github/actions/kind-helm-e2e-setup`.

### Live Discord E2E (`live-discord-e2e.yml`)

**Triggers**: Push to `release/**`; `workflow_dispatch` — never `pull_request`

Installs the same chart with a **real** dedicated CI Discord bot token
(`ci-live-credentials` environment) and proves a genuine round trip: real
gateway auth, then a `!ping`/`pong` exchange through real Discord REST
calls. Full setup, secret rotation, and round-trip design: see
[`docs/CI_LIVE_TESTS.md`](CI_LIVE_TESTS.md).

### Kubernetes Deployment (`deploy-k8s` job in `ci-cd.yml`)

**Triggers**: Push to main branch after successful container builds

**Prerequisites**:
- `KUBE_CONFIG_DATA`: Base64-encoded kubeconfig in GitHub Secrets
- `K8S_NAMESPACE`: Target namespace (optional, defaults to 'waddlebot')

**Deployment Steps**:

1. **Wait for Image Propagation**
   ```yaml
   - name: Wait for GHCR propagation
     run: |
       echo "Waiting 30 seconds for images to be available in GHCR..."
       sleep 30
   ```

2. **Install Tools**
   ```yaml
   - name: Install kubectl
     uses: azure/setup-kubectl@v3
     with:
       version: 'v1.28.0'

   - name: Install Helm
     uses: azure/setup-helm@v3
     with:
       version: 'v3.13.0'
   ```

3. **Configure Kubernetes Access**
   ```yaml
   - name: Configure kubeconfig
     run: |
       mkdir -p ~/.kube
       echo "${{ secrets.KUBE_CONFIG_DATA }}" | base64 -d > ~/.kube/config
       chmod 600 ~/.kube/config
   ```

4. **Verify Cluster Connectivity**
   ```yaml
   - name: Verify cluster connectivity
     run: |
       kubectl cluster-info
       kubectl get nodes
   ```

5. **Deploy with Helm**
   ```bash
   REPO_OWNER="${{ github.repository_owner }}"
   REPO_NAME="${{ github.event.repository.name }}"
   NAMESPACE="${{ secrets.K8S_NAMESPACE || 'waddlebot' }}"

   helm upgrade --install waddlebot ./k8s/helm/waddlebot \
     --namespace $NAMESPACE \
     --create-namespace \
     --set global.imageRegistry=ghcr.io/${REPO_OWNER}/${REPO_NAME} \
     --set global.imageTag=${{ github.sha }} \
     --set global.imagePullPolicy=Always \
     --timeout 15m \
     --wait
   ```

6. **Verify Rollout**
   ```bash
   kubectl rollout status deployment -n waddlebot --timeout=10m
   kubectl get pods -n waddlebot
   kubectl get deployments -n waddlebot
   ```

7. **Run Smoke Tests**
   ```bash
   # Test router health
   kubectl run curl-test --image=curlimages/curl:latest --rm -i --restart=Never \
     --namespace=waddlebot \
     -- curl -f http://router:8000/health

   # Test hub health
   kubectl run curl-test-hub --image=curlimages/curl:latest --rm -i --restart=Never \
     --namespace=waddlebot \
     -- curl -f http://hub:8060/api/health
   ```

**Deployment Output**:
```
=== Deployment Summary ===
NAME                         READY   STATUS    RESTARTS   AGE
router-xxxxx                 2/2     Running   0          2m
hub-xxxxx                    2/2     Running   0          2m
identity-core-xxxxx          2/2     Running   0          2m
...

=== Recent Events ===
(Last 20 events from namespace)
```

### Desktop and Mobile Builds

**Android Build** (`android.yml`):
```yaml
- Setup JDK 17 and Android SDK
- Cache Gradle packages
- Run tests: ./gradlew test
- Run lint: ./gradlew lint
- Build APKs: ./gradlew assembleDebug assembleRelease
- Upload artifacts
```

Legacy desktop workflows were removed when their source projects were retired.

---

## Triggering Workflows

### Automatic Triggers

**Push Events**:
```yaml
on:
  push:
    branches: [ main, develop, 'feature/**', 'bugfix/**' ]
    paths:
      - 'processing/router_module/**'
      - 'libs/flask_core/**'
      - '.version'
```

**Pull Request Events**:
```yaml
on:
  pull_request:
    branches: [ main, develop ]
    paths:
      - 'processing/router_module/**'
```

**Version File Changes**:
```yaml
on:
  push:
    branches: [main]
    paths: ['.version']
```

### Manual Triggers

**Trigger via GitHub UI**:
1. Navigate to Actions tab
2. Select workflow (e.g., "Build router")
3. Click "Run workflow"
4. Select branch
5. Click "Run workflow" button

**Trigger via GitHub CLI**:
```bash
# Trigger specific workflow
gh workflow run build-router.yml --ref main

# Trigger with inputs
gh workflow run ci-cd.yml --ref main

# List workflow runs
gh run list --workflow=build-router.yml
```

**Trigger via API**:
```bash
curl -X POST \
  -H "Accept: application/vnd.github.v3+json" \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/owner/Waddles/actions/workflows/build-router.yml/dispatches \
  -d '{"ref":"main"}'
```

### Workflow Dispatch Inputs

**Example with inputs**:
```yaml
on:
  workflow_dispatch:
    inputs:
      environment:
        description: 'Deployment environment'
        required: true
        default: 'staging'
        type: choice
        options:
          - staging
          - production
      skip-tests:
        description: 'Skip tests'
        required: false
        type: boolean
        default: false
```

---

## Workflow Dependencies

### Job Dependencies

**Sequential Execution**:
```yaml
jobs:
  security:
    runs-on: ubuntu-latest
    # Runs first

  containers:
    needs: security
    # Waits for security to complete

  deploy:
    needs: [containers, android, desktop-bridge]
    # Waits for all builds to complete
```

**Conditional Execution**:
```yaml
deploy:
  needs: containers
  if: github.ref == 'refs/heads/main' && github.event_name == 'push'
  # Only runs on main branch pushes
```

### Workflow Call Dependencies

**Reusable Workflow Pattern**:
```yaml
# build-router.yml
jobs:
  build:
    uses: ./.github/workflows/build-container.yml
    with:
      module_path: 'processing/router_module'
      module_name: 'router'
      module_port: '8000'
    secrets: inherit
```

**Dependency Chain**:
```
1. build-router.yml triggered (by path change)
2. Calls build-container.yml
3. build-container.yml determines version
4. build-container.yml builds and pushes image
5. version-release.yml triggers (if .version changed)
6. GitHub Release created
7. ci-cd.yml deploy-k8s job triggers
8. Kubernetes deployment updated
```

### Inter-Workflow Communication

**Outputs from Called Workflow**:
```yaml
# build-container.yml
jobs:
  determine-version:
    outputs:
      version: ${{ steps.version.outputs.version }}
      is_release: ${{ steps.version.outputs.is_release }}

# Caller workflow
jobs:
  build:
    uses: ./.github/workflows/build-container.yml
    # Access outputs in subsequent jobs
```

**Artifacts Between Jobs**:
```yaml
# Upload in job 1
- name: Upload artifacts
  uses: actions/upload-artifact@v3
  with:
    name: android-apks
    path: Premium/Android/app/build/outputs/apk/

# Download in job 2
- name: Download artifacts
  uses: actions/download-artifact@v3
  with:
    name: android-apks
```

---

## Troubleshooting

### Common Issues

#### 1. Build Fails

**Symptoms**: Workflow fails during container build

**Diagnosis**:
```bash
# Check GitHub Actions logs
# Navigate to: https://github.com/owner/Waddles/actions
# Click failed workflow run
# Review job logs
```

**Common Causes**:
- Dockerfile syntax errors
- Missing dependencies in `libs/flask_core`
- Build context issues
- Registry authentication failures

**Solutions**:
```bash
# Verify Dockerfile locally
docker build -t test-router -f processing/router_module/Dockerfile .

# Check libs dependency
ls -la libs/flask_core/

# Test registry login
echo $GITHUB_TOKEN | docker login ghcr.io -u $GITHUB_USER --password-stdin
```

#### 2. Wrong Container Tags

**Symptoms**: Container tagged with wrong version or suffix

**Diagnosis**:
```bash
# Check .version file
cat .version

# Check git branch
git rev-parse --abbrev-ref HEAD

# Check .version commit history
git log -1 --oneline .version
```

**Common Causes**:
- `.version` file not committed
- Workflow triggered on wrong branch
- Git diff detection failing

**Solutions**:
```bash
# Verify .version is committed
git status .version

# Check recent commits
git log --oneline -5

# Re-commit .version if needed
git add .version
git commit -m "Update version to 0.2.1"
git push origin main
```

#### 3. Missing Latest Tag

**Symptoms**: Image built but no `latest` tag

**Expected Behavior**:
- `latest` tag ONLY applied when `.version` changes on main
- Regular main commits get `-beta` suffix
- Non-main branches get `-alpha` suffix

**Verification**:
```bash
# Check if .version changed in last commit
git diff HEAD^ HEAD .version

# Check workflow run logs
# Look for: "Building RELEASE: v0.2.0 and latest"
```

#### 4. Security Scan Failures

**Symptoms**: Security job fails with vulnerabilities

**Diagnosis**:
```yaml
# Check Trivy output in GitHub Actions
# Navigate to Security tab -> Code scanning alerts
```

**Solutions**:
```bash
# Run Trivy locally
trivy fs .

# Fix Python vulnerabilities
pip-audit --fix

# Fix npm vulnerabilities
cd admin/hub_module/frontend
npm audit fix

# Re-run workflow
gh workflow run ci-cd.yml --ref main
```

#### 5. Kubernetes Deployment Fails

**Symptoms**: `deploy-k8s` job fails

**Diagnosis**:
```bash
# Check if kubeconfig secret is set
gh secret list

# Verify kubeconfig format
echo $KUBE_CONFIG_DATA | base64 -d | kubectl --kubeconfig=- cluster-info
```

**Common Causes**:
- Invalid kubeconfig in secrets
- Cluster unreachable
- Insufficient permissions
- Image pull failures

**Solutions**:
```bash
# Update kubeconfig secret
cat ~/.kube/config | base64 | gh secret set KUBE_CONFIG_DATA

# Test cluster connectivity
kubectl cluster-info

# Check image exists in registry
docker manifest inspect ghcr.io/owner/waddlebot/router:latest

# Verify namespace exists
kubectl get namespace waddlebot
```

#### 6. Workflow Not Triggering

**Symptoms**: Expected workflow doesn't run

**Diagnosis**:
```yaml
# Check path filters in workflow file
cat .github/workflows/build-router.yml

# Verify changed files
git diff HEAD^ HEAD --name-only
```

**Common Causes**:
- Changed files don't match path filters
- Branch not in trigger list
- Workflow disabled

**Solutions**:
```bash
# Trigger manually
gh workflow run build-router.yml --ref main

# Check workflow is enabled
# GitHub UI: Actions -> Select workflow -> Enable if disabled

# Verify paths match
# If router_module changed, build-router.yml should trigger
```

### Debugging Commands

**View Workflow Runs**:
```bash
# List recent runs
gh run list --limit 10

# View specific run
gh run view <run-id>

# Watch running workflow
gh run watch
```

**Check Workflow Status**:
```bash
# Get workflow status
gh workflow view ci-cd.yml

# List all workflows
gh workflow list

# Enable/disable workflow
gh workflow enable ci-cd.yml
gh workflow disable ci-cd.yml
```

**Download Logs**:
```bash
# Download logs for run
gh run download <run-id>

# View logs in terminal
gh run view <run-id> --log
```

---

## Best Practices

### Development Workflow

1. **Create Feature Branch**
   ```bash
   git checkout -b feature/new-command
   ```

2. **Make Changes**
   - Edit module code
   - Update tests
   - Verify locally

3. **Commit and Push**
   ```bash
   git add .
   git commit -m "feat: add new command"
   git push origin feature/new-command
   ```

4. **Automated Checks Run**
   - Module build workflow triggers (alpha tag)
   - Security scans execute
   - Container built and pushed

5. **Create Pull Request**
   - All CI checks must pass
   - Dependency review runs
   - Code review required

6. **Merge to Main**
   - Security checks re-run
   - Containers built with beta tag
   - Optionally deployed to staging

7. **Version Release** (when ready)
   ```bash
   ./scripts/version/update-version.sh minor
   git add .version
   git commit -m "Release v0.3.0"
   git push origin main
   ```

8. **Automatic Release Flow**
   - GitHub Release created
   - All containers rebuilt with release tags
   - Latest tags applied
   - Production deployment triggered

---

## Related Documentation

- **STANDARDS.md**: Microservices architecture patterns and best practices
- **KUBERNETES.md**: Kubernetes deployment and operations guide
- **CLAUDE.md**: Project context and development guidelines
- **docs/api-reference.md**: Complete API endpoint documentation
- **k8s/GITHUB_ACTIONS.md**: GitHub Actions Kubernetes setup guide

---

**Last Updated**: 2025-12-16
**Waddles Version**: 0.2.0
**Total Workflow Files**: 35+
**Module Build Workflows**: 24+
**Container Platforms**: 2 (amd64, arm64)
**Security Scanners**: 6 (Trivy, CodeQL, pip-audit, npm audit, gosec, bandit)
