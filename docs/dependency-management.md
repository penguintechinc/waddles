# Waddles Dependency Management Strategy

## Overview

Waddles uses a **mixed dependency strategy** that balances security, stability, and feature velocity. Security-critical packages are pinned to specific versions, while standard libraries use flexible version ranges to allow compatible updates.

This document outlines our dependency classification, update procedures, testing requirements, and tools for managing the complex dependency landscape across all Waddles modules.

## 1. Dependency Classification Strategy

### 1.1 Security-Critical Packages (Pinned Versioning)

These packages directly handle sensitive operations and are pinned to specific versions:

| Package | Purpose | Version Constraint | Why Pinned | Update Policy |
|---------|---------|-------------------|-----------|---------------|
| `cryptography` | TLS, encryption, hashing | `== 41.0.x` | Cryptographic correctness is critical | Patch + minor releases only, reviewed each release |
| `PyJWT` | JWT token signing/validation | `>= 2.8.0` | Prevents token forgery attacks | Immediate patch releases; minor reviewed quarterly |
| `bcrypt` | Password hashing | `>= 4.1.2` | Protects user credentials | Immediate patch releases; minor reviewed quarterly |
| `python-jose` | JWT/JWS/JWE handling | Latest within 2.8+ | Token security | Patch releases immediately; minor reviewed quarterly |
| `authlib` | OAuth2, OpenID Connect | `>= 1.3.0` | Third-party authentication | Quarterly review before updates |
| `psycopg2-binary` | PostgreSQL adapter | `>= 2.9.9` | Database security & integrity | Quarterly review for major releases |
| `bleach` | HTML sanitization (XSS prevention) | `>= 6.0.0` | Prevents DOM-based XSS | Patch releases immediately |

### 1.2 Standard Library Packages (Flexible Versioning)

These packages provide core functionality and use flexible version ranges:

| Package | Purpose | Version Constraint | Rationale |
|---------|---------|-------------------|-----------|
| `quart` | Async web framework | `>= 0.19.0` | Features stable; minor version compatibility |
| `hypercorn` | ASGI server for Quart | `>= 0.16.0` | ASGI spec compliance; compatible minors |
| `httpx` | Async HTTP client | `>= 0.27.0, < 0.28.0` | HTTP/2 support; next major version reviewed |
| `redis` | Redis client library | `>= 5.0.1` | Redis protocol stable; minor versions compatible |
| `pydal` | Database abstraction | `>= 20240906.1, < 20250101` | Version constraint window for stability |
| `pydantic` | Data validation | `>= 2.5.0` | v2.x stable API; minor versions compatible |
| `requests` | HTTP client (sync) | `>= 2.31.0` | Used by authlib; stable API |
| `python-dateutil` | Date/time utilities | `>= 2.8.2` | Stable API; minor versions backward compatible |
| `pytest` | Testing framework | `>= 7.4.0` | Test infrastructure; minor versions compatible |
| `black` | Code formatter | Latest | Development tool; no production impact |
| `flake8` | Linter | Latest | Development tool; no production impact |
| `mypy` | Type checker | Latest | Development tool; no production impact |

### 1.3 Development-Only Packages

Development and testing tools with no impact on production:

- `pytest`, `pytest-asyncio`, `pytest-cov`
- `black`, `flake8`, `mypy`
- `bandit` (security linter)
- `safety` (vulnerability scanner)

These can use flexible versioning (latest compatible).

## 2. Update Schedule & Process

### 2.1 Monthly Update Cycle

**First Monday of each month:**

1. **Dependency Check** (30 minutes)
   ```bash
   pip list --outdated
   pip-audit --skip-editable
   safety check
   ```

2. **Filter & Plan** (1 hour)
   - Identify security updates (apply immediately)
   - Identify minor updates for standard libraries
   - Flag major versions for quarterly review
   - Plan update PR with targeted changes

3. **Implementation** (1-2 hours)
   - Update `requirements.txt` in affected modules
   - Run local tests
   - Create feature branch
   - Submit PR with description

4. **Review & Merge** (varies)
   - Code review by security team
   - CI/CD pipeline validation
   - Manual testing in staging
   - Merge to `main` branch

### 2.2 Quarterly Security Review

**First week of each quarter (Jan, Apr, Jul, Oct):**

1. **Comprehensive Audit** (4-6 hours)
   - Full dependency tree analysis
   - Transitive dependency vulnerability scan
   - OWASP dependency check
   - Technology stack assessment

2. **Security Assessment**
   - Review CVE databases (NVD, GitHub Security Advisories)
   - Check for EOL dependencies
   - Evaluate replacement libraries if needed
   - Document findings

3. **Planning**
   - Schedule major version upgrades if needed
   - Plan migration paths for deprecated packages
   - Update this document if strategy changes
   - Create issues for planned work

4. **Report**
   - Publish quarterly security summary
   - Notify stakeholders of planned updates
   - Document any blocking issues

### 2.3 Emergency Update Process

**For Critical Vulnerabilities (CVSS >= 9.0):**

1. **Immediate Action** (within 2 hours)
   - Verify vulnerability affects Waddles
   - Begin patch development or pinned version update
   - Notify team in #security-incident Slack channel

2. **Testing** (4-8 hours)
   - Rapid testing in staging environment
   - Smoke tests on critical paths
   - Performance regression check

3. **Deployment** (within 24 hours)
   - Fast-track PR review
   - Deploy to production
   - Monitor for issues
   - Publish security advisory

## 3. Dependency Update Procedure

### 3.1 Creating Update PRs

**Template for dependency update commits:**

```bash
# Update security-critical package
git checkout -b deps/security-critical-update-DATE

# Update one or more security-critical packages
# Example: PyJWT security fix
# File: libs/flask_core/requirements.txt
# Change: PyJWT>=2.8.0 to PyJWT>=2.8.1

# Run full test suite
pytest --cov --cov-report=term-missing

# Commit with descriptive message
git commit -m "Update PyJWT to 2.8.1 (security fix)

- Addresses CVE-XXXX-XXXX
- See: https://github.com/jpadilla/pyjwt/security/advisories/..."
```

### 3.2 Testing Checklist

Every dependency update must pass:

- [ ] Unit tests (100% pass)
- [ ] Integration tests (all modules functional)
- [ ] Security scanning
- [ ] Performance tests (no regressions)
- [ ] Manual testing (critical features)

### 3.3 Module-Specific Updates

Each module has its own `requirements.txt`:

```
libs/flask_core/requirements.txt          # Core shared library
processing/router_module/requirements.txt # Router module
action/interactive/*/requirements.txt     # Interactive action modules
trigger/receiver/*/requirements.txt       # Trigger modules
core/*/requirements.txt                   # Core modules
```

**Update Strategy:**
1. Update base library (`flask_core`) first
2. Propagate to all dependent modules
3. Test each module independently
4. Deploy in order of dependency chain

## 4. Security-Critical Packages Deep Dive

### 4.1 Cryptography Package

**Purpose**: TLS, encryption, hashing operations

**Current Constraint**: `cryptography==41.0.x`

**Security Considerations**:
- Handles encryption algorithms (AES, RSA, etc.)
- Provides secure random number generation
- Implements TLS/SSL operations
- Any flaw here is catastrophic

**Update Policy**:
- Patch versions: Update immediately (41.0.1 → 41.0.2)
- Minor versions: Review quarterly (41.0.x → 41.1.0)
- Major versions: Scheduled migration (41.x → 42.0)

**Verification After Update**:
```bash
python -c "from cryptography.hazmat.primitives import ciphers; print('OK')"
# Test TLS functionality in integration tests
pytest tests/test_tls_connections.py -v
```

### 4.2 JWT Packages (PyJWT, python-jose)

**Purpose**: Secure token creation and validation

**Security Concerns**:
- Algorithm confusion attacks
- Token forgery
- Signature bypass
- Key confusion

**Current Constraints**:
- `PyJWT>=2.8.0`
- `python-jose>=2.8.0`

**Update Policy**:
- Patch releases: Immediate (2.8.0 → 2.8.1)
- Minor releases: Quarterly review
- Major releases: Full testing required

**Verification**:
```bash
# Test token creation/validation
python -c "
import jwt
token = jwt.encode({'data': 'test'}, 'secret', algorithm='HS256')
decoded = jwt.decode(token, 'secret', algorithms=['HS256'])
assert decoded['data'] == 'test'
print('Token verification OK')
"
```

### 4.3 bcrypt (Password Hashing)

**Purpose**: Secure password hashing

**Security Role**:
- Protects user passwords
- Uses adaptive cost factor
- Defends against rainbow tables
- Critical for authentication

**Current Constraint**: `bcrypt>=4.1.2`

**Update Policy**:
- Patch releases: Immediate
- Minor releases: Quarterly
- Major releases: Full testing

**Verification**:
```bash
python -c "
import bcrypt
password = b'test_password'
hashed = bcrypt.hashpw(password, bcrypt.gensalt())
assert bcrypt.checkpw(password, hashed)
print('Bcrypt verification OK')
"
```

### 4.4 Authlib (OAuth2/OpenID Connect)

**Purpose**: Third-party authentication and authorization

**Security Considerations**:
- OAuth2 flow implementation
- Token exchange security
- PKCE validation
- State parameter handling

**Current Constraint**: `authlib>=1.3.0`

**Update Policy**:
- Patch releases: Immediate
- Minor releases: Quarterly review
- Major releases: Scheduled migration

**Verification**:
```bash
# Test OAuth flows in integration tests
pytest tests/test_oauth_flows.py -v
pytest tests/test_twitch_oauth.py -v
pytest tests/test_discord_oauth.py -v
```

## 5. Testing Requirements

### 5.1 Pre-Update Testing

Before committing any dependency update:

```bash
# 1. Run full test suite
pytest --cov --cov-report=term-missing

# 2. Security scanning
pip-audit --skip-editable
bandit -r . -ll
safety check

# 3. Type checking
mypy . --strict

# 4. Linting
flake8 .
black --check .

# 5. Performance baseline
pytest tests/test_performance.py -v
```

### 5.2 CI/CD Pipeline

GitHub Actions automatically runs on all PRs:

- Unit tests: `pytest tests/`
- Integration tests: `pytest tests/integration/`
- Security scanning: `pip-audit` + `bandit`
- Code quality: `black`, `flake8`, `mypy`
- Performance: `pytest --benchmark`

### 5.3 Staging Environment Testing

Before production deployment:

1. Deploy to staging with updated dependencies
2. Run 24-hour smoke tests
3. Load test critical paths
4. Monitor error rates and performance
5. Verify backward compatibility

## 6. Tools & Automation

### 6.1 Dependabot Configuration

GitHub Dependabot automatically creates PRs for:

- **Security updates**: Daily, auto-merge if tests pass
- **Minor updates**: Weekly, requires review
- **Major updates**: Monthly, requires review

**.github/dependabot.yml**:
```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "daily"
    security-updates-only: true
    allow:
      - dependency-type: "direct"
    reviewers:
      - "security-team"

  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
    allow:
      - dependency-type: "direct"
    reviewers:
      - "security-team"
```

### 6.2 Local Tools

**pip-audit** (Local vulnerability scanning):
```bash
# Check all dependencies for known vulnerabilities
pip-audit --skip-editable

# Output JSON for CI/CD
pip-audit --skip-editable --format json > audit.json
```

**Safety** (Known vulnerability database):
```bash
# Check requirements.txt against safety database
safety check --json
```

**Bandit** (Security linting):
```bash
# Scan Python code for security issues
bandit -r . -ll  # -ll = only show HIGH/MEDIUM severity

# Exclude test files
bandit -r . -ll --exclude ./tests
```

### 6.3 Update Automation Scripts

**Monthly check script** (`scripts/check-dependencies.sh`):
```bash
#!/bin/bash
set -e

echo "Checking for outdated packages..."
pip list --outdated

echo -e "\nRunning security scans..."
pip-audit --skip-editable
safety check
bandit -r . -ll

echo -e "\nRunning tests..."
pytest --cov

echo -e "\nAll checks passed!"
```

## 7. Handling Dependency Conflicts

### 7.1 Transitive Dependency Issues

Sometimes indirect dependencies conflict:

```
Package A requires: Library >= 1.0
Package B requires: Library < 1.0
→ Conflict: Cannot upgrade both
```

**Resolution Strategy**:
1. Identify the conflicting requirement
2. Check if either package has newer version that resolves conflict
3. If not, pin one package to compatible version
4. Document conflict in `requirements.txt` comment
5. Schedule major version upgrade work

**Example**:
```
# Conflict: httpx requires chardet, requests prefers urllib3
# Solution: httpx >= 0.27.0 resolves this
httpx>=0.27.0

# TODO: Evaluate httpx 0.28.0 in Q2 2026
```

### 7.2 Abandoned or Slow-Moving Packages

If a security-critical package is no longer maintained:

1. File issue to track replacement
2. Evaluate alternative libraries
3. Plan migration in 90-day window
4. Test alternative thoroughly
5. Execute migration with full test suite

**Current Status**: All security-critical packages are actively maintained.

## 8. Version Pinning Strategy by Module

### 8.1 Flask Core Library (`libs/flask_core/requirements.txt`)

This is the foundation - most pinned for stability:

```
# Security-critical: strict pinning
cryptography==41.0.7
PyJWT>=2.8.1
bcrypt>=4.1.2

# Framework: moderate pinning
quart>=0.19.0
httpx>=0.27.0,<0.28.0

# Development: flexible
pytest>=7.4.0
black>=23.12.0
```

### 8.2 Action Modules

Medium pinning - inherit core dependencies:

```
# From pip install -r ../libs/flask_core/requirements.txt
-r ../../libs/flask_core/requirements.txt

# Module-specific: flexible
module-specific-library>=1.0.0
```

### 8.3 Trigger Modules

Platform-specific dependencies plus core:

```
# Core dependencies
-r ../../libs/flask_core/requirements.txt

# Twitch-specific
twitchio>=2.8.0

# Development
pytest>=7.4.0
```

## 9. Dependency Removal Process

Periodically audit and remove unused dependencies:

**Quarterly Cleanup**:
1. Generate dependency tree: `pipdeptree`
2. Check imports in codebase: `grep -r "import <package>"`
3. If not found, mark for removal
4. Verify removal doesn't break anything
5. Update `requirements.txt`
6. Commit with explanation

**Example**:
```bash
# Removed: old-package (no longer used)
# See: https://github.com/waddlebot/waddlebot/commit/xxxxx
# Usage grep: No imports found in codebase
# Migration: Functionality replaced by newer-package
```

## 10. Vulnerability Response Procedure

When a vulnerability is discovered in a dependency:

1. **Assessment** (< 1 hour)
   - Check if Waddles is affected
   - Determine severity (use CVSS)
   - Check if newer version available

2. **Planning** (1-2 hours)
   - If patch available: Fast-track update
   - If no patch: Plan workaround or pin to vulnerable version (documented)
   - Create issue: `[SECURITY] CVE-XXXX-XXXX: Package Name`

3. **Implementation** (2-24 hours, depends on severity)
   - Update to patched version
   - Run full test suite
   - Deploy to staging
   - Deploy to production
   - Publish security advisory

4. **Follow-up**
   - Monitor for issues
   - Update documentation
   - Include in quarterly review

## 11. Documentation & Compliance

### 11.1 Dependency License Compliance

All dependencies must have permissive licenses:

**Acceptable**:
- MIT
- Apache 2.0
- BSD
- ISC
- MPL 2.0

**Review Required**:
- GPL (GPL 2/3)
- Proprietary

**Not Allowed**:
- AGPL (must have explicit approval)

Check licenses during quarterly review.

### 11.2 Documentation Requirements

- Update `requirements.txt` with meaningful comments
- Document any version pins with reasons
- Keep this document current
- Document conflicts and workarounds
- Log all dependency changes in commit messages

### 11.3 Security & Compliance Standards

This process aligns with:
- **NIST SP 800-53** (CM-3, CM-8: Configuration Management)
- **CIS Controls v8** (2.1: Software Inventory)
- **OWASP A06:2021** (Vulnerable and Outdated Components)

## 12. Quick Reference

### Update Commands

```bash
# Check for vulnerabilities
pip-audit --skip-editable

# Update requirements.txt with latest safe versions
pip-compile requirements.txt

# Run all tests
pytest --cov

# Security checks
bandit -r . -ll
safety check

# Code quality
black .
flake8 .
mypy . --strict
```

### SLA for Different Severities

| Type | SLA | Example |
|------|-----|---------|
| Security vulnerability (critical) | 48 hours | CVE affecting cryptography |
| Security update (high) | 1 week | New minor version of PyJWT |
| Regular update (minor) | Monthly | New version of requests |
| Development tool | Quarterly | New version of pytest |

---

**Document Version**: 1.0
**Last Updated**: December 2025
**Next Review**: March 2026
**Owner**: Security Team

For questions about dependency management, contact: security@waddlebot.io
