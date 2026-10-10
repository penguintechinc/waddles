"""Best-effort PII redaction for prompts sent to EXTERNAL model APIs (BYOK).

The implementation lives in `flask_core.ai_guard.redact_pii` so hub-api's BYOK clients, the
WaddleAI proxy provider and any other egress share ONE redactor (email addresses, token/secret
shapes, SSNs, phone numbers, Luhn-valid card numbers, PEM private keys). This module is the
stable import path `clients.py` and its tests have always used.

Applied by `clients.py`'s `OpenAIClient`/`AnthropicClient` -- the two adapters whose traffic
leaves PenguinTech's infrastructure entirely -- to the system turn AND the user turn (including
any rendered retrieved context), and by the self-hosted Ollama tiers only when
`AI_REDACT_PII_SELF_HOSTED` is enabled: redaction is an egress-boundary control, not a blanket
content filter.
"""

from __future__ import annotations

from flask_core.ai_guard import redact_pii

__all__ = ["redact_pii"]
