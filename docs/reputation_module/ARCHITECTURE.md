# Reputation Module - Architecture

## System Overview

FICO-style reputation system with event-driven scoring, premium weight customization, and automated policy enforcement.

## Architecture Diagram

```
┌────────────────────────────────────────────┐
│         Reputation Module                  │
│                                            │
│  ┌────────────┐     ┌──────────────────┐  │
│  │  REST API  │     │   gRPC Server    │  │
│  │ (Port8021) │     │  (Port 50021)    │  │
│  └──────┬─────┘     └────────┬─────────┘  │
│         │                    │             │
│         ▼                    ▼             │
│  ┌──────────────────────────────────────┐ │
│  │      Event Processor                 │ │
│  │  - Batch processing                  │ │
│  │  - Weight application                │ │
│  │  - Policy enforcement                │ │
│  └────────┬─────────────────────────────┘ │
│           │                                │
│           ▼                                │
│  ┌──────────────────┐  ┌───────────────┐ │
│  │ Reputation       │  │ Weight        │ │
│  │ Service          │  │ Manager       │ │
│  │ - Scoring        │  │ - Custom cfg  │ │
│  │ - Tiers          │  │ - Cache       │ │
│  │ - History        │  │ - Defaults    │ │
│  └──────────────────┘  └───────────────┘ │
│           │                                │
│           ▼                                │
│  ┌──────────────────┐  ┌───────────────┐ │
│  │ Policy Enforcer  │  │  Database     │ │
│  │ - Auto-ban       │  │  (pyDAL)      │ │
│  │ - At-risk users  │  └───────────────┘ │
│  └──────────────────┘                     │
└────────────────────────────────────────────┘
```

## Core Components

### ReputationService
- Calculate reputation scores (FICO-style 300-850)
- Determine tiers (Exceptional, Very Good, Good, Fair, Poor)
- Maintain community and tenant reputation (tenant aggregate never spans tenants)
- Generate leaderboards

### EventProcessor
- Process events in batches
- Apply weights to events
- Update community_members table
- Update reputation_tenant table
- Trigger policy enforcement

### WeightManager
- Load default weights
- Load custom weights (premium only)
- Cache weights (TTL: 5 minutes)
- Validate weight updates

### PolicyEnforcer
- Auto-ban when score < threshold
- Identify at-risk users
- Enforce reputation policies
- Integration with moderation module

## Data Flow

### Event Processing Flow
```
1. Router → POST /internal/events
2. Verify X-Service-Key
3. EventProcessor.process_batch()
4. For each event:
   - Get weights
   - Calculate score_change
   - Update community reputation
   - Update global reputation
   - Create audit log
   - Check policy triggers
5. Return batch result
```

### Reputation Calculation
```python
current_score = 600  # default
event_weight = 5.0   # subscription
multiplier = 1.0     # default

score_change = event_weight * multiplier
new_score = clamp(current_score + score_change, 300, 850)
tier = calculate_tier(new_score)
```

### Auto-Ban Trigger
```
if new_score < auto_ban_threshold:
    PolicyEnforcer.trigger_auto_ban()
    → Call moderation module
    → Ban user across platforms
```

## Database Schema

### community_members
```sql
reputation INTEGER DEFAULT 600,
total_events INTEGER DEFAULT 0,
last_event_at TIMESTAMP
```

### reputation_events
```sql
event_type VARCHAR(50),
score_change DECIMAL(10,2),
score_before INTEGER,
score_after INTEGER,
reason TEXT,
metadata JSONB
```

### reputation_tenant
```sql
tenant_id INTEGER NOT NULL,
hub_user_id INTEGER NOT NULL,
score INTEGER DEFAULT 600,
total_events INTEGER,
last_event_at TIMESTAMP,
PRIMARY KEY (tenant_id, hub_user_id)
```

## Scalability

- gRPC for high-throughput event processing
- Weight caching (TTL: 5 min)
- Batch processing (up to 1000 events)
- Database connection pooling
