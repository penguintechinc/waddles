# Bundle Network Permissions

## Overview

App bundles declare their outbound network requirements through the `net.http.*` egress list in their manifest. Network access is controlled through one of three mechanisms, each with distinct risk levels and approval requirements.

## Network Access Types

### Public FQDNs (Preferred)

**Type:** `net.http.fqdn:<host>`

**Risk Level:** LOW

**Requirements:** None—public FQDNs are the standard and preferred form for bundle network access.

**Use case:** Bundles should target public, fully-qualified domain names whenever possible. This is the safest and most common pattern.

```yaml
egress:
  - net.http.fqdn:api.example.com
  - net.http.fqdn:cdn.third-party.io
```

### Public IP Addresses

**Type:** `net.http.public-ip`

**Risk Level:** HIGH ⚠️

**Requirements:** Explicit reviewer approval; admin sign-off recommended.

**Notes:** Public IP access carries elevated risk compared to FQDN-based policies, as IPs lack the domain-name indirection and accountability of DNS-resolved targets. Use public IPs only when FQDN resolution is unavailable or when direct IP targeting is operationally necessary.

```yaml
egress:
  - net.http.public-ip:203.0.113.42
```

### Private IP Addresses

**Type:** `net.http.private-ip`

**Risk Level:** HIGH — DENIED BY DEFAULT ⛔

**Requirements:**
- Instance-wide default: **DENIED** — bundles cannot access private IP space without explicit administrator opt-in
- Separate explicit approval required
- Only available for exceptional self-hosted / on-premises deployments
- Never available for the platform's own cluster networks
- Requires strong justification and business case review

**Reviewer Guidance:** Treat any `net.http.private-ip` request as a red flag. This access type is a major risk vector—access to cluster-internal services, internal IPs, or unroutable ranges bypasses the platform's network isolation model. Approve conservatively and only for verified edge cases with documented business necessity.

```yaml
egress:
  - net.http.private-ip:10.0.0.5
  - net.http.private-ip:192.168.1.100
```

## Best Practices

1. **Default to public FQDNs** — they are the safest and most common pattern
2. **Avoid public IPs** unless FQDN resolution is impossible or operationally unfeasible
3. **Never request private-IP access** unless your bundle runs in a self-hosted / on-premises environment with explicit business justification
4. **Document your rationale** in the bundle's manifest comment or PR description whenever requesting public IP or private IP access
5. **Reviewers:** Flag any private-IP request immediately — these are exceptions that require strong justification and admin approval

## See Also

- Bundle Manifest Reference — egress field specification
- Network Policies (Kubernetes) — cluster-level network isolation rules
