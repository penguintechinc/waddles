"""OIDC / Google authorization-code + PKCE login, with full ID-token validation.

One implementation serves both generic OIDC connections (Enterprise) and Google
(Professional): Google is simply OIDC against a pinned issuer, plus the
hosted-domain (`hd`) check. The flow is the standard Authorization Code flow
with PKCE (RFC 7636, S256) and a per-flow `nonce`; the ID token is validated
locally against the IdP's JWKS -- the `userinfo` endpoint is never trusted for
identity.

ID-token checks (all mandatory, any failure is an `SsoProtocolError`):

* signature, with the algorithm drawn from a fixed asymmetric allow-list --
  `none` and every HMAC algorithm are rejected, so an attacker cannot forge a
  token by exploiting the client secret as a symmetric key;
* the signing key chosen by `kid` from the IdP's JWKS (re-fetched once on an
  unknown `kid` to ride out key rotation, rate-limited);
* `iss` equals the pinned issuer (discovery metadata's `issuer` must also equal
  it -- the OIDC mix-up defence), `aud` contains our `client_id`, and when
  there are several audiences `azp` equals our `client_id`;
* `exp`/`iat`/`nbf` with a small configurable clock skew;
* `nonce` equals the value bound to this browser's flow;
* a non-empty `sub`.

The discovery document and JWKS are cached per process for a short TTL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import jwt
from flask_core.jwt_hardening import (
    OUTCOME_OK,
    REASON_FORBIDDEN_HEADER,
    REASON_INVALID,
    REASON_MALFORMED,
    REASON_UNKNOWN_KID,
    VERIFIER_OIDC_ID_TOKEN,
    JwtRejection,
    classify_decode_error,
    inspect_header,
    log_rejection,
    record_verification,
)
from jwt import PyJWK

from services.sso_http import SsoHttp
from services.sso_settings import GOOGLE_ISSUER, SsoSettings
from services.sso_telemetry import sso_span
from services.sso_types import (
    PROTOCOL_GOOGLE,
    ExternalIdentity,
    OidcSettings,
    SsoConfigError,
    SsoProtocolError,
)

#: Asymmetric algorithms accepted for ID-token signatures. HS* / none are never
#: accepted, whatever the token header claims.
ALLOWED_ID_TOKEN_ALGS: Final[tuple[str, ...]] = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)

#: Google documents both spellings as valid `iss` values.
GOOGLE_ISSUERS: Final[tuple[str, ...]] = (GOOGLE_ISSUER, "accounts.google.com")

_DISCOVERY_TTL_S: Final = 300
_JWKS_TTL_S: Final = 600
_JWKS_MIN_REFETCH_S: Final = 30
_MAX_SUBJECT_LEN: Final = 255
_MAX_NAME_LEN: Final = 255


@dataclass(slots=True, frozen=True)
class ProviderMetadata:
    """The subset of an OIDC discovery document this flow uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    token_auth_methods: tuple[str, ...] = ("client_secret_basic",)


@dataclass(slots=True)
class _CacheEntry:
    fetched_at: float
    value: Any = field(repr=False)


_METADATA_CACHE: dict[str, _CacheEntry] = {}
_JWKS_CACHE: dict[str, _CacheEntry] = {}


def clear_caches() -> None:
    """Drop the process-wide discovery/JWKS caches (tests; operator-triggered refresh)."""
    _METADATA_CACHE.clear()
    _JWKS_CACHE.clear()


def new_pkce_pair() -> tuple[str, str]:
    """Return `(code_verifier, code_challenge)` per RFC 7636 (S256)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_nonce() -> str:
    """Return a fresh unguessable OIDC nonce."""
    return secrets.token_urlsafe(32)


def _require_https(url: str, field_name: str, settings: SsoSettings) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    approved = host in settings.allowed_private_hosts
    if parsed.scheme != "https" and not (parsed.scheme == "http" and approved):
        raise SsoProtocolError("oidc_metadata_insecure", f"discovery {field_name} must be https")
    if not host or parsed.username or parsed.password:
        raise SsoProtocolError("oidc_metadata_invalid", f"discovery {field_name} is malformed")


class OidcClient:
    """Stateless-per-request OIDC relying party; caches are module-level and short-lived."""

    def __init__(
        self,
        http: SsoHttp,
        settings: SsoSettings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the guarded HTTP client, operator settings and an injectable clock."""
        self._http = http
        self._settings = settings
        self._clock = clock

    async def metadata(self, oidc: OidcSettings) -> ProviderMetadata:
        """Fetch (or return cached) discovery metadata, validating issuer and endpoints."""
        cached = _METADATA_CACHE.get(oidc.discovery_url)
        now = self._clock()
        if cached is not None and now - cached.fetched_at < _DISCOVERY_TTL_S:
            meta: ProviderMetadata = cached.value
            return meta

        with sso_span("sso.oidc.discovery", **{"sso.protocol": "oidc"}):
            doc = await self._http.get_json(oidc.discovery_url, operation="discovery")
        if not isinstance(doc, dict):
            raise SsoProtocolError("oidc_metadata_invalid", "discovery document is not an object")

        issuer = doc.get("issuer")
        if issuer != oidc.issuer:
            # OIDC Discovery 4.3 / mix-up defence: the document must describe
            # the issuer we configured, byte for byte.
            raise SsoProtocolError(
                "oidc_issuer_mismatch", "discovery issuer does not match the configured issuer"
            )
        endpoints: dict[str, str] = {}
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = doc.get(key)
            if not isinstance(value, str) or not value:
                raise SsoProtocolError("oidc_metadata_invalid", f"discovery is missing {key}")
            _require_https(value, key, self._settings)
            endpoints[key] = value

        methods_raw = doc.get("token_endpoint_auth_methods_supported")
        methods: tuple[str, ...] = ("client_secret_basic",)
        if isinstance(methods_raw, list) and all(isinstance(m, str) for m in methods_raw):
            methods = tuple(methods_raw)

        meta = ProviderMetadata(
            issuer=oidc.issuer,
            authorization_endpoint=endpoints["authorization_endpoint"],
            token_endpoint=endpoints["token_endpoint"],
            jwks_uri=endpoints["jwks_uri"],
            token_auth_methods=methods,
        )
        _METADATA_CACHE[oidc.discovery_url] = _CacheEntry(now, meta)
        return meta

    async def authorization_url(
        self,
        oidc: OidcSettings,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        """Build the IdP authorize URL the browser is redirected to (PKCE S256 + nonce)."""
        meta = await self.metadata(oidc)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(oidc.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if oidc.hosted_domain:
            params["hd"] = oidc.hosted_domain
        parsed = urlparse(meta.authorization_endpoint)
        merged = parse_qsl(parsed.query, keep_blank_values=True) + list(params.items())
        return urlunparse(parsed._replace(query=urlencode(merged)))

    async def complete(
        self,
        oidc: OidcSettings,
        *,
        protocol: str,
        client_id: str,
        client_secret: str | None,
        redirect_uri: str,
        code: str,
        code_verifier: str,
        expected_nonce: str,
    ) -> ExternalIdentity:
        """Exchange `code`, validate the ID token, and return the vouched identity."""
        meta = await self.metadata(oidc)

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        basic: tuple[str, str] | None = None
        if client_secret:
            if "client_secret_basic" in meta.token_auth_methods or (
                "client_secret_post" not in meta.token_auth_methods
            ):
                basic = (client_id, client_secret)
            else:
                form["client_id"] = client_id
                form["client_secret"] = client_secret
        else:
            form["client_id"] = client_id  # public client: PKCE is the proof of possession

        with sso_span("sso.oidc.token_exchange", **{"sso.protocol": protocol}):
            token_response = await self._http.post_form(
                meta.token_endpoint, form, operation="token", basic_auth=basic
            )
        if not isinstance(token_response, dict):
            raise SsoProtocolError("oidc_token_invalid", "token response is not an object")
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise SsoProtocolError("oidc_no_id_token", "token response carried no id_token")

        claims = await self._validate_id_token(
            id_token,
            meta=meta,
            oidc=oidc,
            protocol=protocol,
            client_id=client_id,
            expected_nonce=expected_nonce,
        )
        return _identity_from_claims(claims)

    async def _signing_key(self, meta: ProviderMetadata, kid: str | None) -> PyJWK:
        now = self._clock()
        entry = _JWKS_CACHE.get(meta.jwks_uri)
        stale = entry is None or now - entry.fetched_at >= _JWKS_TTL_S
        if entry is not None and not stale:
            found = _pick_key(entry.value, kid)
            if found is not None:
                return found
            if now - entry.fetched_at < _JWKS_MIN_REFETCH_S:
                raise SsoProtocolError("oidc_unknown_kid", "ID token signed by an unknown key")

        with sso_span("sso.oidc.jwks", **{"sso.protocol": "oidc"}):
            doc = await self._http.get_json(meta.jwks_uri, operation="jwks")
        keys = _parse_jwks(doc)
        _JWKS_CACHE[meta.jwks_uri] = _CacheEntry(now, keys)
        found = _pick_key(keys, kid)
        if found is None:
            raise SsoProtocolError("oidc_unknown_kid", "ID token signed by an unknown key")
        return found

    async def _validate_id_token(
        self,
        id_token: str,
        *,
        meta: ProviderMetadata,
        oidc: OidcSettings,
        protocol: str,
        client_id: str,
        expected_nonce: str,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            # H-2 Phase 0 header vetting: `alg` from the fixed asymmetric allow-list,
            # `none` (any case) and key-material params (jku/jwk/x5u/x5c/crit) refused
            # before any key is fetched or any signature checked. `kid` stays
            # unvalidated here -- IdPs use arbitrary strings -- and only ever selects a
            # key from the IdP's own JWKS.
            header = inspect_header(id_token, allowed_algs=ALLOWED_ID_TOKEN_ALGS)
        except JwtRejection as rejection:
            record_verification(
                verifier=VERIFIER_OIDC_ID_TOKEN,
                alg=rejection.alg,
                outcome=rejection.reason,
                started=started,
            )
            log_rejection(
                verifier=VERIFIER_OIDC_ID_TOKEN, reason=rejection.reason, alg=rejection.alg
            )
            if rejection.reason == REASON_MALFORMED:
                raise SsoProtocolError(
                    "oidc_id_token_malformed", "ID token is malformed"
                ) from rejection
            if rejection.reason == REASON_FORBIDDEN_HEADER:
                raise SsoProtocolError(
                    "oidc_header_rejected", "ID token header carries a forbidden parameter"
                ) from rejection
            raise SsoProtocolError(
                "oidc_alg_rejected", "ID token algorithm is not allowed"
            ) from rejection
        alg = header.alg
        try:
            key = await self._signing_key(meta, header.kid)
        except SsoProtocolError as exc:
            record_verification(
                verifier=VERIFIER_OIDC_ID_TOKEN,
                alg=alg,
                outcome=REASON_UNKNOWN_KID if exc.code == "oidc_unknown_kid" else REASON_INVALID,
                started=started,
            )
            raise

        issuers = GOOGLE_ISSUERS if protocol == PROTOCOL_GOOGLE else (oidc.issuer,)
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key=key.key,
                algorithms=[alg],
                audience=client_id,
                issuer=list(issuers),
                leeway=self._settings.clock_skew_s,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            reason = classify_decode_error(exc)
            record_verification(
                verifier=VERIFIER_OIDC_ID_TOKEN, alg=alg, outcome=reason, started=started
            )
            log_rejection(verifier=VERIFIER_OIDC_ID_TOKEN, reason=reason, alg=alg)
            # PyJWT messages can echo claim values; withhold them (type is logged).
            raise SsoProtocolError(
                "oidc_id_token_invalid", f"ID token failed validation ({type(exc).__name__})"
            ) from exc
        record_verification(
            verifier=VERIFIER_OIDC_ID_TOKEN, alg=alg, outcome=OUTCOME_OK, started=started
        )

        audience = claims.get("aud")
        if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != client_id:
            raise SsoProtocolError("oidc_azp_mismatch", "multi-audience ID token has wrong azp")
        if claims.get("azp") is not None and claims.get("azp") != client_id:
            raise SsoProtocolError("oidc_azp_mismatch", "ID token azp does not match client")

        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
            raise SsoProtocolError("oidc_nonce_mismatch", "ID token nonce does not match the flow")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > _MAX_SUBJECT_LEN:
            raise SsoProtocolError("oidc_bad_subject", "ID token subject is missing or oversized")
        return claims


def _parse_jwks(doc: Any) -> list[PyJWK]:
    if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
        raise SsoProtocolError("oidc_jwks_invalid", "JWKS document has no key list")
    keys: list[PyJWK] = []
    for raw in doc["keys"]:
        if not isinstance(raw, dict) or raw.get("kty") not in ("RSA", "EC", "OKP"):
            continue  # symmetric ('oct') and unknown key types are never usable
        if raw.get("use") not in (None, "sig"):
            continue
        try:
            keys.append(PyJWK.from_dict(raw))
        except jwt.PyJWTError:
            continue
    if not keys:
        raise SsoProtocolError("oidc_jwks_invalid", "JWKS contains no usable signing keys")
    return keys


def _pick_key(keys: list[PyJWK], kid: str | None) -> PyJWK | None:
    if kid is not None:
        for key in keys:
            if key.key_id == kid:
                return key
        return None
    # No `kid` in the header: only unambiguous when the set has exactly one key.
    return keys[0] if len(keys) == 1 else None


def _truthy(value: Any) -> bool:
    """`email_verified` is a boolean per spec but some IdPs send the string "true"."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def _identity_from_claims(claims: dict[str, Any]) -> ExternalIdentity:
    email_raw = claims.get("email")
    email = email_raw.strip().lower() if isinstance(email_raw, str) and email_raw.strip() else None
    name_raw = claims.get("name")
    name = (
        name_raw.strip()[:_MAX_NAME_LEN] if isinstance(name_raw, str) and name_raw.strip() else None
    )
    hd_raw = claims.get("hd")
    hosted = hd_raw.strip().lower() if isinstance(hd_raw, str) and hd_raw.strip() else None
    return ExternalIdentity(
        subject=str(claims["sub"]),
        email=email,
        email_verified=_truthy(claims.get("email_verified")),
        display_name=name,
        hosted_domain=hosted,
    )


def validate_oidc_settings(oidc: OidcSettings) -> None:
    """Static sanity checks on parsed OIDC settings (shape only -- no network)."""
    if not oidc.issuer:
        raise SsoConfigError("oidc_settings_invalid", "issuer is required")
    if not oidc.client_id and not oidc.use_platform_client:
        raise SsoConfigError("oidc_settings_invalid", "client_id is required")
    if "openid" not in oidc.scopes:
        raise SsoConfigError("oidc_settings_invalid", "the 'openid' scope is required")


__all__ = [
    "ALLOWED_ID_TOKEN_ALGS",
    "OidcClient",
    "ProviderMetadata",
    "clear_caches",
    "new_nonce",
    "new_pkce_pair",
    "validate_oidc_settings",
]
