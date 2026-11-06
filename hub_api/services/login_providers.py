"""Single source of truth for which OAuth login/link providers are enabled.

Only providers backed by a platform connector service bundle on the release line
(Discord, Twitch, and Google -- the YouTube identity path) are on by default. Every other
provider (Slack today) keeps its code but is dark until its flag is turned on.

Each provider has one flag `waddles.login-<provider>` evaluated through
`flask_core.feature_flags.feature_enabled` (PostHog, then the `FLAG_WADDLES_LOGIN_<PROVIDER>`
ENV baseline, then the default below). Not a licensed feature -- no tier gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.feature_flags import feature_enabled

from services.errors import ApiError

#: Error code surfaced when a disabled (or unknown) provider is requested.
ERR_PROVIDER_DISABLED = "provider_disabled"

#: Every provider the OAuth code knows about, in display order, with its default state.
_PROVIDER_DEFAULTS: tuple[tuple[str, str, bool], ...] = (
    ("discord", "Discord", True),
    ("twitch", "Twitch", True),
    ("google", "Google", True),
    ("slack", "Slack", False),
)

ALL_PROVIDERS: tuple[str, ...] = tuple(p for p, _, _ in _PROVIDER_DEFAULTS)


@dataclass(slots=True, frozen=True)
class LoginProvider:
    """One enabled login/link provider as exposed to clients."""

    platform: str
    display_name: str


def flag_key(platform: str) -> str:
    """PostHog flag key for `platform` (`waddles.login-<provider>`)."""
    return f"waddles.login-{platform}"


async def is_enabled(platform: str) -> bool:
    """True iff `platform` is a known provider whose flag evaluates on."""
    for name, _, default in _PROVIDER_DEFAULTS:
        if name == platform:
            return bool(
                await feature_enabled(flag_key(name), tenant=DEFAULT_TENANT_SLUG, default=default)
            )
    return False


async def enabled_providers() -> list[LoginProvider]:
    """Enabled providers in display order."""
    out: list[LoginProvider] = []
    for name, label, default in _PROVIDER_DEFAULTS:
        if await feature_enabled(flag_key(name), tenant=DEFAULT_TENANT_SLUG, default=default):
            out.append(LoginProvider(platform=name, display_name=label))
    return out


async def require_enabled(platform: str) -> None:
    """Raise a typed 404 `provider_disabled` unless `platform` is enabled -- never falls back."""
    if not await is_enabled(platform):
        raise ApiError("Provider not available", 404, ERR_PROVIDER_DISABLED)
