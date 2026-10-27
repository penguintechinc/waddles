"""Consumer-group lifecycle only -- hub-api never reads/writes stream entries (spec Sec5.2, Sec9.5).

TLS/auth defaults mirror spec Sec11.6.4: `security.transport.tls`
(env `SECURITY_TRANSPORT_TLS`, default `true`) refuses a plaintext
`redis://` URL at construction time, matching every Rust service's own
startup check -- the opt-out is explicit and visible, never silent.

hub-api's role here is only consumer-group lifecycle -- creating the
`hub_api_usage_aggregator` group on `waddles:usage` (spec Sec5.12) --
never reading or writing stream entries; that is exclusively the Rust
stages' job (spec Sec5.2: "Bundles hold no Valkey connection... the
stage is the enforcement point").
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import redis.asyncio as redis_asyncio
import redis.exceptions

#: regression: seeder VALKEY_URL REPLACE_ME / missing TLS wiring. Same default path
#: `penguin_spine::SpineConfig`'s Rust services read their mounted CA from
#: (`templates/_helpers.tpl`'s `waddlebot.valkeyTlsCaVolumeMount`) -- using the identical
#: default here means the chart mounts one CA volume per pod and both the Rust and Python
#: consumers find it with zero per-language Helm divergence.
_DEFAULT_CA_FILE = "/etc/waddles/ca/valkey-ca.crt"


def _tls_required() -> bool:
    return os.environ.get("SECURITY_TRANSPORT_TLS", "true").lower() != "false"


def _with_password(url: str, password: str) -> str:
    """Inject `password` as the URL's userinfo (password-only auth, empty username).

    `templates/secrets.yaml`'s `VALKEY_URL_TLS` key is deliberately credential-free (see
    its own comment: the Rust data-plane pods take the password from a separate
    `VALKEY_PASSWORD` env var instead, so the URL never needs rewriting when the password
    rotates) -- this reconstructs the equivalent at connect time rather than requiring a
    second, Python-only secret key. No-op if `url` already carries credentials (an
    operator-supplied `VALKEY_URL` with embedded auth always wins) or `password` is empty.
    """
    if not password:
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    netloc = f":{password}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def build_client() -> Any:
    """A `redis.asyncio.Redis` from `VALKEY_URL`. Refuses a plaintext URL when TLS is required.

    `VALKEY_PASSWORD` (if set) is injected into the URL's userinfo before connecting --
    see `_with_password()`. Over `rediss://`, the CA bundle at `VALKEY_CA_FILE` (default:
    the same mount path the Rust data-plane pods use) is passed to `redis.asyncio` for
    server-certificate verification when the file is present on disk; absent, this falls
    back to the system trust store rather than silently disabling verification.
    """
    url = os.environ.get("VALKEY_URL", "rediss://valkey:6379/0")
    if _tls_required() and not url.startswith("rediss://"):
        raise ValueError(
            f"VALKEY_URL must use rediss:// when security.transport.tls is true (got {url!r})"
        )
    url = _with_password(url, os.environ.get("VALKEY_PASSWORD", ""))
    kwargs: dict[str, Any] = {}
    if url.startswith("rediss://"):
        ca_file = os.environ.get("VALKEY_CA_FILE", _DEFAULT_CA_FILE)
        if Path(ca_file).is_file():
            kwargs["ssl_ca_certs"] = ca_file
    return redis_asyncio.from_url(url, **kwargs)


async def ensure_group(client: Any, *, stream: str, group: str) -> None:
    """`XGROUP CREATE {stream} {group} $ MKSTREAM`.

    Tolerant of an already-existing group (BUSYGROUP).
    """
    try:
        await client.xgroup_create(stream, group, id="$", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def destroy_group(client: Any, *, stream: str, group: str) -> None:
    """`XGROUP DESTROY {stream} {group}`, tolerant of a stream/group that no longer exists."""
    try:
        await client.xgroup_destroy(stream, group)
    except redis.exceptions.ResponseError as exc:
        if "no such key" not in str(exc).lower() and "no such" not in str(exc).lower():
            raise
