# Translate Interaction Module — Overview

A standalone translation microservice extracted from `router_module` to allow independent scaling and deployment.

## Quick Reference

| Interface | Port | Purpose |
|-----------|------|---------|
| REST | 8033 | Health, cache admin, detect endpoint |
| gRPC | 50033 | Hot-path translate calls from router |

## Tech Stack

- **Language:** Python 3.12
- **REST Framework:** Quart + Hypercorn
- **gRPC:** grpcio + protobuf (proto3)
- **Database:** PyDAL → PostgreSQL
- **Cache:** In-memory LRU + Redis
- **Translation:** deep-translator (Google), WaddleAI fallback
- **Language Detection:** ensemble (fasttext + lingua + langdetect)

## What It Does

1. Receives a chat message + target language + community ID via gRPC from `router_module`
2. Strips platform emotes (BTTV, 7TV, FFZ, native Twitch/Discord/Slack) into placeholders
3. Detects source language using an ensemble of detectors
4. Skips translation if already in target language, too short, or confidence too low
5. Calls the configured translation provider (Google Cloud → free Google → WaddleAI)
6. Restores original emote tokens back into the translated text
7. Caches result at three tiers: in-memory LRU → Redis → PostgreSQL

## Directory Layout

```
translate_interaction_module/
├── app.py                    # Dual REST+gRPC entry point
├── config.py                 # All configuration via environment variables
├── requirements.txt
├── Dockerfile
├── proto/
│   ├── translate_interaction.proto
│   ├── translate_interaction_pb2.py      # generated
│   └── translate_interaction_pb2_grpc.py # generated
├── services/
│   ├── translation_service.py            # Core logic + 3-tier cache
│   ├── translation_preprocessor.py       # Token/emote preservation
│   ├── grpc_handler.py                   # gRPC servicer
│   └── translation_providers/            # Provider implementations
│       ├── base_provider.py
│       ├── googletrans_provider.py
│       ├── google_cloud_provider.py
│       ├── waddleai_provider.py
│       ├── ensemble_detector.py
│       └── emote_providers/              # BTTV, 7TV, FFZ, Discord, Slack
└── tests/
```

## Related Modules

- processing/router_module — calls this module via gRPC for every message
- `admin/hub_module` — manages `community_translation_settings` DB table (CRUD)
- core/identity_core_module — JWT validation key shared via `MODULE_SECRET_KEY`
