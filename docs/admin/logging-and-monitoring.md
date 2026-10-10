# Waddles Logging and Monitoring Documentation

## Overview

Waddles implements comprehensive Authentication, Authorization, and Auditing (AAA) logging across all container modules to ensure security, compliance, and operational visibility. This document outlines the logging standards, implementation requirements, and monitoring strategies.

> **Enterprise audit trail:** security-relevant events (authz denials, tenant/role changes, DSAR/erasure, logins, admin actions, license changes) are also written to a tamper-evident, hash-chained store with verification and export tooling -- see [Enterprise Audit Logging](../compliance/audit-logging.md).

## Logging Requirements

### Universal Implementation

**ALL Waddles container modules MUST implement comprehensive AAA logging.**

### Required Logging Outputs

1. **Console Logging**
   - All logs output to stdout/stderr for container orchestration
   - Compatible with Docker, Kubernetes, and other container platforms
   - Structured format for log aggregation tools

2. **File Logging**
   - Structured logs to `/var/log/waddlebotlog/` directory
   - Automatic rotation (10MB files, 5 backups)
   - Separate log files by category (auth, authz, audit, error, system)

3. **Syslog (Optional)**
   - Configurable syslog support for centralized logging
   - RFC 3164 compliant syslog messages
   - Configurable facility and severity levels

## Log Categories

### AUTH (Authentication)
Events related to user authentication and session management.

**Examples:**
- User login attempts (success/failure)
- Token generation and refresh
- Authentication method changes
- Session creation and expiration
- OAuth flows and callback processing

### AUTHZ (Authorization)
Events related to permission checks and access control.

**Examples:**
- Permission checks (granted/denied)
- Role assignments and modifications
- ACL rule evaluations
- Resource access attempts
- Administrative privilege usage

### AUDIT (User Actions)
Events related to user actions and system changes.

**Examples:**
- CRUD operations (create, read, update, delete)
- Configuration changes
- Module installations/removals
- Data exports and imports
- Administrative actions

### ERROR (Error Conditions)
Events related to system errors and exceptions.

**Examples:**
- Application exceptions
- Database connection failures
- External API errors
- Resource exhaustion
- Validation failures

### SYSTEM (System Events)
Events related to system operations and health.

**Examples:**
- Service startup and shutdown
- Health check results
- Performance metrics
- Resource utilization
- Maintenance operations

## Log Structure

### Standard Format

```
[timestamp] LEVEL module:version EVENT_TYPE community=X user=Y action=Z result=STATUS [additional_fields]
```

### Field Specifications

- **timestamp**: ISO 8601 format with timezone (e.g., `2024-01-15T14:30:00.123Z`)
- **LEVEL**: Log level (DEBUG, INFO, WARNING, ERROR)
- **module**: Module name (e.g., `inventory_interaction_module`)
- **version**: Module version (e.g., `1.0.0`)
- **EVENT_TYPE**: Log category (AUTH, AUTHZ, AUDIT, ERROR, SYSTEM)
- **community**: Community identifier (when applicable)
- **user**: User identifier (when applicable)
- **action**: Action being performed
- **result**: Operation result (SUCCESS, FAILURE, DENIED, ERROR)
- **additional_fields**: Context-specific fields

### Example Log Entries

```
[2024-01-15T14:30:00.123Z] INFO inventory_interaction_module:1.0.0 AUTH community=gaming_server user=user123 action=token_refresh result=SUCCESS ip=192.168.1.100 session=sess_abc123

[2024-01-15T14:30:05.456Z] INFO inventory_interaction_module:1.0.0 AUTHZ community=gaming_server user=user123 action=add_item resource=inventory result=GRANTED permissions=inventory_admin

[2024-01-15T14:30:10.789Z] INFO inventory_interaction_module:1.0.0 AUDIT community=gaming_server user=user123 action=add_item resource=inventory result=SUCCESS duration=150ms item_name=laptop description="Gaming laptop"

[2024-01-15T14:30:15.012Z] ERROR inventory_interaction_module:1.0.0 ERROR community=gaming_server user=user456 action=checkout_item error="Item not found" item_name=nonexistent
```

## Configuration

### Environment Variables

All modules must support these logging configuration variables:

```bash
# Log Level Configuration
LOG_LEVEL=INFO                    # DEBUG, INFO, WARNING, ERROR

# File Logging Configuration
LOG_DIR=/var/log/waddlebotlog    # Log directory path

# Syslog Configuration (Optional)
ENABLE_SYSLOG=false              # Enable syslog output
SYSLOG_HOST=localhost            # Syslog server host
SYSLOG_PORT=514                  # Syslog server port (514 for UDP, 6514 for TCP)
SYSLOG_FACILITY=LOCAL0           # Syslog facility (LOCAL0-LOCAL7)
SYSLOG_PROTOCOL=UDP              # UDP or TCP
```

### Log Rotation Configuration

```bash
# Rotation Settings
LOG_MAX_SIZE=10485760            # 10MB in bytes
LOG_BACKUP_COUNT=5               # Number of backup files
LOG_ROTATION_WHEN=midnight       # Daily rotation
```

## Implementation Guide

### Python Implementation

```python
import logging
import logging.handlers
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict

@dataclass
class LogEvent:
    """Structured log event for Waddles"""
    timestamp: str
    level: str
    module: str
    version: str
    event_type: str  # AUTH, AUTHZ, AUDIT, SYSTEM, ERROR
    community_id: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    action: Optional[str] = None
    resource: Optional[str] = None
    result: Optional[str] = None  # SUCCESS, FAILURE, DENIED
    details: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    duration_ms: Optional[int] = None
    error_message: Optional[str] = None

class WaddlesLogger:
    """Comprehensive logging system for Waddles modules"""
    
    def __init__(self, module_name: str, module_version: str):
        self.module_name = module_name
        self.module_version = module_version
        self.setup_logging()
    
    def setup_logging(self):
        """Setup logging configuration"""
        # Configure loggers, handlers, and formatters
        # Implementation details...
    
    def log_authentication(self, user_id: str, action: str, result: str, **kwargs):
        """Log authentication events"""
        # Implementation details...
    
    def log_authorization(self, community_id: str, user_id: str, action: str, 
                         resource: str, result: str, **kwargs):
        """Log authorization events"""
        # Implementation details...
    
    def log_audit(self, community_id: str, user_id: str, action: str, 
                  resource: str, result: str, **kwargs):
        """Log audit events"""
        # Implementation details...
```

### Decorator Patterns

```python
def audit_log(action: str, resource_type: str = "default"):
    """Decorator for automatic audit logging"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            start_time = datetime.now()
            try:
                result = func(*args, **kwargs)
                duration = (datetime.now() - start_time).total_seconds() * 1000
                # Log successful operation
                return result
            except Exception as e:
                duration = (datetime.now() - start_time).total_seconds() * 1000
                # Log failed operation
                raise
        return wrapper
    return decorator

def require_permission(required_permission: str):
    """Decorator for authorization checking with logging"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Check permissions and log result
            # Implementation details...
        return wrapper
    return decorator
```

## Log File Organization

### Directory Structure

```
/var/log/waddlebotlog/
├── {module_name}/
│   ├── {module_name}.log              # Main application log
│   ├── {module_name}_auth.log         # Authentication events
│   ├── {module_name}_authz.log        # Authorization events
│   ├── {module_name}_audit.log        # Audit trail
│   ├── {module_name}_error.log        # Error events
│   └── {module_name}_system.log       # System events
```

### File Naming Convention

- Main log: `{module_name}.log`
- Category logs: `{module_name}_{category}.log`
- Rotated files: `{module_name}.log.1`, `{module_name}.log.2`, etc.

## Monitoring and Alerting

### Log Aggregation

#### ELK Stack Integration

```yaml
# Filebeat configuration for Waddles logs
filebeat.inputs:
- type: log
  enabled: true
  paths:
    - /var/log/waddlebotlog/*/*.log
  fields:
    service: waddlebot
  fields_under_root: true
  multiline.pattern: '^\['
  multiline.negate: true
  multiline.match: after

processors:
- add_docker_metadata:
    host: "unix:///var/run/docker.sock"
```

#### Fluentd Configuration

```xml
<source>
  @type tail
  path /var/log/waddlebotlog/*/*.log
  pos_file /var/log/td-agent/waddlebot.log.pos
  tag waddlebot.*
  format json
  time_format %Y-%m-%dT%H:%M:%S.%L%z
</source>

<match waddlebot.**>
  @type elasticsearch
  host elasticsearch
  port 9200
  index_name waddlebot-logs
</match>
```

### Alerting Rules

#### Critical Events

1. **Authentication Failures**
   - More than 5 failed login attempts per user in 1 minute
   - Invalid token usage patterns
   - Suspicious authentication patterns

2. **Authorization Violations**
   - Repeated permission denied events
   - Privilege escalation attempts
   - Unusual access patterns

3. **System Errors**
   - Database connection failures
   - Service unavailability
   - Performance degradation

#### Alert Configuration (Prometheus/AlertManager)

```yaml
groups:
- name: waddlebot-security
  rules:
  - alert: HighAuthenticationFailures
    expr: rate(waddlebot_auth_failures_total[1m]) > 5
    for: 1m
    labels:
      severity: warning
    annotations:
      summary: High authentication failure rate
      description: "{{ $labels.module }} has {{ $value }} auth failures/sec"

  - alert: DatabaseConnectionFailure
    expr: waddlebot_db_connection_failures_total > 0
    for: 0m
    labels:
      severity: critical
    annotations:
      summary: Database connection failure
      description: "{{ $labels.module }} cannot connect to database"
```

## Compliance and Retention

### Data Retention Policies

1. **Security Logs (AUTH/AUTHZ)**
   - Retention: 2 years minimum
   - Immutable storage recommended
   - Regular backup and archival

2. **Audit Logs**
   - Retention: 7 years (compliance requirement)
   - Encrypted storage required
   - Access logging for log access

3. **System Logs**
   - Retention: 90 days
   - Rolling deletion acceptable
   - Performance optimization focus

4. **Error Logs**
   - Retention: 1 year
   - Compressed storage acceptable
   - Debugging and troubleshooting focus

### Compliance Standards

- **SOC 2 Type II**: Comprehensive audit trails
- **GDPR**: Personal data handling logs
- **HIPAA**: Healthcare data access logs (if applicable)
- **PCI DSS**: Payment card data access logs (if applicable)

## Performance Considerations

### Logging Overhead

1. **Asynchronous Logging**
   - Use background threads for log writing
   - Queue-based log processing
   - Non-blocking application logic

2. **Log Level Optimization**
   - Production: INFO level minimum
   - Development: DEBUG level acceptable
   - Dynamic log level adjustment

3. **Structured Logging**
   - JSON format for machine parsing
   - Consistent field naming
   - Efficient serialization

### Storage Optimization

1. **Compression**
   - Gzip compression for rotated logs
   - Archive older logs to cold storage
   - Use efficient storage formats

2. **Indexing**
   - Index critical fields for search
   - Partition logs by time and module
   - Optimize query performance

## Security Considerations

### Log Security

1. **Access Control**
   - Restrict log file access to authorized users
   - Separate log access from application access
   - Audit log access activities

2. **Data Protection**
   - Encrypt logs in transit and at rest
   - Redact sensitive information
   - Use secure log transmission protocols

3. **Integrity Protection**
   - Implement log signing for critical events
   - Use immutable storage for security logs
   - Regular integrity verification

### Sensitive Data Handling

#### Data to Redact

- User passwords and tokens
- API keys and secrets
- Personal identifiable information (PII)
- Financial information
- Private communication content

#### Redaction Examples

```python
def redact_sensitive_data(log_data: dict) -> dict:
    """Redact sensitive information from log data"""
    sensitive_fields = ['password', 'token', 'api_key', 'secret']
    
    for field in sensitive_fields:
        if field in log_data:
            log_data[field] = '[REDACTED]'
    
    # Redact credit card numbers
    if 'card_number' in log_data:
        log_data['card_number'] = log_data['card_number'][:4] + '****'
    
    return log_data
```

## Troubleshooting

### Common Issues

1. **Log Directory Permissions**
   ```bash
   # Fix log directory permissions
   sudo mkdir -p /var/log/waddlebotlog
   sudo chown -R waddlebot:waddlebot /var/log/waddlebotlog
   sudo chmod 755 /var/log/waddlebotlog
   ```

2. **Log Rotation Issues**
   ```bash
   # Check logrotate configuration
   cat /etc/logrotate.d/waddlebot
   
   # Test logrotate
   sudo logrotate -d /etc/logrotate.d/waddlebot
   ```

3. **Disk Space Issues**
   ```bash
   # Monitor log disk usage
   du -sh /var/log/waddlebotlog/*
   
   # Clean old logs
   find /var/log/waddlebotlog -name "*.log.*" -mtime +30 -delete
   ```

### Log Analysis Tools

```bash
# View recent authentication events
grep "AUTH" /var/log/waddlebotlog/*/auth.log | tail -100

# Count authorization denials
grep "DENIED" /var/log/waddlebotlog/*/authz.log | wc -l

# Find errors in the last hour
find /var/log/waddlebotlog -name "*error.log" -exec grep "$(date -d '1 hour ago' '+%Y-%m-%d %H')" {} \;

# Monitor real-time logs
tail -f /var/log/waddlebotlog/*/*.log | grep ERROR
```

## Best Practices

### Development

1. **Consistent Logging**
   - Use the same log format across all modules
   - Implement logging early in development
   - Test logging functionality regularly

2. **Context Preservation**
   - Include request/session IDs in all related logs
   - Maintain user context throughout operation chains
   - Log both successful and failed operations

3. **Performance Testing**
   - Test logging performance under load
   - Monitor logging overhead in production
   - Optimize logging configuration for performance

### Operations

1. **Log Monitoring**
   - Set up automated monitoring and alerting
   - Regular log review and analysis
   - Proactive issue identification

2. **Incident Response**
   - Use logs for incident investigation
   - Maintain log availability during incidents
   - Document log analysis procedures

3. **Capacity Planning**
   - Monitor log volume growth
   - Plan for storage expansion
   - Optimize retention policies

## Implementation Checklist

### Module Implementation

- [ ] Implement WaddlesLogger class
- [ ] Configure all five log categories (AUTH, AUTHZ, AUDIT, ERROR, SYSTEM)
- [ ] Set up file rotation and retention
- [ ] Implement decorator patterns for automatic logging
- [ ] Add syslog support (optional)
- [ ] Create comprehensive unit tests for logging
- [ ] Document module-specific logging patterns
- [ ] Implement log redaction for sensitive data
- [ ] Set up health check logging
- [ ] Configure performance metrics logging

### Deployment

- [ ] Create log directory with correct permissions
- [ ] Configure environment variables
- [ ] Set up log aggregation (ELK/Fluentd)
- [ ] Configure monitoring and alerting
- [ ] Implement log backup and archival
- [ ] Set up log access controls
- [ ] Create operational runbooks
- [ ] Train operations team on log analysis
- [ ] Establish incident response procedures
- [ ] Schedule regular log review processes

This comprehensive logging and monitoring system ensures Waddles maintains the highest standards of security, compliance, and operational visibility across all modules.