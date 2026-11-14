# Waddles Security Policy

## Overview

Waddles is a critical infrastructure component for community management and chat automation. This document outlines our comprehensive security policy, vulnerability management process, and security update procedures to ensure the integrity, confidentiality, and availability of the platform and its users' data.

## 1. Vulnerability Disclosure & Reporting

### 1.1 Reporting Vulnerabilities

We take security seriously and appreciate the security research community's efforts to help improve Waddles. If you discover a security vulnerability, please report it responsibly:

**Reporting Channels:**
- **Email**: security@waddlebot.io (preferred)
- **GitHub Security Advisory**: Use GitHub's security advisory feature for private disclosure
- **Discord Security Channel**: Available for verified security researchers (contact security team)

**What to Include:**
- Detailed description of the vulnerability
- Steps to reproduce the issue
- Affected components and versions
- Potential impact assessment (data exposure, unauthorized access, DoS, etc.)
- Your contact information for follow-up

### 1.2 Vulnerability Handling Process

1. **Acknowledgment** (within 24 hours)
   - We will acknowledge receipt of your report within one business day
   - You will receive a tracking ID for reference

2. **Investigation** (1-7 days)
   - Security team conducts thorough analysis
   - We determine severity using CVSS v3.1 scoring
   - Impact assessment on deployed systems

3. **Remediation** (varies by severity)
   - **Critical (CVSS 9.0-10.0)**: Fix within 48 hours
   - **High (CVSS 7.0-8.9)**: Fix within 1 week
   - **Medium (CVSS 4.0-6.9)**: Fix within 2 weeks
   - **Low (CVSS 0.1-3.9)**: Fix within 1 month

4. **Verification** (before public disclosure)
   - Patches are tested thoroughly
   - Coordinated release scheduled
   - Credits offered to reporter (if desired)

5. **Disclosure**
   - Security advisory published
   - Users notified through security mailing list
   - Credit to researcher (with permission)

### 1.3 Coordinated Disclosure Timeline

- **Day 0**: Vulnerability reported
- **Day 1**: Acknowledgment and initial assessment
- **Day 7-30**: Fix developed and tested (depending on severity)
- **Day 30-90**: Public disclosure and patch availability
- **Embargo Agreement**: Reporters agree not to disclose before patch release

**Note**: We follow responsible disclosure principles aligned with industry standards and will work with reporters on reasonable timelines.

## 2. Security Update Schedule

### 2.1 Quarterly Security Reviews

Waddles conducts comprehensive security reviews on a quarterly basis:

**Q1 Review (January-March)**
- Dependency audit and updates
- Code security analysis (bandit, static analysis)
- Penetration testing of critical components
- Access control review

**Q2 Review (April-June)**
- Infrastructure security assessment
- Database security hardening
- API security review
- Third-party integration audit

**Q3 Review (July-September)**
- Cryptographic standards review
- Authentication mechanism assessment
- Authorization policy review
- Incident response plan updates

**Q4 Review (October-December)**
- Full security audit
- Compliance verification (if applicable)
- Year-end threat assessment
- Planning for next year's security initiatives

### 2.2 Emergency Security Updates

Critical vulnerabilities (CVSS >= 9.0) trigger emergency procedures:

1. **Hotfix Release**: Released immediately with limited changes
2. **Security Advisory**: Published within 24 hours
3. **Patch Verification**: Deployed to all critical systems
4. **Post-Incident Review**: Conducted within 5 days

### 2.3 Regular Update Cadence

- **Security Patches**: Released as needed (within severity timelines)
- **Monthly Dependency Updates**: All non-critical package updates
- **Quarterly Major Reviews**: Comprehensive assessment and planning
- **Annual Security Audit**: Full third-party security assessment

## 3. Dependency Management Process

### 3.1 Dependency Classification

Waddles uses a mixed dependency strategy for optimal security and stability:

#### Security-Critical Packages (Pinned to Specific Versions)
These packages are pinned to specific secure versions due to their critical security role:

| Package | Purpose | Current Version | Update Policy |
|---------|---------|-----------------|----------------|
| `cryptography` | Encryption/TLS | 41.0.x | Patch + minor releases only |
| `PyJWT` | JWT token handling | 2.8.0+ | Patch releases immediately |
| `bcrypt` | Password hashing | 4.1.2+ | Patch releases immediately |
| `python-jose` | JWT/JWS handling | Latest secure | Patch releases immediately |
| `authlib` | OAuth/OpenID Connect | 1.3.0+ | Quarterly review |

#### Standard Library Packages (Flexible Versioning)
These packages are version-flexible for better compatibility and feature updates:

| Package | Purpose | Version Range |
|---------|---------|----------------|
| `flask` / `quart` | Web framework | >= 0.19.0 |
| `httpx` | Async HTTP client | >= 0.27.0, < 0.28.0 |
| `redis` | Cache/session store | >= 5.0.1 |
| `pydal` | Database abstraction | >= 20240906.1, < 20250101 |
| `pydantic` | Data validation | >= 2.5.0 |
| `pytest` | Testing framework | >= 7.4.0 |

### 3.2 Update Schedule

**Monthly Cycle:**
- First Monday of each month: Dependency check
- Minor/patch updates for non-critical packages
- Security updates applied immediately
- Changes tested before merge

**Quarterly Cycle:**
- Full security audit of all dependencies
- Major version updates evaluated
- Transitive dependency review
- Vulnerability scanning (Dependabot, pip-audit)

**Annual Cycle:**
- Complete dependency audit
- Removal of unused packages
- Technology stack review
- Planning for version upgrades

### 3.3 Testing Requirements Before Updates

All dependency updates must pass:

1. **Unit Tests**: 100% passing
2. **Integration Tests**: All modules functional
3. **Security Scanning**:
   ```bash
   pip-audit --skip-editable
   bandit -r . -ll
   safety check
   ```
4. **Performance Tests**: No regressions
5. **Manual Testing**: Critical functionality verified

### 3.4 Vulnerability Scanning Tools

Integrated into CI/CD pipeline:

- **Dependabot**: GitHub's built-in dependency vulnerability alerts
- **pip-audit**: Direct dependency vulnerability checking
- **Safety**: Known security issue database
- **Bandit**: Python security linting
- **OWASP Dependency-Check**: Transitive dependency scanning

## 4. Credential Rotation Procedures

### 4.1 Credential Types and Rotation Intervals

| Credential Type | Storage | Rotation Interval | Procedure |
|-----------------|---------|-------------------|-----------|
| Database Passwords | Kubernetes Secrets | Every 90 days | Update secret, rolling deployment |
| API Keys (Third-party) | Environment variables | Every 180 days | Generate new, update config, revoke old |
| OAuth Tokens | Secure cache (Redis encrypted) | 1 hour (auto-refresh) | Automatic via OAuth2 refresh flow |
| JWT Signing Keys | Kubernetes Secrets | Every 365 days | Key rotation with grace period |
| SSH Keys | Secured access control | Every 180 days | Generate new, update authorized_keys |
| TLS Certificates | Certificate manager | Before expiry | Auto-renewal (Let's Encrypt) |

### 4.2 Database Credential Rotation

**Procedure:**
1. Generate new database credentials in PostgreSQL
2. Update Kubernetes secret with new password
3. Test new credentials in staging environment
4. Perform rolling deployment (30 seconds downtime)
5. Verify all pods connected successfully
6. Revoke old credentials after 24-hour grace period

**Example:**
```bash
# Generate new password
NEW_PASS=$(openssl rand -base64 32)

# Update secret
kubectl patch secret postgres-credentials \
  -p '{"data":{"password":"'$(echo -n $NEW_PASS | base64)'"}}'

# Trigger rolling update
kubectl rollout restart deployment/router-service
```

### 4.3 API Key Rotation

**For Third-Party APIs (Twitch, Discord, Slack, etc.):**

1. **Preparation Phase**
   - Schedule rotation window (typically off-peak hours)
   - Notify platform maintainers
   - Prepare new credentials

2. **Implementation Phase**
   - Generate new API keys in provider dashboard
   - Update environment variables in Kubernetes
   - Deploy changes in canary fashion (25% → 50% → 100%)
   - Monitor error rates and logs

3. **Verification Phase**
   - Verify OAuth flows working
   - Test webhook deliveries
   - Check API rate limits
   - Monitor error metrics for 1 hour

4. **Cleanup Phase**
   - Revoke old API keys (24-hour delay for safety)
   - Update documentation
   - Log rotation completion

### 4.4 JWT Key Rotation

**Strategy:** Rolling key rotation with grace period

```yaml
# Old key (grace period: 7 days)
jwt_keys:
  old:
    key_id: "v2"
    algorithm: "HS256"
    revoked_at: "2025-12-02T00:00:00Z"

  # Current key
  current:
    key_id: "v3"
    algorithm: "HS256"
    active_since: "2025-12-09T00:00:00Z"
```

**Rotation Process:**
1. New key generated and marked as "current"
2. Old key marked as "grace period" (7 days)
3. Both keys validate incoming JWTs
4. Only current key signs new tokens
5. After 7 days, old key removed

### 4.5 Emergency Credential Revocation

**Immediate Actions for Compromised Credentials:**

1. **Assessment** (< 5 minutes)
   - Determine scope of compromise
   - Identify affected systems
   - Check audit logs

2. **Immediate Mitigation** (< 15 minutes)
   - Revoke compromised credentials immediately
   - Deploy temporary credentials
   - Block suspicious access patterns
   - Initiate incident response

3. **Communication** (< 30 minutes)
   - Notify security team
   - Inform affected users (if data exposure)
   - Document incident
   - Initiate post-mortem

4. **Recovery** (1-24 hours)
   - Deploy new credentials
   - Verify system functionality
   - Audit logs for unauthorized activity
   - Implement preventive measures

## 5. Contact Information

### 5.1 Security Team

**Primary Contact**: security@waddlebot.io

**Key Personnel:**
- **Security Lead**: security-lead@waddlebot.io
- **On-Call Security**: Rotated daily (check status page)
- **Incident Commander**: incident@waddlebot.io

### 5.2 Reporting Channels

| Issue Type | Channel | Response SLA |
|------------|---------|--------------|
| Critical Vulnerability | security@waddlebot.io | 1 hour |
| High Severity | GitHub Security Advisory | 4 hours |
| Medium Severity | Security form on website | 1 business day |
| Bug Reports | GitHub Issues (public) | 2 business days |

### 5.3 Mailing Lists

- **Security Updates**: security-announce@waddlebot.io (subscribe for advisories)
- **Security Discussions**: security-discuss@waddlebot.io (for verified researchers)

## 6. Response Timeline Commitments

### 6.1 SLA by Severity

**Critical (CVSS 9.0-10.0)**
- Acknowledgment: < 1 hour
- Initial assessment: < 4 hours
- Fix development: < 48 hours
- Public disclosure: < 7 days

**High (CVSS 7.0-8.9)**
- Acknowledgment: < 4 hours
- Initial assessment: < 1 day
- Fix development: < 5 days
- Public disclosure: < 30 days

**Medium (CVSS 4.0-6.9)**
- Acknowledgment: < 1 day
- Initial assessment: < 2 days
- Fix development: < 14 days
- Public disclosure: < 60 days

**Low (CVSS 0.1-3.9)**
- Acknowledgment: < 2 days
- Initial assessment: < 5 days
- Fix development: < 30 days
- Public disclosure: Coordinated timing

### 6.2 Communication Commitments

- **Status Updates**: Every 3 days during remediation
- **Estimated Fixes**: Provided after initial assessment
- **Patch Notifications**: Sent 24 hours before release
- **Public Advisory**: Published within timeline (see above)

## 7. Supported Versions

Waddles maintains security support for:

- **Latest Release**: Full support (critical, high, medium, low vulnerabilities)
- **Previous Major Release**: Security fixes only (critical, high vulnerabilities)
- **Older Releases**: No support (encourage upgrades)

**Version Support Example:**
- Waddles v2.x: Full support
- Waddles v1.x: Security fixes only
- Waddles v0.x: No support

## 8. Security Best Practices for Deployment

### 8.1 Container Security

- Run containers as non-root user
- Use read-only root filesystem where possible
- Implement network policies
- Regular image scanning with Trivy
- Use specific base image versions (no `latest` tags)

### 8.2 Database Security

- Enable encryption at rest (PostgreSQL)
- Use TLS for all connections
- Implement least-privilege database roles
- Regular automated backups with encryption
- Database access logging

### 8.3 API Security

- Rate limiting on all endpoints
- Request size limits
- CORS policy enforcement
- Request validation and sanitization
- API key rotation procedures
- Webhook signature verification (HMAC-SHA256)

### 8.4 Secrets Management

- Never commit secrets to repository
- Use Kubernetes Secrets or Vault
- Implement RBAC for secret access
- Regular secret rotation
- Audit all secret access

## 9. Incident Response

### 9.1 Response Procedures

1. **Detection**: Security monitoring alerts
2. **Assessment**: Scope and impact determination
3. **Containment**: Isolate affected systems
4. **Remediation**: Deploy fixes
5. **Recovery**: Restore normal operations
6. **Post-Mortem**: Analyze and prevent

### 9.2 Communication During Incidents

- Internal: Real-time updates to #security-incident Slack channel
- External: Status page updates (if user-facing)
- Follow-up: Public advisory and post-mortem (if appropriate)

## 10. Compliance & Standards

Waddles follows these security standards:

- **OWASP Top 10**: Mitigations for all items
- **CWE Coverage**: Focus on CWE-89, CWE-79, CWE-200, CWE-306, CWE-798
- **Security.txt**: RFC 9110 compliance with security contact info
- **NIST Cybersecurity Framework**: Risk assessment and management

## 11. Annual Security Review

Every December, Waddles conducts a comprehensive security review:

- **Coverage**: All systems, dependencies, and processes
- **Assessment**: Vulnerability scans, penetration testing, code review
- **Planning**: Next year's security initiatives
- **Reporting**: Public transparency report (anonymized findings)

---

**Policy Version**: 1.0
**Last Updated**: December 2025
**Next Review**: March 2026
**Owner**: Security Team

For questions or concerns, contact: security@waddlebot.io
