"""Professional-tier whitelabel gate for per-tenant branding.

A tenant's branding lives on the `tenants` row: `logo_url`, and
`config.theme` / `config.welcomeMessage`. It is APPLIED by the pre-auth
tenant login-info endpoint (`GET /api/v1/auth/tenant/<slug>`, consumed by
the login page) and WRITTEN by the tenant-admin `PUT /api/v1/tenant/<slug>`.
Both are gated here on one Feature contract -- `tenancy.whitelabel`
(Professional, flag `waddles.tenancy.whitelabel`, registered in
`libs/core_platform_module/features.py`):

* **Application** (`resolve_login_branding`): a tenant that is not
  entitled gets DEFAULT branding (no logo/theme/welcome override -- the
  client falls back to the stock Waddles look); an entitled one gets its
  custom branding, sanitized. Fail-closed: the gate is
  `flask_core.feature_flags.feature_enabled`'s two-gate (PostHog flag AND
  license tier), which answers `False` -- never raises -- when either gate
  is off or unreachable and nothing is cached, so an outage degrades a
  customer to default branding, never the other way round.
* **Write** (`sets_branding` + `whitelabel_enabled`, used by
  `blueprints/v1/tenant.py::update_tenant`): SETTING branding to a new
  non-empty value without entitlement is a 402. Only a real new value is
  gated -- a downgraded tenant's admin echoing back unchanged stored
  branding while editing something else, or clearing branding, is not
  blocked.

The gate is only evaluated when the tenant actually has custom branding
configured (`has_custom_branding`) -- a tenant with nothing to whitelabel
never triggers an entitlement evaluation (PostHog + license-server round
trip) on every login-page view.

Custom values are sanitized at application time: the logo must be
`https://` or a root-relative path (never `javascript:`/`data:`/
protocol-relative), text is length-capped and stripped of control
characters. The login page renders them through React (escaped); this is
defense in depth for any other consumer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from flask_core.feature_flags import feature_enabled

#: `libs/core_platform_module/features.py` -- `tenancy.whitelabel` (professional).
FEATURE_WHITELABEL = "waddles.tenancy.whitelabel"

#: `tenants.config` keys that carry branding (everything else in `config` is not gated).
BRANDING_CONFIG_KEYS: tuple[str, ...] = ("theme", "welcomeMessage")

_THEME_MAX = 100
_WELCOME_MAX = 500
_LOGO_MAX = 2048
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(slots=True, frozen=True)
class LoginBranding:
    """The branding fields the login-info endpoint returns. `None` = stock Waddles default."""

    logo_url: str | None
    theme: str | None
    welcome_message: str | None


DEFAULT_BRANDING = LoginBranding(logo_url=None, theme=None, welcome_message=None)


def _clean_text(value: Any, max_len: int) -> str | None:
    """Return `value` stripped of control chars and capped, or `None` if not usable text."""
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    if not cleaned or len(cleaned) > max_len:
        return None
    return cleaned


def _clean_logo_url(value: Any) -> str | None:
    """Allow only `https://...` or a root-relative `/path` (not protocol-relative `//host`)."""
    cleaned = _clean_text(value, _LOGO_MAX)
    if cleaned is None:
        return None
    if cleaned.startswith("https://") or (cleaned.startswith("/") and not cleaned.startswith("//")):
        return cleaned
    return None


def sanitize_branding(branding: LoginBranding) -> LoginBranding:
    """Return `branding` with every field validated; an invalid field falls back to default."""
    return LoginBranding(
        logo_url=_clean_logo_url(branding.logo_url),
        theme=_clean_text(branding.theme, _THEME_MAX),
        welcome_message=_clean_text(branding.welcome_message, _WELCOME_MAX),
    )


def has_custom_branding(branding: LoginBranding) -> bool:
    """True if any branding field is set to something other than the default."""
    return any((branding.logo_url, branding.theme, branding.welcome_message))


async def whitelabel_enabled(tenant_slug: str) -> bool:
    """The `tenancy.whitelabel` two-gate for `tenant_slug` -- fail-closed (`False` by default)."""
    return bool(await feature_enabled(FEATURE_WHITELABEL, tenant=tenant_slug, default=False))


async def resolve_login_branding(
    tenant_slug: str, stored: LoginBranding
) -> tuple[LoginBranding, bool]:
    """Return `(branding_to_serve, whitelabeled)` for a tenant's stored branding.

    Entitled tenants get their sanitized custom branding; everyone else
    gets `DEFAULT_BRANDING`. `whitelabeled` is `True` only when custom
    branding is actually being served.
    """
    if not has_custom_branding(stored):
        return DEFAULT_BRANDING, False
    if not await whitelabel_enabled(tenant_slug):
        return DEFAULT_BRANDING, False
    served = sanitize_branding(stored)
    return served, has_custom_branding(served)


def sets_branding(
    *,
    current_logo_url: str | None,
    current_config: dict[str, Any] | None,
    new_logo_url: str | None,
    new_config: dict[str, Any] | None,
) -> bool:
    """True if a tenant update would SET a branding field to a new, non-empty value.

    `None` for `new_logo_url`/`new_config` means "not provided" (leave as
    is), matching `tenant_service.update_tenant`. Only setting or changing
    branding to something non-empty needs entitlement: re-sending the value
    already stored is not a change, and CLEARING branding is always allowed
    (a downgraded tenant must be able to remove what it can no longer use).
    """
    if new_logo_url and new_logo_url != (current_logo_url or None):
        return True
    if new_config is not None:
        current = current_config or {}
        for key in BRANDING_CONFIG_KEYS:
            if new_config.get(key) and new_config.get(key) != current.get(key):
                return True
    return False
