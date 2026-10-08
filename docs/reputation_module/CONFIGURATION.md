# Reputation Module - Configuration

## Environment Variables

```bash
MODULE_PORT=8021
GRPC_PORT=50021
DATABASE_URL=postgresql://waddlebot:password@localhost:5432/waddlebot
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/api/v1/router
HUB_API_URL=http://hub-module:8060
LOG_LEVEL=INFO
SECRET_KEY=change-me-in-production
SERVICE_API_KEY=service-to-service-key
WEIGHT_CACHE_TTL=300
```

## Reputation Boundaries

```python
REPUTATION_MIN = 300
REPUTATION_MAX = 850
REPUTATION_DEFAULT = 600
REPUTATION_AUTO_BAN_THRESHOLD = 450
```

## Default Weights

```python
DEFAULT_WEIGHTS = {
    'chat_message': 0.01,
    'command_usage': -0.1,
    'giveaway_entry': -1.0,
    'follow': 1.0,
    'subscription': 5.0,
    'subscription_tier2': 10.0,
    'subscription_tier3': 20.0,
    'gift_subscription': 3.0,
    'donation_per_dollar': 1.0,
    'cheer_per_100bits': 1.0,
    'raid': 2.0,
    'boost': 5.0,
    'warn': -25.0,
    'timeout': -50.0,
    'kick': -75.0,
    'ban': -200.0,
}
```

## Reputation Tiers

```python
REPUTATION_TIERS = {
    'exceptional': {'min': 800, 'max': 850, 'label': 'Exceptional'},
    'very_good': {'min': 740, 'max': 799, 'label': 'Very Good'},
    'good': {'min': 670, 'max': 739, 'label': 'Good'},
    'fair': {'min': 580, 'max': 669, 'label': 'Fair'},
    'poor': {'min': 300, 'max': 579, 'label': 'Poor'},
}
```

## Weight Customization

Premium communities can customize weights. Non-premium use defaults.

## Database Tables

- `community_members`: Per-community reputation scores
- `reputation_events`: Audit log of all reputation changes
- `reputation_tenant`: Cross-community, single-tenant scores (never cross-tenant)
- `reputation_weights`: Custom weight configurations (premium only)
