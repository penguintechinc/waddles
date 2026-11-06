"""RSI Spectrum adapter: a community's RSI session credential + the Spectrum community it ingests.

Capability: ``chat_ingest`` only. Spectrum is a one-way INBOUND source (gh #486 / #101): the data
plane's Spectrum connector polls forum/lobby/org feeds and never writes to RSI, so there is no
``action_relay`` capability and no outbound path to gate.

* **Credential** -- the RSI session token (the value of the ``Rsi-Token`` cookie of a normal RSI
  login). It is the ``access_token`` of the connection: sealed at rest like every other provider
  token, never returned by any admin endpoint, never logged, never placed in a URL. It is verified
  with a single read-only ``POST /api/spectrum/auth/identify`` (the call the Spectrum web client
  itself makes on load) which also reports the member id the session belongs to. RSI session
  cookies are not OAuth tokens: they carry no scopes, no refresh token and no stated lifetime, so
  the connection is non-refreshable and an expired session surfaces as the framework's
  ``expired`` status when the poller is refused -- the admin then replaces the credential.
* **Resource** -- the Spectrum community the connection ingests, given as ``metadata.community_id``
  (digits). RSI itself enforces membership when the connector polls, so resolving is a no-extra-call
  step: the community id is the connection's ``resource_id``.

The member id reported by RSI is stored as ``account_id`` (an opaque numeric id); the member's
handle / display name is NOT stored (PII boundary -- the account label stays empty).

Admin-only by construction: the connection routes already demand ``connections:admin`` plus a
DB-backed check that the caller administers THIS community (``authorize_community``).

The one URL is a fixed constant whose host is :data:`ALLOWED_HOSTS` and still goes through the
shared SSRF guard. Responses are size-bounded, redirects are never followed, and the token only
ever sits in request headers -- never in a URL, a log line, a span or an error message.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from services.errors import ApiError
from services.provider_connections.adapter import (
    INSTALL_KIND_CREDENTIALS,
    Account,
    AdapterSpec,
    BindingKey,
    CredentialField,
    Credentials,
    MetadataField,
    RefreshedTokens,
    ResolvedResource,
)
from services.provider_connections.errors import (
    CredentialsRejected,
    InvalidConnectionInput,
    ProviderUnavailable,
    RefreshNotConfigured,
)
from services.provider_connections.telemetry import provider_call
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)

PROVIDER_ID: Final = "spectrum"
RESOURCE_TYPE_COMMUNITY: Final = "spectrum_community"
CAPABILITY_CHAT_INGEST: Final = "chat_ingest"

RSI_IDENTIFY_URL: Final = "https://robertsspaceindustries.com/api/spectrum/auth/identify"
ALLOWED_HOSTS: Final = frozenset({"robertsspaceindustries.com"})

#: Spectrum community ids are numeric strings.
COMMUNITY_ID_PATTERN: Final = r"[0-9]{1,18}"
_MEMBER_ID_RE: Final = re.compile(r"[0-9]{1,18}")
#: What a session token may contain. Everything outside this set (``;`` ``,`` quotes, controls)
#: could break out of the ``Cookie`` header the connector builds, so it is refused up front.
_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9._~+/=-]{8,2048}")
_COOKIE_PREFIX: Final = "rsi-token="

_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
_MAX_RESPONSE_BYTES: Final = 262_144

SPECTRUM_SPEC: Final = AdapterSpec(
    provider=PROVIDER_ID,
    display_name="RSI Spectrum",
    platform=PROVIDER_ID,
    resource_type=RESOURCE_TYPE_COMMUNITY,
    install_kind=INSTALL_KIND_CREDENTIALS,
    capabilities=frozenset({CAPABILITY_CHAT_INGEST}),
    required_scopes=(),
    credential_fields=(
        CredentialField(
            "access_token",
            True,
            "RSI session token (the value of the Rsi-Token cookie of a logged-in RSI session). "
            "Stored encrypted and never shown again; replace it when the session expires.",
        ),
    ),
    metadata_fields=(
        MetadataField(
            "community_id",
            True,
            "Numeric id of the Spectrum community to ingest (read-only; nothing is ever posted).",
            pattern=COMMUNITY_ID_PATTERN,
            max_length=18,
        ),
    ),
    public_metadata_keys=frozenset({"community_id", "scopes", "capabilities"}),
    runtime_metadata_keys=frozenset({"community_id", "scopes", "capabilities"}),
    refreshable=False,
    binding_metadata=(BindingKey("community_id", "community_id", required=True),),
)


class SpectrumAdapter:
    """Spectrum implementation of the framework's ``ProviderAdapter`` protocol."""

    spec: AdapterSpec = SPECTRUM_SPEC

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Build the adapter (``transport`` swaps the socket in tests; the guard still runs)."""
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Strip a pasted ``Rsi-Token=`` prefix and refuse a token that is not header-safe."""
        token = credentials.access_token
        if token.lower().startswith(_COOKIE_PREFIX):
            token = token[len(_COOKIE_PREFIX) :]
        if not _TOKEN_RE.fullmatch(token):
            raise InvalidConnectionInput("access_token is not a valid RSI session token")
        return Credentials(access_token=token)

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """A session token carries no OAuth scopes: it grants exactly the spec's capabilities."""
        return self.spec.capabilities

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """``POST /auth/identify`` -- the session must resolve to a signed-in RSI member."""
        response = await self._send(credentials)
        if response.status_code in (401, 403):
            raise CredentialsRejected("RSI rejected the session token")
        if response.status_code != 200:
            logger.warning("spectrum.validate_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("RSI could not validate the session right now")
        payload = _json_object(response)
        data = payload.get("data")
        member = data.get("member") if isinstance(data, dict) else None
        member_id = member.get("id") if isinstance(member, dict) else None
        if isinstance(member_id, int) and not isinstance(member_id, bool):
            member_id = str(member_id)
        if payload.get("success") != 1 or member_id is None:
            # RSI answers an anonymous / expired session with success=0 and no member.
            raise CredentialsRejected("RSI did not recognise the session as signed in")
        if not (isinstance(member_id, str) and _MEMBER_ID_RE.fullmatch(member_id)):
            raise ProviderUnavailable("RSI returned an unexpected validate response")
        return Account(
            account_id=member_id, account_label="", app_id=member_id, scopes=(), expires_at=None
        )

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """The Spectrum community is the resource; RSI enforces membership at poll time."""
        community_id = metadata["community_id"]
        return ResolvedResource(
            resource_id=community_id,
            facts={"community_id": community_id, "member_id": account.account_id},
        )

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """RSI sessions do not refresh; reaching this is a framework bug, refused loudly."""
        raise RefreshNotConfigured("RSI session tokens are not refreshable")

    async def _send(self, credentials: Credentials) -> httpx.Response:
        """One guarded, timed, size-bounded POST; transport failure -> ProviderUnavailable."""
        if urlparse(RSI_IDENTIFY_URL).hostname not in ALLOWED_HOSTS:
            raise ProviderUnavailable("the outbound request is not to an allowed RSI host")
        try:
            await validate_outbound_url(RSI_IDENTIFY_URL, allowed_schemes=("https",))
        except ApiError as exc:
            raise ProviderUnavailable(
                "the outbound request was blocked by the network guard"
            ) from exc
        token = credentials.access_token
        headers = {"Cookie": f"Rsi-Token={token}", "X-Rsi-Token": token}
        with provider_call(PROVIDER_ID, "validate"):
            try:
                async with httpx.AsyncClient(
                    timeout=_TIMEOUT, transport=self._transport, follow_redirects=False
                ) as client:
                    response = await client.post(RSI_IDENTIFY_URL, json={}, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning("spectrum.request_failed call=validate")
                raise ProviderUnavailable("RSI could not be reached") from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ProviderUnavailable("RSI returned an unexpectedly large response")
        logger.debug("spectrum.call_done call=validate status=%d", response.status_code)
        return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON object body or raise :class:`ProviderUnavailable` (never echoes the body)."""
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise ProviderUnavailable("RSI returned a malformed response") from exc
    if not isinstance(payload, dict):
        raise ProviderUnavailable("RSI returned a malformed response")
    return payload
