"""Operator-controlled SSO settings, read from the process environment.

Everything here is set by the platform operator through the Helm chart
(`k8s/helm/waddlebot/templates/sso.yaml` + `values.yaml`'s `sso:` subtree), never
by a tenant admin and never from a request. Secrets (`SSO_ENCRYPTION_KEY`,
`SSO_GOOGLE_CLIENT_SECRET`) arrive as env vars sourced from Kubernetes Secrets
via `existingSecret`/`secretKeyRef` -- the chart never inlines a value.

Parsing is strict and fails loud: a malformed number is an `SsoConfigError`,
not a silent fall-back to the default, because a silently-defaulted security
parameter (clock skew, state TTL) is exactly the kind of drift nobody notices.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from services.sso_types import SsoConfigError

ENV_ENCRYPTION_KEY: Final = "SSO_ENCRYPTION_KEY"
ENV_GOOGLE_CLIENT_ID: Final = "SSO_GOOGLE_CLIENT_ID"
ENV_GOOGLE_CLIENT_SECRET: Final = "SSO_GOOGLE_CLIENT_SECRET"  # noqa: S105 - env var NAME, not a secret
ENV_ALLOWED_PRIVATE_HOSTS: Final = "SSO_ALLOWED_PRIVATE_HOSTS"
ENV_STATE_TTL: Final = "SSO_STATE_TTL_SECONDS"
ENV_CLOCK_SKEW: Final = "SSO_CLOCK_SKEW_SECONDS"
ENV_HTTP_TIMEOUT: Final = "SSO_HTTP_TIMEOUT_SECONDS"

_DEFAULT_STATE_TTL_S: Final = 600
_DEFAULT_CLOCK_SKEW_S: Final = 120
_DEFAULT_HTTP_TIMEOUT_S: Final = 10.0

GOOGLE_ISSUER: Final = "https://accounts.google.com"
GOOGLE_DISCOVERY_URL: Final = "https://accounts.google.com/.well-known/openid-configuration"


@dataclass(slots=True, frozen=True)
class SsoSettings:
    """Immutable snapshot of the operator's SSO configuration."""

    state_ttl_s: int = _DEFAULT_STATE_TTL_S
    clock_skew_s: int = _DEFAULT_CLOCK_SKEW_S
    http_timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S
    #: Hostnames the operator explicitly approved for private-address IdPs
    #: (on-prem Keycloak/ADFS). Anything else must resolve to a public address.
    allowed_private_hosts: frozenset[str] = field(default_factory=frozenset)
    google_client_id: str | None = None
    google_client_secret: str | None = field(default=None, repr=False)

    @property
    def platform_google_configured(self) -> bool:
        """True when the operator provisioned a shared Google OAuth client."""
        return bool(self.google_client_id and self.google_client_secret)


def _bounded_int(raw: str | None, *, name: str, default: int, low: int, high: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SsoConfigError("bad_setting", f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise SsoConfigError("bad_setting", f"{name} must be between {low} and {high}")
    return value


def _bounded_float(raw: str | None, *, name: str, default: float, low: float, high: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SsoConfigError("bad_setting", f"{name} must be a number") from exc
    if not low <= value <= high:
        raise SsoConfigError("bad_setting", f"{name} must be between {low} and {high}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> SsoSettings:
    """Build `SsoSettings` from `env` (default: `os.environ`); raise on any malformed value."""
    source = os.environ if env is None else env
    hosts_raw = source.get(ENV_ALLOWED_PRIVATE_HOSTS, "")
    hosts = frozenset(h.strip().lower() for h in hosts_raw.split(",") if h.strip())
    return SsoSettings(
        state_ttl_s=_bounded_int(
            source.get(ENV_STATE_TTL),
            name=ENV_STATE_TTL,
            default=_DEFAULT_STATE_TTL_S,
            low=60,
            high=3600,
        ),
        clock_skew_s=_bounded_int(
            source.get(ENV_CLOCK_SKEW),
            name=ENV_CLOCK_SKEW,
            default=_DEFAULT_CLOCK_SKEW_S,
            low=0,
            high=300,
        ),
        http_timeout_s=_bounded_float(
            source.get(ENV_HTTP_TIMEOUT),
            name=ENV_HTTP_TIMEOUT,
            default=_DEFAULT_HTTP_TIMEOUT_S,
            low=1.0,
            high=60.0,
        ),
        allowed_private_hosts=hosts,
        google_client_id=(source.get(ENV_GOOGLE_CLIENT_ID) or None),
        google_client_secret=(source.get(ENV_GOOGLE_CLIENT_SECRET) or None),
    )
