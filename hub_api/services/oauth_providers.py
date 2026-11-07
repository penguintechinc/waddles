"""Provider registry + token exchange for per-community OAuth "Connections" (issue #320, chunk C2).

Pure HTTP against each provider's own OAuth endpoints -- no DB access here.
Callers (the C1 DB service, C3 blueprint) own persistence of tokens/state;
this module only knows how to build an authorize URL, exchange a code (or
refresh token) for a `TokenResponse`, and best-effort fetch a human-readable
account label for a just-connected token.

Security posture: every outbound request goes through `services.url_guard.
validate_outbound_url` first (defense-in-depth -- provider endpoints below
are fixed constants in `PROVIDERS`, not user-supplied, but the guard is
applied uniformly per this module's security contract rather than assumed
safe because "we wrote the URL"). Tokens are never logged -- failure logs
carry provider name + HTTP status only, and every `OAuthExchangeError`
message is built from those same two fields (or a provider's own `error`
code, itself never a token), safe to surface to a user.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from services.errors import ApiError
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)

#: Connect within 5s, whole request (connect + read/write) within 15s -- see
#: `client.md` Update Checks / general outbound-call hygiene; matches the
#: 5s/15s split called out in this chunk's own instructions.
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

_ALLOWED_SCHEMES: tuple[str, ...] = ("https",)


class ProviderNotConfigured(RuntimeError):  # noqa: N818 - contract-mandated name (issue #320 C2)
    """Raised by `client_credentials()` when a provider's client id/secret env vars are absent."""


class OAuthExchangeError(RuntimeError):
    """Raised when a provider's token/API endpoint returns a non-2xx or malformed response.

    Message is always safe to show a user -- provider name, HTTP status,
    and/or the provider's own `error` code only, never a token or raw
    response body.
    """


@dataclass(slots=True, frozen=True)
class ProviderSpec:
    """Static OAuth configuration for one connectable provider -- no per-user state."""

    name: str
    display_name: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    extra_authorize_params: Mapping[str, str]
    uses_pkce: bool
    client_id_env: str
    client_secret_env: str
    token_auth: Literal["body", "basic"]


@dataclass(slots=True)
class TokenResponse:
    """Normalized token-exchange/refresh result -- provider response shapes vary, this doesn't."""

    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scopes: list[str]
    token_type: str


PROVIDERS: dict[str, ProviderSpec] = {
    "youtube": ProviderSpec(
        name="youtube",
        display_name="YouTube",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106 - URL, not a credential
        scopes=(
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/youtube.force-ssl",
        ),
        extra_authorize_params=MappingProxyType(
            {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}
        ),
        uses_pkce=False,
        client_id_env="YOUTUBE_CLIENT_ID",
        client_secret_env="YOUTUBE_CLIENT_SECRET",  # noqa: S106 - env var name, not a credential
        token_auth="body",  # noqa: S106 - auth-mode literal, not a credential
    ),
    "spotify": ProviderSpec(
        name="spotify",
        display_name="Spotify",
        authorize_url="https://accounts.spotify.com/authorize",
        token_url="https://accounts.spotify.com/api/token",  # noqa: S106 - URL, not a credential
        scopes=(
            "user-read-playback-state",
            "user-modify-playback-state",
            "user-read-currently-playing",
            "playlist-read-private",
        ),
        extra_authorize_params=MappingProxyType({}),
        uses_pkce=False,
        client_id_env="SPOTIFY_CLIENT_ID",
        client_secret_env="SPOTIFY_CLIENT_SECRET",  # noqa: S106 - env var name, not a credential
        token_auth="basic",  # noqa: S106 - auth-mode literal, not a credential
    ),
    "twitch": ProviderSpec(
        name="twitch",
        display_name="Twitch",
        authorize_url="https://id.twitch.tv/oauth2/authorize",
        token_url="https://id.twitch.tv/oauth2/token",  # noqa: S106 - URL, not a credential
        scopes=(
            "chat:read",
            "chat:edit",
            "moderator:manage:banned_users",
            "moderator:manage:chat_messages",
            "moderator:manage:shoutouts",
            "channel:manage:broadcast",
        ),
        extra_authorize_params=MappingProxyType({}),
        uses_pkce=False,
        client_id_env="TWITCH_CLIENT_ID",
        client_secret_env="TWITCH_CLIENT_SECRET",  # noqa: S106 - env var name, not a credential
        token_auth="body",  # noqa: S106 - auth-mode literal, not a credential
    ),
    "discord": ProviderSpec(
        name="discord",
        display_name="Discord",
        authorize_url="https://discord.com/oauth2/authorize",
        token_url="https://discord.com/api/oauth2/token",  # noqa: S106 - URL, not a credential
        scopes=("identify", "guilds"),
        extra_authorize_params=MappingProxyType({}),
        uses_pkce=False,
        client_id_env="DISCORD_CLIENT_ID",
        client_secret_env="DISCORD_CLIENT_SECRET",  # noqa: S106 - env var name, not a credential
        token_auth="body",  # noqa: S106 - auth-mode literal, not a credential
    ),
    "kick": ProviderSpec(
        name="kick",
        display_name="Kick",
        authorize_url="https://id.kick.com/oauth/authorize",
        token_url="https://id.kick.com/oauth/token",  # noqa: S106 - URL, not a credential
        scopes=(
            "user:read",
            "channel:read",
            "chat:write",
            "moderation:ban",
            "moderation:chat_message:manage",
            "events:subscribe",
        ),
        extra_authorize_params=MappingProxyType({}),
        uses_pkce=True,
        client_id_env="KICK_CLIENT_ID",
        client_secret_env="KICK_CLIENT_SECRET",  # noqa: S106 - env var name, not a credential
        token_auth="body",  # noqa: S106 - auth-mode literal, not a credential
    ),
    "slack": ProviderSpec(
        name="slack",
        display_name="Slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",  # noqa: S106 - URL, not a credential
        scopes=("chat:write", "channels:read"),
        extra_authorize_params=MappingProxyType({}),
        uses_pkce=False,
        client_id_env="SLACK_CLIENT_ID",
        client_secret_env="SLACK_CLIENT_SECRET",  # noqa: S106 - env var name, not a credential
        token_auth="body",  # noqa: S106 - auth-mode literal, not a credential
    ),
}

#: Best-effort account-label endpoint per provider -- see `fetch_account_label()`.
#: Slack has no plain "who am I" REST resource keyed off a bot token beyond
#: `auth.test` (the token-exchange response's own `team.name` isn't
#: available here since this function only receives `access_token`) --
#: called out as an assumption in this chunk's own report, not a verified
#: provider fact.
_LABEL_URLS: dict[str, str] = {
    "youtube": "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
    "spotify": "https://api.spotify.com/v1/me",
    "twitch": "https://api.twitch.tv/helix/users",
    "discord": "https://discord.com/api/users/@me",
    "kick": "https://api.kick.com/public/v1/users",
    "slack": "https://slack.com/api/auth.test",
}


def get_provider(name: str) -> ProviderSpec:
    """Look up a `ProviderSpec` by key. Raises `ValueError` (never `KeyError`) if unknown."""
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError("unsupported provider") from None


def client_credentials(spec: ProviderSpec) -> tuple[str, str]:
    """Resolve `(client_id, client_secret)` from env for `spec`.

    Raises `ProviderNotConfigured` if either env var is absent or empty --
    fail closed, never falls back to a placeholder credential.
    """
    client_id = os.getenv(spec.client_id_env)
    client_secret = os.getenv(spec.client_secret_env)
    if not client_id or not client_secret:
        raise ProviderNotConfigured(
            f"{spec.name}: missing {spec.client_id_env} or {spec.client_secret_env}"
        )
    return client_id, client_secret


def make_pkce_pair() -> tuple[str, str]:
    """Generate an RFC 7636 S256 PKCE `(code_verifier, code_challenge)` pair.

    `code_verifier` is 43-128 URL-safe characters drawn from `secrets`
    (cryptographically strong, not `random`); `code_challenge` is the
    base64url-no-padding SHA-256 digest of the verifier, per RFC 7636 §4.2.
    """
    code_verifier = secrets.token_urlsafe(96)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_authorize_url(
    name: str, *, redirect_uri: str, state: str, code_challenge: str | None = None
) -> str:
    """Build the provider's authorize-redirect URL for a login/connect flow.

    Raises `ValueError` for an unknown provider (via `get_provider`),
    `ProviderNotConfigured` if the provider's client id isn't set, and
    `ValueError` if `spec.uses_pkce` is `True` but no `code_challenge` was
    given (Kick requires PKCE; a caller skipping it would otherwise send an
    authorize request the token exchange can never complete).
    """
    spec = get_provider(name)
    client_id, _ = client_credentials(spec)

    if spec.uses_pkce and code_challenge is None:
        raise ValueError(f"{spec.name}: uses_pkce requires a code_challenge")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(spec.scopes),
        "state": state,
        **spec.extra_authorize_params,
    }
    if code_challenge is not None:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

    return f"{spec.authorize_url}?{urlencode(params)}"


async def _guard_url(url: str) -> None:
    """Re-validate `url` through the shared SSRF guard before any outbound request.

    Provider endpoints are fixed constants in `PROVIDERS`/`_LABEL_URLS`, not
    user input, so this is defense-in-depth rather than a control against an
    attacker-supplied target -- applied uniformly per this module's security
    contract. Raises `OAuthExchangeError` (never `services.errors.ApiError`,
    keeping this module's exception surface self-contained per its
    contract) on rejection.
    """
    try:
        await validate_outbound_url(url, allowed_schemes=_ALLOWED_SCHEMES)
    except ApiError as exc:
        raise OAuthExchangeError(f"outbound URL blocked: {exc.message}") from exc


def _token_request_payload(
    spec: ProviderSpec, data: dict[str, str], client_id: str, client_secret: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Split `data` between the form body and headers per `spec.token_auth`."""
    headers = {"Accept": "application/json"}
    if spec.token_auth == "basic":  # noqa: S105 - auth-mode literal, not a credential
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
        return data, headers
    return {**data, "client_id": client_id, "client_secret": client_secret}, headers


async def _post_token(
    spec: ProviderSpec, data: dict[str, str], client_id: str, client_secret: str
) -> dict[str, Any]:
    """POST `data` to `spec.token_url`, returning the decoded JSON object.

    Raises `OAuthExchangeError` on transport failure, a non-2xx response, or
    a response body that isn't a JSON object.
    """
    body, headers = _token_request_payload(spec, data, client_id, client_secret)
    await _guard_url(spec.token_url)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(spec.token_url, data=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("oauth_providers.token_request_failed provider=%s", spec.name)
        raise OAuthExchangeError(f"{spec.name}: token request failed") from exc

    if response.status_code // 100 != 2:
        logger.warning(
            "oauth_providers.token_request_non_2xx provider=%s status=%d",
            spec.name,
            response.status_code,
        )
        raise OAuthExchangeError(
            f"{spec.name}: token endpoint returned HTTP {response.status_code}"
        )

    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise OAuthExchangeError(f"{spec.name}: token endpoint returned malformed JSON") from exc

    if not isinstance(payload, dict):
        raise OAuthExchangeError(f"{spec.name}: token endpoint returned malformed JSON")

    if spec.name == "slack" and not payload.get("ok", True):
        raise OAuthExchangeError(f"slack: token exchange rejected ({payload.get('error')})")

    return payload


def _token_response_from_payload(spec: ProviderSpec, payload: dict[str, Any]) -> TokenResponse:
    """Map a provider's raw token JSON object into a normalized `TokenResponse`."""
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthExchangeError(f"{spec.name}: token endpoint response missing access_token")

    refresh_token_raw = payload.get("refresh_token")
    refresh_token = str(refresh_token_raw) if refresh_token_raw else None

    expires_in_raw = payload.get("expires_in")
    expires_in = int(expires_in_raw) if isinstance(expires_in_raw, int | float) else None

    scope_raw = payload.get("scope")
    if isinstance(scope_raw, str):
        scopes = scope_raw.replace(",", " ").split()
    elif isinstance(scope_raw, list):
        scopes = [str(item) for item in scope_raw]
    else:
        scopes = list(spec.scopes)

    token_type = str(payload.get("token_type") or "Bearer")

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        scopes=scopes,
        token_type=token_type,
    )


async def exchange_code(
    name: str, *, code: str, redirect_uri: str, code_verifier: str | None = None
) -> TokenResponse:
    """Exchange an authorization `code` for a `TokenResponse`.

    Raises `ValueError` (unknown provider), `ProviderNotConfigured` (env
    creds absent), or `OAuthExchangeError` (transport failure, non-2xx,
    malformed JSON, or Slack's `ok: false`).
    """
    spec = get_provider(name)
    client_id, client_secret = client_credentials(spec)

    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    if code_verifier is not None:
        data["code_verifier"] = code_verifier

    payload = await _post_token(spec, data, client_id, client_secret)
    return _token_response_from_payload(spec, payload)


async def refresh_access_token(name: str, *, refresh_token: str) -> TokenResponse:
    """Refresh an access token. `TokenResponse.refresh_token` is `None` if the provider omits it.

    Raises `ValueError` (unknown provider), `ProviderNotConfigured` (env
    creds absent), or `OAuthExchangeError` (transport failure, non-2xx,
    malformed JSON, or Slack's `ok: false`).
    """
    spec = get_provider(name)
    client_id, client_secret = client_credentials(spec)

    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    payload = await _post_token(spec, data, client_id, client_secret)
    return _token_response_from_payload(spec, payload)


def _extract_label(name: str, payload: Any) -> str | None:
    """Pick a human-readable account label out of a provider's profile-endpoint JSON."""
    if not isinstance(payload, dict):
        return None

    if name == "youtube":
        items = payload.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            snippet = items[0].get("snippet")
            title = snippet.get("title") if isinstance(snippet, dict) else None
            return str(title) if title else None
        return None

    if name == "spotify":
        display_name = payload.get("display_name")
        return str(display_name) if display_name else None

    if name == "twitch":
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            login = data[0].get("login")
            return str(login) if login else None
        return None

    if name == "discord":
        username = payload.get("username")
        return str(username) if username else None

    if name == "kick":
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            label = data[0].get("name")
            return str(label) if label else None
        return None

    if name == "slack":
        if not payload.get("ok"):
            return None
        team = payload.get("team")
        return str(team) if team else None

    return None


async def _fetch_label_uncaught(spec: ProviderSpec, access_token: str) -> str | None:
    """Do the real network call for `fetch_account_label` -- exceptions propagate."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if spec.name == "twitch":
        client_id, _ = client_credentials(spec)
        headers["Client-Id"] = client_id

    url = _LABEL_URLS[spec.name]
    await _guard_url(url)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        if spec.name == "slack":
            response = await client.post(url, headers=headers)
        else:
            response = await client.get(url, headers=headers)

    if response.status_code // 100 != 2:
        return None
    return _extract_label(spec.name, response.json())


async def fetch_account_label(name: str, access_token: str) -> str | None:
    """Best-effort human-readable account label for a just-connected token.

    Never raises -- any failure (unknown provider, network error, non-2xx,
    malformed JSON, missing field) returns `None`; callers show a generic
    "Connected" label instead of failing the connect flow over a cosmetic
    lookup.
    """
    try:
        spec = get_provider(name)
    except ValueError:
        return None

    try:
        return await _fetch_label_uncaught(spec, access_token)
    except Exception:
        logger.debug("oauth_providers.fetch_account_label_failed provider=%s", name)
        return None
