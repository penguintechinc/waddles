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

The missing-CA WARN below never interpolates `redis_url` (or anything
derived from it, even "redacted") into the log line -- CodeQL's clear-text-
logging-of-sensitive-data query treats any value that has flowed through a
credential-bearing URL as tainted regardless of custom redaction logic, so
the only CodeQL-clean fix is for no part of the URL to ever reach a logger
call in the first place.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Same mount path as `hub_api.services.valkey_admin_client._DEFAULT_CA_FILE`
#: and the chart's `waddlebot.valkeyTlsCaVolumeMount` -- must never drift
#: from either.
DEFAULT_CA_FILE = "/etc/waddles/ca/valkey-ca.crt"

#: Set True after the first missing-CA WARN -- one WARN for the process
#: lifetime, not once per connection attempt/retry, to avoid log-flooding a
#: crash-looping pod while still being loud on first sight. A single
#: process-wide flag (not keyed by URL) is deliberate: it sidesteps ever
#: needing to derive a log-safe identifier from a credential-bearing URL.
_warned_missing_ca = False


def build_tls_kwargs(redis_url: str | None, ca_file: str | None = None) -> dict[str, Any]:
    """redis-py `ssl_*` kwargs for `redis.asyncio.from_url(redis_url, **kwargs)`.

    Returns `{}` for a plain `redis://` URL (or a falsy one) -- TLS kwargs are
    never attached to a non-TLS connection. For `rediss://`, always sets
    `ssl_cert_reqs="required"` (certificate verification is never disabled,
    on purpose -- there is no opt-out here); additionally sets
    `ssl_ca_certs` to `ca_file` (default: the `VALKEY_CA_FILE` env var,
    falling back to `DEFAULT_CA_FILE`) when that file exists on disk. When it
    does not exist, logs one WARN (process lifetime, not per call -- see
    `_warned_missing_ca`) and returns just `ssl_cert_reqs` -- falls back to
    the system trust store rather than silently disabling verification,
    matching `valkey_admin_client.build_client()`'s existing behavior.
    """
    global _warned_missing_ca

    if not redis_url or not redis_url.startswith("rediss://"):
        return {}

    resolved_ca = ca_file if ca_file is not None else os.environ.get("VALKEY_CA_FILE", DEFAULT_CA_FILE)
    kwargs: dict[str, Any] = {"ssl_cert_reqs": "required"}

    if Path(resolved_ca).is_file():
        kwargs["ssl_ca_certs"] = resolved_ca
        return kwargs

    if not _warned_missing_ca:
        # Deliberately no interpolation of `redis_url`/`ca_file` origin
        # here -- see module docstring.
        logger.warning(
            "A rediss:// Valkey/Redis URL is configured but no CA file was found "
            "on disk -- falling back to the system trust store, which will reject "
            "a self-signed Valkey CA (CERTIFICATE_VERIFY_FAILED). Set "
            "VALKEY_CA_FILE or mount the chart's valkey-ca secret."
        )
        _warned_missing_ca = True

    return kwargs
