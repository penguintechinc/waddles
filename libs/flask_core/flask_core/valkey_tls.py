"""Shared TLS kwargs builder for flask_core's Redis/Valkey clients.

regression: rate limiter ignored Valkey CA, silent in-memory fallback (alpha
2026-10-02) -- `RateLimiter.connect()` called `redis.from_url()` with no
`ssl_ca_certs`, so it verified the chart's self-signed Valkey CA against the
system trust store instead and failed with `CERTIFICATE_VERIFY_FAILED`. The
non-fatal symptom (silent degrade to `enable_fallback=True`'s in-memory mode)
masked the real cause in alpha logs. `hub_api/services/valkey_admin_client.py`
already had this right; this module is the one place that logic now lives so
`cache.py`, `message_queue.py`, `rate_limiter.py` and `stream_pipeline.py`
(every flask_core module that opens its own `redis.asyncio` client) build
their TLS kwargs identically instead of four independent near-copies.

Default CA path/env var mirror `valkey_admin_client._DEFAULT_CA_FILE` and the
chart's `waddlebot.valkeyTlsCaVolumeMount` (same mounted file every Rust
data-plane pod reads) -- one CA volume per pod, zero per-module divergence.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Same mount path as `hub_api.services.valkey_admin_client._DEFAULT_CA_FILE`
#: and the chart's `waddlebot.valkeyTlsCaVolumeMount` -- must never drift
#: from either.
DEFAULT_CA_FILE = "/etc/waddles/ca/valkey-ca.crt"

#: Redacted URLs already warned about (missing CA file) -- one WARN per
#: distinct endpoint, not once per connection attempt/retry, to avoid
#: log-flooding a crash-looping pod while still being loud on first sight.
_warned_missing_ca: set[str] = set()


def _redact_url(url: str) -> str:
    """Strip userinfo (password) from `url` before it ever reaches a log line."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[-1]
    return parts._replace(netloc=host).geturl()


def build_tls_kwargs(redis_url: str | None, ca_file: str | None = None) -> dict[str, Any]:
    """redis-py `ssl_*` kwargs for `redis.asyncio.from_url(redis_url, **kwargs)`.

    Returns `{}` for a plain `redis://` URL (or a falsy one) -- TLS kwargs are
    never attached to a non-TLS connection. For `rediss://`, always sets
    `ssl_cert_reqs="required"` (certificate verification is never disabled,
    on purpose -- there is no opt-out here); additionally sets
    `ssl_ca_certs` to `ca_file` (default: the `VALKEY_CA_FILE` env var,
    falling back to `DEFAULT_CA_FILE`) when that file exists on disk. When it
    does not exist, logs one WARN (per distinct, credential-redacted URL) and
    returns just `ssl_cert_reqs` -- falls back to the system trust store
    rather than silently disabling verification, matching
    `valkey_admin_client.build_client()`'s existing behavior.
    """
    if not redis_url or not redis_url.startswith("rediss://"):
        return {}

    resolved_ca = ca_file if ca_file is not None else os.environ.get("VALKEY_CA_FILE", DEFAULT_CA_FILE)
    kwargs: dict[str, Any] = {"ssl_cert_reqs": "required"}

    if Path(resolved_ca).is_file():
        kwargs["ssl_ca_certs"] = resolved_ca
        return kwargs

    redacted = _redact_url(redis_url)
    if redacted not in _warned_missing_ca:
        logger.warning(
            "rediss:// URL %s configured but no Valkey CA file found at %s -- "
            "falling back to the system trust store, which will reject the "
            "chart's self-signed Valkey CA (CERTIFICATE_VERIFY_FAILED). Set "
            "VALKEY_CA_FILE or mount the chart's valkey-ca secret.",
            redacted,
            resolved_ca,
        )
        _warned_missing_ca.add(redacted)

    return kwargs
