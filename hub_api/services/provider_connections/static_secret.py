"""Static-secret provider adapters (external HTTP, AWS Lambda, GCP function) + their validators.

These three providers have no OAuth install: an admin pastes a structured secret (bearer token,
AWS access key, GCP service-account key ...) and names a target (URL, or region/function). The
secret is sealed at rest as one JSON document (``platform_connections.sealed_secret``, row-bound
AAD) and never returned by any admin route.

``validate_credentials`` here is **structural only** -- no outbound call is ever made at write time.
Host policy and the SSRF DNS-pin are the *send-time* concern of the dispatcher; probing an
admin-supplied URL from the write path would itself be an SSRF primitive.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

from services.provider_connections.adapter import (
    INSTALL_KIND_CREDENTIALS,
    Account,
    AdapterSpec,
    Credentials,
    MetadataField,
    RefreshedTokens,
    ResolvedResource,
)
from services.provider_connections.errors import (
    CredentialsRejected,
    InvalidConnectionInput,
    RefreshNotConfigured,
)
from services.provider_connections.kinds import ConnectionKind, SecretScheme

#: Longest accepted endpoint URL.
MAX_URL_LENGTH: Final = 2048
#: Longest accepted single secret-document value (a GCP private key PEM is ~1.7 KiB).
MAX_SECRET_VALUE_LENGTH: Final = 4096

#: RFC 3986 host/port grammar and nothing else: a DNS-ish run of ``[A-Za-z0-9.-]`` or a bracketed
#: IPv6 literal, an optional numeric port, then path/query free of whitespace, controls, ``#`` and
#: ``\``. No userinfo (``@``), no ``%`` in the host, no fragment. ``fullmatch`` also rejects a
#: trailing newline, which ``$`` would let by.
_URL_RE: Final = re.compile(
    r"https://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?(?:[/?][^\s\x00-\x1f\x7f#\\]*)?"
)
_DIGITS_AND_DOTS_RE: Final = re.compile(r"[0-9.]+")
_DNS_LABEL_RE: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
#: A real TLD starts with a letter (``com``, ``xn--p1ai``). ``0x1`` / ``017`` / ``1`` as the last
#: label is how ``inet_aton`` spells an IPv4 address (``127.0.0.0x1`` == 127.0.0.1), so it is
#: refused.
_TLD_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_AWS_REGION_RE: Final = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}")
_AWS_FUNCTION_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_GCP_PROJECT_RE: Final = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
_GCP_REGION_RE: Final = re.compile(r"[a-z]+-[a-z]+\d{1,2}")
_GCP_FUNCTION_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,62}")
_HEADER_NAME_RE: Final = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
_CGNAT: Final = ipaddress.ip_network("100.64.0.0/10")
#: IPv6 ranges that embed or tunnel an IPv4 address: NAT64 and 6to4 can reach a private v4 target.
_V6_TUNNELS: Final = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("2002::/16"))

#: Exact key set each scheme's secret document must carry -- nothing missing, nothing extra.
_SCHEME_KEYS: Final[dict[SecretScheme, frozenset[str]]] = {
    SecretScheme.BEARER: frozenset({"token"}),
    SecretScheme.BASIC: frozenset({"username", "password"}),
    SecretScheme.API_KEY_HEADER: frozenset({"header", "value"}),
    SecretScheme.AWS_ACCESS_KEY: frozenset({"access_key_id", "secret_access_key"}),
    SecretScheme.GCP_SERVICE_ACCOUNT: frozenset({"client_email", "private_key"}),
}
_MAX_LENGTHS: Final[dict[str, int]] = {
    "token": MAX_SECRET_VALUE_LENGTH,
    "username": 256,
    "password": 1024,
    "header": 64,
    "value": MAX_SECRET_VALUE_LENGTH,
    "access_key_id": 128,
    "secret_access_key": 256,
    "client_email": 320,
    "private_key": MAX_SECRET_VALUE_LENGTH,
}


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether ``ip`` is in a range an admin-supplied URL must never target."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _blocked_ip(ip.ipv4_mapped)
        if any(ip in net for net in _V6_TUNNELS):
            return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
    )


def _canonical_host(host: str) -> str:
    """Return the canonical host or raise: bracketed v6, canonical dotted-quad v4, or DNS name.

    Anything else (``2130706433``, ``0x7f.0.0.1``, ``127.1``, ``017700000001``) is a spelling
    ``getaddrinfo`` / ``inet_aton`` would resolve to an address the plain ``ipaddress`` parser
    rejects, so it is refused outright rather than guessed at.
    """
    if host.startswith("["):
        try:
            v6 = ipaddress.IPv6Address(host[1:-1])
        except ValueError:  # pragma: no cover - urlsplit already rejected a malformed literal
            raise InvalidConnectionInput("endpoint url has an invalid host") from None
        if _blocked_ip(v6):
            raise InvalidConnectionInput("endpoint url must not target a private or local address")
        return f"[{v6.compressed}]"
    if _DIGITS_AND_DOTS_RE.fullmatch(host):
        try:
            v4 = ipaddress.IPv4Address(host)  # strict: four decimal octets, no leading zeros
        except ValueError:
            raise InvalidConnectionInput("endpoint url has an invalid host") from None
        if _blocked_ip(v4):
            raise InvalidConnectionInput("endpoint url must not target a private or local address")
        return str(v4)
    labels = host.lower().split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or not all(_DNS_LABEL_RE.fullmatch(label) for label in labels)
        or not _TLD_RE.fullmatch(labels[-1])
    ):
        raise InvalidConnectionInput("endpoint url has an invalid host")
    return ".".join(labels)


def validate_endpoint_url(raw: str) -> str:
    """Return ``raw`` if it is an acceptable outbound https endpoint; raise otherwise.

    https only, no userinfo, no fragment, host not an IP literal in a private / loopback /
    link-local / CGNAT range, at most :data:`MAX_URL_LENGTH` characters. Errors never echo the URL.
    DNS-name hosts that *resolve* to a blocked address are the dispatcher's send-time check.
    """
    if not isinstance(raw, str) or not raw:
        raise InvalidConnectionInput("endpoint url is required")
    if len(raw) > MAX_URL_LENGTH:
        raise InvalidConnectionInput("endpoint url is too long")
    if "\\" in raw or not _URL_RE.fullmatch(raw):
        raise InvalidConnectionInput("endpoint url must be a plain https url")
    try:
        parts = urlsplit(raw)  # itself rejects a malformed bracketed IPv6 literal
    except ValueError:
        raise InvalidConnectionInput("endpoint url has an invalid host") from None
    host = parts.hostname
    if not host:  # pragma: no cover - defence in depth: the pattern requires a host
        raise InvalidConnectionInput("endpoint url has no host")
    try:
        port = parts.port
    except ValueError:  # > 65535 raises inside urlsplit
        port = -1
    if port is not None and not 1 <= port <= 65535:
        raise InvalidConnectionInput("endpoint url has an invalid port")
    netloc = _canonical_host(f"[{host}]" if ":" in host else host)
    if port is not None:
        netloc = f"{netloc}:{port}"
    # Re-serialise from the validated components so the validator and the dispatcher read the
    # same host: what is stored is the canonical form, never the caller's spelling.
    return urlunsplit(("https", netloc, parts.path, parts.query, ""))


def validate_aws_region(raw: str) -> str:
    """Return ``raw`` if it is a well-formed AWS region name (``fullmatch``, no stripping)."""
    if not isinstance(raw, str) or len(raw) > 32 or not _AWS_REGION_RE.fullmatch(raw):
        raise InvalidConnectionInput("metadata field 'region' has an invalid format")
    return raw


def reject_control_characters(metadata: Mapping[str, Any]) -> None:
    """Refuse any metadata value carrying a control character (checked before ``strip()``).

    The generic metadata validator trims whitespace; a static target is later embedded in a URL or
    request line, so a stray newline is rejected rather than silently normalised.
    """
    for name, value in metadata.items():
        if isinstance(value, str) and _CONTROL_RE.search(value):
            raise InvalidConnectionInput(f"metadata field {name!r} has an invalid format")


def validate_secret_document(scheme: SecretScheme, doc: Mapping[str, Any]) -> dict[str, str]:
    """Return a clean copy of ``doc`` for ``scheme``: exact keys, string values, bounded lengths.

    Raises :class:`InvalidConnectionInput` naming only the offending *field*, never its value.
    """
    required = _SCHEME_KEYS[scheme]
    if not isinstance(doc, Mapping):
        raise InvalidConnectionInput("secret document must be an object")
    keys = set(doc)
    if any(not isinstance(key, str) for key in keys):
        raise InvalidConnectionInput("secret document keys must be strings")
    unknown = sorted(keys - required)
    if unknown:
        raise InvalidConnectionInput(f"unknown secret field(s): {', '.join(unknown)}")
    missing = sorted(required - keys)
    if missing:
        raise InvalidConnectionInput(f"missing secret field(s): {', '.join(missing)}")
    cleaned: dict[str, str] = {}
    for name in sorted(required):
        value = doc[name]
        if not isinstance(value, str) or not value:
            raise InvalidConnectionInput(f"secret field {name!r} must be a non-empty string")
        if len(value) > _MAX_LENGTHS[name]:
            raise InvalidConnectionInput(f"secret field {name!r} is too long")
        cleaned[name] = value
    if scheme is SecretScheme.API_KEY_HEADER and not _HEADER_NAME_RE.fullmatch(cleaned["header"]):
        raise InvalidConnectionInput("secret field 'header' is not a valid header name")
    if scheme is SecretScheme.BEARER and re.search(r"[\s\x00-\x1f\x7f]", cleaned["token"]):
        raise InvalidConnectionInput("secret field 'token' must not contain whitespace")
    return cleaned


def infer_scheme(doc: Mapping[str, Any]) -> SecretScheme | None:
    """The scheme whose exact key set ``doc`` carries, or ``None`` (structural check helper)."""
    keys = frozenset(doc)
    for scheme, required in _SCHEME_KEYS.items():
        if keys == required:
            return scheme
    return None


class StaticSecretAdapter:
    """Shared behaviour of the three static-secret adapters (concrete ones set ``spec``)."""

    spec: AdapterSpec

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """No canonicalisation: the document was already validated key-by-key."""
        return credentials

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """Structural check only (NO outbound call): the token is a JSON secret document.

        The document must be an object matching one of this provider's allowed schemes exactly.
        """
        try:
            doc = json.loads(credentials.access_token)
        except (ValueError, TypeError):
            raise CredentialsRejected("secret document is not valid JSON") from None
        scheme = infer_scheme(doc) if isinstance(doc, dict) else None
        if scheme is None or scheme not in self.spec.secret_schemes:
            raise CredentialsRejected("secret document does not match an allowed scheme")
        validate_secret_document(scheme, doc)
        return Account(
            account_id=self.spec.provider,
            account_label=self.spec.display_name,
            app_id="",
            scopes=("dispatch",),
            expires_at=None,
        )

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """Validate the target; the row's ``resource_id`` is its ``public_id`` (caller sets it)."""
        return ResolvedResource(resource_id="", facts=self.validate_target(metadata))

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """Static secrets have nothing to refresh."""
        raise RefreshNotConfigured("static-secret connections are not refreshable")

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """A static connection can always ``dispatch`` -- it has no scopes to narrow it."""
        return frozenset({"dispatch"})

    def validate_target(self, metadata: Mapping[str, str]) -> dict[str, Any]:
        """Return the non-secret, normalised target facts (overridden per provider)."""
        raise NotImplementedError("a static adapter must implement validate_target")


def _spec(
    *,
    provider: str,
    display_name: str,
    kind: ConnectionKind,
    metadata_fields: tuple[MetadataField, ...],
    schemes: tuple[SecretScheme, ...],
) -> AdapterSpec:
    keys = frozenset(item.name for item in metadata_fields)
    return AdapterSpec(
        provider=provider,
        display_name=display_name,
        platform=provider,
        resource_type=provider,
        install_kind=INSTALL_KIND_CREDENTIALS,
        capabilities=frozenset({"dispatch"}),
        required_scopes=(),
        credential_fields=(),
        metadata_fields=metadata_fields,
        public_metadata_keys=keys | {"secret_scheme", "capabilities"},
        runtime_metadata_keys=keys | {"secret_scheme"},
        refreshable=False,
        kind=kind,
        secret_schemes=schemes,
    )


class ExternalHttpAdapter(StaticSecretAdapter):
    """An arbitrary https endpoint called with a bearer / basic / api-key-header secret."""

    def __init__(self) -> None:
        """Build the immutable spec."""
        self.spec = _spec(
            provider="external_http",
            display_name="External HTTP endpoint",
            kind=ConnectionKind.EXTERNAL_HTTP,
            metadata_fields=(
                MetadataField("url", True, "https endpoint to call", max_length=MAX_URL_LENGTH),
            ),
            schemes=(SecretScheme.BEARER, SecretScheme.BASIC, SecretScheme.API_KEY_HEADER),
        )

    def validate_target(self, metadata: Mapping[str, str]) -> dict[str, Any]:
        """Validate the URL (https, no userinfo/fragment, no private IP literal)."""
        return {"url": validate_endpoint_url(metadata["url"])}


class AwsLambdaAdapter(StaticSecretAdapter):
    """An AWS Lambda function invoked with an IAM access key."""

    def __init__(self) -> None:
        """Build the immutable spec."""
        self.spec = _spec(
            provider="aws_lambda",
            display_name="AWS Lambda function",
            kind=ConnectionKind.AWS_LAMBDA,
            metadata_fields=(
                MetadataField(
                    "region", True, "AWS region, e.g. us-east-1", _AWS_REGION_RE.pattern, 32
                ),
                MetadataField("function", True, "function name", _AWS_FUNCTION_RE.pattern, 64),
            ),
            schemes=(SecretScheme.AWS_ACCESS_KEY,),
        )

    def validate_target(self, metadata: Mapping[str, str]) -> dict[str, Any]:
        """Region/function were pattern-checked by the framework; return them as facts."""
        return {"region": validate_aws_region(metadata["region"]), "function": metadata["function"]}


class GcpFunctionAdapter(StaticSecretAdapter):
    """A GCP Cloud Function invoked with a service-account key."""

    def __init__(self) -> None:
        """Build the immutable spec."""
        self.spec = _spec(
            provider="gcp_function",
            display_name="GCP Cloud Function",
            kind=ConnectionKind.GCP_FUNCTION,
            metadata_fields=(
                MetadataField("project", True, "GCP project id", _GCP_PROJECT_RE.pattern, 30),
                MetadataField("region", True, "GCP region", _GCP_REGION_RE.pattern, 32),
                MetadataField("function", True, "function name", _GCP_FUNCTION_RE.pattern, 63),
            ),
            schemes=(SecretScheme.GCP_SERVICE_ACCOUNT,),
        )

    def validate_target(self, metadata: Mapping[str, str]) -> dict[str, Any]:
        """Project/region/function were pattern-checked by the framework; return them as facts."""
        return {key: metadata[key] for key in ("project", "region", "function")}
