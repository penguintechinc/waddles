# Waddles Workflow Compliance and CI/CD Documentation

## Overview

Waddles uses automated CI/CD workflows with `.version` file monitoring, multi-language security scanning, and containerized builds for 24+ microservices modules. This document provides complete workflow configuration and compliance standards.

**Current Version**: 0.2.0 (stored in `.version` file at repository root)

---

## .WORKFLOW Compliance

### Version File Monitoring

The `.version` file serves as the single source of truth for semantic versioning:

```
Location: /home/penguin/code/Waddles/.version
Format: vMajor.Minor.Patch (e.g., 0.2.0)
Current: 0.2.0
```

**Version Update Methods**:

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

### Epoch64 Timestamp Integration

All container builds include epoch64 build timestamp:
- **Location**: Container labels and metadata
- **Format**: Unix epoch timestamp at build time
- **Purpose**: Track exact build time for compliance and debugging

---

## Module Architecture (24+ Microservices)

### 1. Processing Module (1)
- **router_module** (port 8000)
  - Central request routing and command dispatch
  - Multi-threaded event processing
  - API gateway for all modules

### 2. Trigger/Receiver Modules (4)
Platform integrations that receive webhooks and events:

- **twitch_module** (port 8010)
  - EventSub webhooks for channel events
  - OAuth token management
  - Multi-channel monitoring (1000+ concurrent)

- **discord_module** (port 8011)
  - py-cord bot integration
  - Slash command handling
  - Guild event processing

- **slack_module** (port 8012)
  - Slack SDK event handling
  - Slash command responses
  - Interactive component handling

- **youtube_live_module** (port 8013)
  - YouTube LiveChat integration
  - Stream event monitoring
  - Real-time message handling

### 3. Core Modules (10)
Platform services for identity, reputation, and infrastructure:

- **workflow_core_module** (port 8070)
  - Workflow automation and orchestration
  - Event routing and processing logic
  - State management

- **identity_core_module** (port 8050)
  - Cross-platform identity linking
  - Flask-Security-Too authentication
  - User profile management

- **labels_core_module** (port 8051)
  - High-performance label/tag system
  - User categorization and metadata
  - Query optimization with caching

- **browser_source_core_module** (port 8052)
  - OBS browser source integration
  - WebSocket real-time updates
  - Dynamic overlay rendering

- **community_module** (port 8053)
  - Community management and settings
  - Server configuration storage
  - Member permission management

- **reputation_module** (port 8054)
  - User activity and reputation tracking
  - Point system and achievements
  - Leaderboard generation

- **security_core_module** (port 8055)
  - Content moderation and filtering
  - Spam detection
  - Abuse prevention

- **ai_researcher_module** (port 8056)
  - AI model evaluation and testing
  - Provider integration (Ollama, OpenAI, MCP)
  - Research utilities

- **analytics_core_module** (port 8057)
  - Event analytics and reporting
  - Usage statistics
  - Performance metrics

- **hub_module/admin_portal** (port 8060)
  - Administrative dashboard
  - User management
  - Community configuration interface

### 4. Action/Interactive Modules (9)
Response modules that execute commands and user interactions:

- **ai_interaction_module** (port 8005)
  - Multi-provider AI responses
  - Ollama, OpenAI, MCP integration
  - Context-aware conversations

- **alias_interaction_module** (port 8006)
  - Custom command alias system
  - Linux-style command substitution
  - User-defined shortcuts

- **shoutout_interaction_module** (port 8007)
  - User shoutout generation
  - Platform-specific formatting
  - Twitch API integration

- **inventory_interaction_module** (port 8008)
  - Multi-threaded inventory management
  - Item tracking and trading
  - Persistence layer

- **calendar_interaction_module** (port 8009)
  - Event scheduling and management
  - Approval workflows
  - Recurring event support

- **memories_interaction_module** (port 8014)
  - Quote and memory storage
  - Reminder management
  - URL bookmark system

- **youtube_music_interaction_module** (port 8015)
  - YouTube Music integration
  - Playlist management
  - Browser source integration

- **spotify_interaction_module** (port 8016)
  - Spotify OAuth integration
  - Playback control
  - Playlist discovery

- **loyalty_interaction_module** (port 8017)
  - Loyalty points system
  - Reward tracking
  - Redemption processing

### 5. Premium Mobile Apps
- **Android** (Kotlin)
  - Native Android application
  - Push notifications
  - Offline functionality

- **iOS** (Swift)
  - Native iOS application
  - iCloud synchronization
  - Siri integration

---

## GitHub Actions Workflows

### Workflow Files Structure

```
.github/workflows/
├── ci-cd.yml                          # Main CI/CD pipeline
├── build-container.yml                # Reusable container build template
├── version-release.yml                # Version release workflow
├── build-router.yml                   # Router module build
├── build-{module-name}.yml            # Individual module builds (24 files)
├── containers.yml                     # Container orchestration
├── pr-validation.yml                  # Pull request and merge-queue tests
└── security.yml                       # Dependency and code security checks
```

### Main CI/CD Pipeline (`ci-cd.yml`)

**Triggers**:
- Push to `main` or `develop` branches
- Pull requests to `main` or `develop`
- Manual workflow dispatch
- `.version` file changes

**Security Checks** (runs first):
1. Trivy vulnerability scanner (filesystem)
2. CodeQL Analysis (Go, Java, JavaScript)
3. pip-audit for Python dependencies
4. npm audit for Node.js dependencies
5. Dependency Review (PRs only, fails on high severity)

**Language-Specific Security**:

- **Python**:
  - `pip-audit` for dependency vulnerabilities
  - `bandit` for code security issues
  - `flake8`, `black`, `isort`, `mypy` for code quality

- **JavaScript/Node.js**:
  - `npm audit` for all Node.js modules
  - ESLint and Prettier for linting
  - Dependency vulnerability scanning

- **Go** (if present):
  - `gosec` for security issues
  - `golangci-lint` for code quality

### Container Build Workflow (`build-container.yml` - Reusable Template)

**Purpose**: Shared reusable workflow for all module containers

**Inputs**:
- `module_name`: Container image name (e.g., "router")
- `module_path`: Path to module (e.g., "processing/router_module")
- `port`: Service port number

**Version Determination** (five-tier tag scheme, computed once in `determine-version`; `.version` is not used for tags):
```
Tier   Trigger                                           Image tag
alpha  push/dispatch on any non-release branch           alpha-<epoch>
beta   push to release/v*.X (or main)                    beta-<epoch>
gamma  GitHub release flagged pre-release (prereleased)  gamma-<epoch>
prod   GitHub release published (released)               v{Major}.{Minor}.{Patch} + latest
```
- `<epoch>` = unix seconds at `determine-version` time (shared by all matrix jobs).
- Prod requires the release tag to match `v{Major}.{Minor}.{Patch}` or the build fails.
- Gamma/prod run on the `release:` event (not a `tags: v*` push); every image workflow
  carries `release: types: [released, prereleased]` and force-builds on it (path filters do not apply).
- The same mapping is documented in the `determine-version` step of every image workflow.

**Build Features**:
- Multi-platform: Linux AMD64 + ARM64
- Docker Buildx for performance
- Per-module cache scopes
- Build arguments (MODULE_NAME, MODULE_PORT)
- GitHub Container Registry (ghcr.io)

### Module Build Workflows

Each of 24+ modules has its own workflow file:

**Pattern**: `.github/workflows/build-{module-name}.yml`

**Examples**:
```yaml
name: Build Router Module

on:
  push:
    branches: [main, develop, 'feature/**', 'bugfix/**']
    paths:
      - 'processing/router_module/**'
      - 'libs/flask_core/**'
      - '.version'
      - '.github/workflows/build-router.yml'
      - '.github/workflows/build-container.yml'
  pull_request:
    branches: [main, develop, 'v*']
  workflow_dispatch:

jobs:
  build:
    uses: ./.github/workflows/build-container.yml
    with:
      module_name: 'router'
      module_path: 'processing/router_module'
      port: 8000
    secrets: inherit
```

### Version Release Workflow (`version-release.yml`)

**Triggers**: Changes to `.version` file on main branch

**Actions**:
1. Detects `.version` change using git diff
2. Creates GitHub Release/Pre-release with version tag
3. Generates release notes (placeholder for conventional commits)
4. Notifies all module workflows to rebuild with release tags

**Output**:
- GitHub Release: `v0.2.0` (or current version)
- Release artifacts and changelog

### Mobile / Desktop Clients

Mobile and desktop client code (and its CI) is not built from this repository.
**pr-validation.yml** provides the pull-request and merge-queue test gate.

---

## Security Scanning Integration

### Multi-Language Security Scanning

#### Python Security
```bash
# Run bandit for security issues
bandit -r core/ action/ processing/ admin/

# Run pip-audit for dependency vulnerabilities
pip-audit --fix

# Type checking with mypy
mypy --strict core/ action/
```

#### JavaScript Security
```bash
# NPM audit
npm audit --production --audit-level=moderate

# Fix vulnerabilities
npm audit fix

# ESLint security plugin
npm run lint
```

#### Go Security (if applicable)
```bash
# gosec for security issues
gosec ./...

# golangci-lint
golangci-lint run
```

#### Kotlin Security
```bash
# Kotlin linting via ktlint
ktlint src/

# Detekt for code quality
detekt -i src/
```

### Integrated Scanning in CI/CD

**Trivy Filesystem Scan**:
- Scans all source files for vulnerabilities
- Generates SARIF format for GitHub Security tab
- Fails on high/critical vulnerabilities (configurable)

**CodeQL Analysis**:
- Supports: Go, Java, JavaScript
- Detects code vulnerabilities
- Creates security alerts

**Dependency Review** (PRs only):
- Blocks PRs with high-severity vulnerabilities
- Provides remediation suggestions

---

## Version Management System

### Semantic Versioning Format

```
vMajor.Minor.Patch.Epoch64
├─ Major: Breaking changes, API changes
├─ Minor: Significant new features
├─ Patch: Bug fixes, security patches
└─ Epoch64: Unix timestamp at build time (added to container labels)
```

### Current Version: 0.2.0

**Version History**:
- 0.1.0: Initial microservices migration
- 0.2.0: Core modules implementation (current)
- Future: 1.0.0 (production release)

### Version Update Process

**Manual Update**:
```bash
# Navigate to repository root
cd /home/penguin/code/Waddles

# Update version file
./scripts/version/update-version.sh patch

# Verify update
cat .version

# Commit and push
git add .version
git commit -m "Release v0.2.1"
git push origin main
```

**Automated Actions**:
1. Version file change detected by CI/CD
2. Version-release workflow triggers
3. GitHub Release created
4. All 24+ module workflows rebuild with release tags
5. Containers tagged: `v0.2.1` AND `latest`

---

## Container Registry & Deployment

### GitHub Container Registry (ghcr.io)

**Registry URL**: `ghcr.io/{owner}/waddlebot/{module}`

**Example Images**:
```
ghcr.io/penguintechinc/waddlebot/router:v0.2.0
ghcr.io/penguintechinc/waddlebot/router:latest
ghcr.io/penguintechinc/waddlebot/ai-interaction:v0.2.0
ghcr.io/penguintechinc/waddlebot/discord:v0.2.0
... (24+ modules)
```

### Image Tags by Build Type

**Feature Branch Push** (e.g., feature/new-command):
```
ghcr.io/penguintechinc/waddlebot/router:v0.2.0-alpha
```

**Main Branch Push** (regular commit):
```
ghcr.io/penguintechinc/waddlebot/router:v0.2.0-beta
```

**Version Release** (.version change on main):
```
ghcr.io/penguintechinc/waddlebot/router:v0.2.0
ghcr.io/penguintechinc/waddlebot/router:latest
```

### Multi-Platform Builds

All container builds target:
- `linux/amd64` (Intel/AMD x86_64)
- `linux/arm64` (ARM-based systems, Apple Silicon)

---

## Development Workflow

### Local Development

```bash
# Clone and setup
git clone https://github.com/penguintechinc/Waddles.git
cd Waddles

# Install dependencies
pip install -r requirements.txt
npm install

# Create feature branch
git checkout -b feature/new-feature

# Make changes, test locally
# See STANDARDS.md for testing patterns

# Commit changes
git add .
git commit -m "feat: add new feature"
git push origin feature/new-feature

# Create Pull Request
# CI/CD automatically runs security checks and builds
```

### Pull Request Process

1. **Branch Protection Rules**:
   - Requires PR review (configurable)
   - All CI checks must pass
   - No direct pushes to main

2. **CI/CD Checks** (automated):
   - Security scanning (Trivy, CodeQL, pip-audit, npm audit)
   - Container builds for modified modules
   - Dependency review for vulnerabilities

3. **Merge Requirements**:
   - All checks passing
   - Code review approved
   - Branch up to date with main

### Version Release Process

```bash
# 1. Ensure all commits merged to main
git checkout main
git pull origin main

# 2. Update version
./scripts/version/update-version.sh minor

# 3. Verify change
git diff .version

# 4. Commit version bump
git add .version
git commit -m "Release v0.3.0"
git push origin main

# 5. Watch GitHub Actions
# - version-release.yml creates GitHub Release
# - All 24+ module workflows rebuild with v0.3.0
# - Containers tagged as latest
```

---

## Monitoring and Troubleshooting

### Build Status Monitoring

**GitHub Actions Dashboard**:
1. Navigate to: https://github.com/{owner}/Waddles/actions
2. View workflow runs by name
3. Click workflow to see job logs
4. Check step output for build details

### Common Issues

**Build Fails**:
1. Check GitHub Actions logs
2. Verify Dockerfile exists in module path
3. Ensure `libs/flask_core` dependencies resolve
4. Check Docker build context and COPY commands

**Wrong Container Tags**:
1. Verify `.version` file content: `cat .version`
2. Check git branch: `git rev-parse --abbrev-ref HEAD`
3. Confirm `.version` file was committed: `git log -1 --oneline .version`

**Missing Latest Tag**:
- `latest` tag only applied when `.version` changes on main
- Regular main commits get `-beta` suffix
- Non-main branches get `-alpha` suffix

**Security Scan Failures**:
1. Check Trivy output in GitHub Actions
2. Run locally: `trivy fs .`
3. Fix vulnerabilities: `npm audit fix`, `pip-audit --fix`
4. Re-run workflow

### Debugging

**View Workflow Definition**:
```bash
# Check what triggers a specific workflow
cat .github/workflows/build-router.yml

# View reusable template
cat .github/workflows/build-container.yml
```

**Test Version Detection**:
```bash
# Check current version
cat .version

# Simulate version update
echo "0.3.0" > .version
git add .version
git diff --cached

# Revert
git checkout .version
```

---

## Compliance Checklist

- [x] `.version` file monitoring implemented
- [x] Epoch64 build timestamp in container labels
- [x] Multi-language security scanning (Python, JS, Go, Kotlin)
- [x] 24+ module container builds automated
- [x] Version release workflow with GitHub Releases
- [x] Multi-platform builds (amd64, arm64)
- [x] GitHub Container Registry integration
- [x] Pull request security checks
- [x] Dependency vulnerability scanning
- [x] Build caching for performance
- [x] Documentation with WORKFLOWS.md and STANDARDS.md

---

## Future Enhancements

- [ ] Integration tests after container builds
- [ ] Automatic staging deployments for beta tags
- [ ] Production deployments for release tags
- [ ] Container image signing with cosign
- [ ] SBOM (Software Bill of Materials) generation
- [ ] Release notes from conventional commits
- [ ] Automated hotfix releases
- [ ] Canary deployment strategy
- [ ] Performance regression testing
- [ ] Compliance scanning (GDPR, HIPAA, etc.)

---

## Related Documentation

- **STANDARDS.md**: Microservices architecture patterns and best practices
- **CLAUDE.md**: Project context and development guidelines
- **docs/api-reference.md**: Complete API endpoint documentation
- **docs/module-details-core.md**: Core module implementation details
- **docs/module-details-action.md**: Action module implementation details
- **docs/environment-variables.md**: Configuration reference for all modules

---

**Last Updated**: 2025-12-11
**Waddles Version**: 0.2.0
**Total Modules**: 24+
**Workflow Files**: 35+
**Container Platforms**: 2 (amd64, arm64)
**Security Scanners**: 4+ (Trivy, CodeQL, pip-audit, npm audit, gosec, bandit)
