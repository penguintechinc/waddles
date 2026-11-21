# Translate Interaction Module — Release Notes

## v1.0.0 (2026-02-16)

### Initial Release — Extraction from router_module

**What changed:**

- Extracted `TranslationService`, `TranslationPreprocessor`, and all `translation_providers/` from processing/router_module into a dedicated standalone microservice
- Added gRPC interface (port 50033) for router hot-path calls — replaces direct in-process Python call
- Added REST interface (port 8033) for health checks, admin cache management, and language detection
- Router now calls translate via gRPC stub (`proto_clients/translate_interaction_pb2_grpc.py`)
- `emote_providers/` sub-package retained in router's `services/` for `emote_service.py` compatibility
- Three-tier caching (in-memory LRU + Redis + PostgreSQL) carried over from router implementation
- Ensemble language detection (fasttext + lingua + langdetect) carried over
- Provider fallback chain (Google Cloud → GoogleTrans → WaddleAI) carried over
- Token preservation (emotes, mentions, commands, URLs) carried over via `TranslationPreprocessor`
- No DB migrations required — all tables (`translation_cache`, `ai_translation_decision_cache`, `community_translation_settings`, `caption_events`) already existed
- K8s: Deployment + Service added to `k8s/kustomize/base/interactive/translate.yaml`
- Docker: Dual-port `EXPOSE 8033 50033`, non-root user, proto generation at build time

**Migration notes for operators:**

1. Set `MODULE_SECRET_KEY` in K8s secrets — must match router's `SECRET_KEY`
2. Set `TRANSLATE_GRPC_HOST=interactive-translate:50033` in router configmap (already in `configmap.yaml`)
3. Deploy translate module before restarting router (router gracefully falls back to `None` on translate failure)
