"""Fake identity providers for the SSO test suite.

These are *real protocol implementations*, not mocks of our own code: the OIDC fake
runs RFC 7636 PKCE verification, redirect-URI and client-authentication checks and
signs real RS256 ID tokens (served via a real JWKS document); the SAML fake builds
and XML-DSig-signs real SAML 2.0 Responses with `signxml`. The only thing replaced
is the *socket*: `FakeOidcIdp.transport` is an `httpx.MockTransport`, so hub-api's
guarded HTTP client, discovery/JWKS caching, token request construction and ID-token
validation all execute for real against it.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from xml.etree.ElementTree import (  # noqa: S405 -- build/serialise only; parsing is defusedxml
    Element,
    register_namespace,
    tostring,
)

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import NameOID
from defusedxml.ElementTree import fromstring as safe_fromstring
from signxml import XMLSigner, methods
from signxml.algorithms import CanonicalizationMethod, DigestAlgorithm, SignatureMethod

NS_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
NS_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS_MD = "urn:oasis:names:tc:SAML:2.0:metadata"

for _prefix, _uri in (("samlp", NS_SAMLP), ("saml", NS_SAML), ("ds", NS_DS), ("md", NS_MD)):
    register_namespace(_prefix, _uri)


class _WeakSigner(XMLSigner):
    """`XMLSigner` that will produce SHA-1 signatures (signxml refuses by default).

    Test-only: lets the downgrade regression test mint a *cryptographically valid* SHA-1
    signature so that only hub-api's algorithm policy can be what rejects it.
    """

    def check_deprecated_methods(self) -> None:
        """Allow SHA-1 -- deliberately."""


_SHARED_KEYS: list[rsa.RSAPrivateKey] = []


def _rsa_key() -> rsa.RSAPrivateKey:
    """A process-wide RSA-2048 key (generation is the slow part of per-test setup).

    Tests that need a *different* key (attacker, rotation) call
    `rsa.generate_private_key` themselves, so sharing this one is safe.
    """
    if not _SHARED_KEYS:
        _SHARED_KEYS.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return _SHARED_KEYS[0]


def make_cert(
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey,
    *,
    cn: str = "idp.test",
    days_valid: int = 3650,
    expired: bool = False,
) -> x509.Certificate:
    """Self-signed X.509 certificate for `key` (optionally already expired)."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(UTC)
    not_before = now - timedelta(days=days_valid + 2 if expired else 1)
    not_after = now - timedelta(days=1) if expired else now + timedelta(days=days_valid)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256())
    )


def pem_cert(cert: x509.Certificate) -> str:
    """PEM text of `cert`."""
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def pem_key(key: Any) -> str:
    """PKCS8 PEM text of a private key."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _IssuedCode:
    nonce: str | None
    challenge: str | None
    redirect_uri: str
    claims: dict[str, Any]


@dataclass(slots=True)
class FakeOidcIdp:
    """A working OIDC provider: discovery, JWKS, authorize (simulated), token."""

    issuer: str = "https://idp.example.com"
    client_id: str = "client-123"
    client_secret: str | None = "s3cret-value"  # noqa: S105
    kid: str = "kid-1"
    key: rsa.RSAPrivateKey = field(default_factory=_rsa_key)
    #: Extra/override claims merged into every ID token (use None to drop a claim).
    claims: dict[str, Any] = field(default_factory=dict)
    #: Override the token endpoint's response wholesale (status, json body).
    token_response_override: tuple[int, dict[str, Any]] | None = None
    #: Sign tokens with this instead of `key` (attacker key) / with this alg.
    signing_key_override: Any = None
    alg: str = "RS256"
    include_kid: bool = True
    advertised_auth_methods: list[str] = field(default_factory=lambda: ["client_secret_basic"])
    discovery_issuer_override: str | None = None
    jwks_keys_override: list[dict[str, Any]] | None = None
    jwks_document_override: Any = None
    omit_endpoints: tuple[str, ...] = ()
    id_token_ttl: int = 300
    iat_offset: int = 0
    requests: list[httpx.Request] = field(default_factory=list)
    codes: dict[str, _IssuedCode] = field(default_factory=dict)
    token_calls: int = 0
    jwks_calls: int = 0
    discovery_calls: int = 0
    extra_keys: list[tuple[str, rsa.RSAPrivateKey]] = field(default_factory=list)

    @property
    def discovery_url(self) -> str:
        """The OIDC discovery document URL."""
        return f"{self.issuer}/.well-known/openid-configuration"

    def public_jwk(self, key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
        """JWK for `key`'s public half."""
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
        return jwk

    def jwks(self) -> dict[str, Any]:
        """The JWKS document."""
        if self.jwks_document_override is not None:
            return self.jwks_document_override  # type: ignore[no-any-return]
        if self.jwks_keys_override is not None:
            return {"keys": self.jwks_keys_override}
        keys = [self.public_jwk(self.key, self.kid)]
        keys += [self.public_jwk(k, kid) for kid, k in self.extra_keys]
        return {"keys": keys}

    @property
    def transport(self) -> httpx.MockTransport:
        """The mock socket: hub-api's HTTP client is pointed at this."""
        return httpx.MockTransport(self.handle)

    # -- simulated user agent ------------------------------------------------

    def authorize(self, authorize_url: str, **claims: Any) -> tuple[str, str]:
        """Play the browser+IdP: validate the authorize request, return `(code, state)`.

        Checks what a real IdP checks (response_type, client_id, PKCE method present,
        nonce present) and remembers nonce/challenge/redirect_uri for the token call.
        """
        params = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        assert params["response_type"] == "code"
        assert params["client_id"] == self.client_id
        assert params["code_challenge_method"] == "S256"
        assert "openid" in params["scope"].split()
        code = secrets.token_urlsafe(16)
        self.codes[code] = _IssuedCode(
            nonce=params.get("nonce"),
            challenge=params.get("code_challenge"),
            redirect_uri=params["redirect_uri"],
            claims=claims,
        )
        return code, params["state"]

    # -- the socket ----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Route a request to discovery / jwks / token."""
        self.requests.append(request)
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            self.discovery_calls += 1
            return self._discovery()
        if path == "/jwks":
            self.jwks_calls += 1
            return httpx.Response(200, json=self.jwks())
        if path == "/token":
            self.token_calls += 1
            return self._token(request)
        return httpx.Response(404, json={"error": "not_found"})

    def _discovery(self) -> httpx.Response:
        doc: dict[str, Any] = {
            "issuer": self.discovery_issuer_override or self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "token_endpoint_auth_methods_supported": self.advertised_auth_methods,
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        for name in self.omit_endpoints:
            doc.pop(name, None)
        return httpx.Response(200, json=doc)

    def _client_credentials(self, request: httpx.Request, form: dict[str, str]) -> bool:
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            decoded = base64.b64decode(header[6:]).decode()
            cid, _, sec = decoded.partition(":")
            return cid == self.client_id and sec == (self.client_secret or "")
        if self.client_secret is None:
            return form.get("client_id") == self.client_id
        return form.get("client_id") == self.client_id and form.get("client_secret") == (
            self.client_secret
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_response_override is not None:
            status, body = self.token_response_override
            return httpx.Response(status, json=body)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if not self._client_credentials(request, form):
            return httpx.Response(401, json={"error": "invalid_client"})
        issued = self.codes.pop(form.get("code", ""), None)
        if issued is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if form.get("redirect_uri") != issued.redirect_uri:
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier = form.get("code_verifier", "")
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        if digest.decode().rstrip("=") != issued.challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200,
            json={
                "access_token": "at-" + secrets.token_hex(8),
                "token_type": "Bearer",
                "id_token": self.sign_id_token(issued),
            },
        )

    def sign_id_token(self, issued: _IssuedCode) -> str:
        """Mint the ID token for an issued code, applying every configured knob."""
        now = int(time.time()) + self.iat_offset
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": "idp-subject-1",
            "iat": now,
            "exp": now + self.id_token_ttl,
            "nonce": issued.nonce,
            "email": "alice@acme.test",
            "email_verified": True,
            "name": "Alice Example",
        }
        claims.update(self.claims)
        claims.update(issued.claims)
        claims = {k: v for k, v in claims.items() if v is not None}
        headers: dict[str, str] = {"kid": self.kid} if self.include_kid else {}
        key = self.signing_key_override or self.key
        if self.alg.startswith("HS"):
            return jwt.encode(claims, b"shared-secret-of-sufficient-length-x", self.alg, headers)
        if self.alg == "none":
            return jwt.encode(claims, None, "none", headers)
        return jwt.encode(claims, key, self.alg, headers)


# ---------------------------------------------------------------------------
# SAML
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ParsedAuthnRequest:
    """What the IdP learned from our redirect."""

    request_id: str
    acs_url: str
    issuer: str
    destination: str
    relay_state: str
    name_id_policy_format: str | None
    force_authn: bool


@dataclass(slots=True)
class FakeSamlIdp:
    """A SAML IdP that can emit well-formed and deliberately malformed signed responses."""

    entity_id: str = "https://saml-idp.example.com/metadata"
    sso_url: str = "https://saml-idp.example.com/sso"
    key: Any = field(default_factory=_rsa_key)
    cert: x509.Certificate | None = None

    def __post_init__(self) -> None:
        """Mint the IdP's signing certificate."""
        if self.cert is None:
            self.cert = make_cert(self.key)

    @property
    def cert_pem(self) -> str:
        """PEM of the IdP signing certificate."""
        assert self.cert is not None
        return pem_cert(self.cert)

    def metadata_xml(self) -> str:
        """IdP metadata an admin would paste into hub-api."""
        cert_b64 = "".join(self.cert_pem.splitlines()[1:-1])
        return (
            '<?xml version="1.0"?>'
            '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            f'xmlns:ds="{NS_DS}" entityID="{self.entity_id}">'
            '<md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            '<md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data>'
            f"<ds:X509Certificate>{cert_b64}</ds:X509Certificate>"
            "</ds:X509Data></ds:KeyInfo></md:KeyDescriptor>"
            '<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location="{self.sso_url}/post"/>'
            '<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
            f'Location="{self.sso_url}"/>'
            "</md:IDPSSODescriptor></md:EntityDescriptor>"
        )

    def parse_authn_request(self, redirect_url: str) -> ParsedAuthnRequest:
        """Decode the SP's HTTP-Redirect AuthnRequest exactly as a real IdP would."""
        parsed = urlparse(redirect_url)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == self.sso_url
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        xml = zlib.decompress(base64.b64decode(params["SAMLRequest"]), -15)
        root = safe_fromstring(xml)
        assert root.tag == f"{{{NS_SAMLP}}}AuthnRequest"
        assert root.get("Version") == "2.0"
        policy = root.find(f"{{{NS_SAMLP}}}NameIDPolicy")
        return ParsedAuthnRequest(
            request_id=root.get("ID", ""),
            acs_url=root.get("AssertionConsumerServiceURL", ""),
            issuer=(root.find(f"{{{NS_SAML}}}Issuer").text or ""),
            destination=root.get("Destination", ""),
            relay_state=params["RelayState"],
            name_id_policy_format=policy.get("Format") if policy is not None else None,
            force_authn=root.get("ForceAuthn") == "true",
        )

    def build_response(
        self,
        authn: ParsedAuthnRequest,
        *,
        name_id: str = "alice@acme.test",
        name_id_format: str = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        attributes: dict[str, list[str]] | None = None,
        sign: str = "response",
        issuer: str | None = None,
        audience: str | None = None,
        recipient: str | None = None,
        destination: str | None = None,
        in_response_to: str | None = None,
        status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
        not_before_offset: int = -30,
        not_on_or_after_offset: int = 300,
        conf_not_on_or_after_offset: int = 300,
        authn_statement: bool = True,
        assertion_id: str | None = None,
        signing_key: Any = None,
        signing_cert: x509.Certificate | None = None,
        signature_algorithm: SignatureMethod = SignatureMethod.RSA_SHA256,
        digest_algorithm: DigestAlgorithm = DigestAlgorithm.SHA256,
        include_conditions: bool = True,
        extra_condition_xml: str = "",
        name_id_inner_xml: str | None = None,
        bearer: bool = True,
        omit_destination: bool = False,
        response_issuer: str | None = None,
        session_not_on_or_after_offset: int | None = None,
        extra_audience_restriction_xml: str = "",
        omit_name_id: bool = False,
        assertion_version: str = "2.0",
        omit_assertion_id: bool = False,
        omit_subject: bool = False,
        assertion_issuer: str | None = None,
        extra_confirmation_xml: str = "",
        bearer_without_data: bool = False,
        conf_in_response_to: str | None = None,
        conf_not_before_offset: int | None = None,
        omit_audience: bool = False,
        not_on_or_after_raw: str | None = None,
        omit_not_on_or_after: bool = False,
        allow_weak_algorithms: bool = False,
    ) -> bytes:
        """Return the raw XML bytes of a (by default valid) signed SAML Response."""
        now = datetime.now(UTC)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        a_id = assertion_id or "_a" + secrets.token_hex(12)
        r_id = "_r" + secrets.token_hex(12)
        attrs = attributes if attributes is not None else {"displayName": ["Alice Example"]}
        attr_xml = "".join(
            f'<saml:Attribute Name="{n}"><saml:AttributeValue>{v}</saml:AttributeValue>'
            + "".join(f"<saml:AttributeValue>{x}</saml:AttributeValue>" for x in vals[1:])
            + "</saml:Attribute>"
            for n, vals in attrs.items()
            for v in vals[:1]
        )
        conf_nb = (
            f' NotBefore="{(now + timedelta(seconds=conf_not_before_offset)).strftime(fmt)}"'
            if conf_not_before_offset is not None
            else ""
        )
        conf = (
            extra_confirmation_xml
            + '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
            + (
                ""
                if bearer_without_data
                else (
                    f'<saml:SubjectConfirmationData Recipient="{recipient or authn.acs_url}" '
                    f'InResponseTo="{conf_in_response_to or in_response_to or authn.request_id}" '
                    f'NotOnOrAfter="{(now + timedelta(seconds=conf_not_on_or_after_offset)).strftime(fmt)}"'
                    f"{conf_nb}/>"
                )
            )
            + "</saml:SubjectConfirmation>"
            if bearer
            else extra_confirmation_xml
        )
        cond_end = (
            ""
            if omit_not_on_or_after
            else (
                f' NotOnOrAfter="{not_on_or_after_raw}"'
                if not_on_or_after_raw is not None
                else f' NotOnOrAfter="{(now + timedelta(seconds=not_on_or_after_offset)).strftime(fmt)}"'
            )
        )
        audience_xml = (
            ""
            if omit_audience
            else (
                "<saml:AudienceRestriction><saml:Audience>"
                f"{audience or self._sp_entity(authn)}</saml:Audience></saml:AudienceRestriction>"
            )
        )
        conditions = (
            f'<saml:Conditions NotBefore="{(now + timedelta(seconds=not_before_offset)).strftime(fmt)}"'
            f"{cond_end}>"
            f"{audience_xml}"
            f"{extra_audience_restriction_xml}{extra_condition_xml}</saml:Conditions>"
            if include_conditions
            else ""
        )
        session_attr = (
            f' SessionNotOnOrAfter="{(now + timedelta(seconds=session_not_on_or_after_offset)).strftime(fmt)}"'
            if session_not_on_or_after_offset is not None
            else ""
        )
        authn_xml = (
            f'<saml:AuthnStatement AuthnInstant="{now.strftime(fmt)}"{session_attr}>'
            "<saml:AuthnContext><saml:AuthnContextClassRef>"
            "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
            "</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
            if authn_statement
            else ""
        )
        sig_a = (
            f'<ds:Signature xmlns:ds="{NS_DS}" Id="placeholder"/>'
            if sign in ("assertion", "both")
            else ""
        )
        sig_r = (
            f'<ds:Signature xmlns:ds="{NS_DS}" Id="placeholder"/>'
            if sign in ("response", "both")
            else ""
        )
        name_id_body = name_id_inner_xml if name_id_inner_xml is not None else name_id
        dest_attr = "" if omit_destination else f'Destination="{destination or authn.acs_url}" '
        name_id_xml = (
            ""
            if omit_name_id
            else f'<saml:NameID Format="{name_id_format}">{name_id_body}</saml:NameID>'
        )
        subject_xml = "" if omit_subject else f"<saml:Subject>{name_id_xml}{conf}</saml:Subject>"
        a_id_attr = "" if omit_assertion_id else f'ID="{a_id}" '
        assertion_xml = (
            f'<saml:Assertion xmlns:saml="{NS_SAML}" xmlns:ds="{NS_DS}" {a_id_attr}'
            f'Version="{assertion_version}" IssueInstant="{now.strftime(fmt)}">'
            f"<saml:Issuer>{assertion_issuer or issuer or self.entity_id}</saml:Issuer>"
            f"{sig_a}"
            f"{subject_xml}"
            f"{conditions}{authn_xml}"
            f"<saml:AttributeStatement>{attr_xml}</saml:AttributeStatement>"
            "</saml:Assertion>"
        )

        def response_with(assertion: str) -> str:
            return (
                f'<samlp:Response xmlns:samlp="{NS_SAMLP}" xmlns:saml="{NS_SAML}" ID="{r_id}" '
                f'Version="2.0" IssueInstant="{now.strftime(fmt)}" '
                f"{dest_attr}"
                f'InResponseTo="{in_response_to or authn.request_id}">'
                f"<saml:Issuer>{response_issuer or issuer or self.entity_id}</saml:Issuer>"
                f"{sig_r}"
                f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>'
                f"{assertion}</samlp:Response>"
            )

        if sign == "none":
            return response_with(assertion_xml).encode()

        key = signing_key or self.key
        cert = signing_cert or self.cert
        assert cert is not None
        signer = (_WeakSigner if allow_weak_algorithms else XMLSigner)(
            method=methods.enveloped,
            signature_algorithm=signature_algorithm,
            digest_algorithm=digest_algorithm,
            c14n_algorithm=CanonicalizationMethod.EXCLUSIVE_XML_CANONICALIZATION_1_0,
        )
        assertion_part = assertion_xml
        if sign in ("assertion", "both"):
            # signxml's enveloped signing needs an element (it converts a stdlib Element itself) and
            # hands back its own (lxml) element, which the signer serialises -- no lxml import here.
            signed_assertion = signer.sign(
                safe_fromstring(assertion_xml),
                key=pem_key(key),
                cert=pem_cert(cert),
                reference_uri=a_id,
            )
            assertion_part = signer._tostring(signed_assertion).decode("utf-8")
        response_xml = response_with(assertion_part)
        if sign in ("response", "both"):
            signed_response = signer.sign(
                safe_fromstring(response_xml),
                key=pem_key(key),
                cert=pem_cert(cert),
                reference_uri=r_id,
            )
            return bytes(signer._tostring(signed_response))
        return response_xml.encode()

    @staticmethod
    def _sp_entity(authn: ParsedAuthnRequest) -> str:
        return authn.issuer

    @staticmethod
    def encode(raw: bytes) -> str:
        """Base64 as posted in the `SAMLResponse` form field."""
        return base64.b64encode(raw).decode("ascii")


def post_form(url: str, **fields: str) -> str:
    """URL-encoded form body (test helper)."""
    return urlencode(fields)


def tamper(raw: bytes, mutate: Callable[[Element], None]) -> bytes:
    """Parse `raw`, apply `mutate` to the tree, re-serialise (breaking any signature)."""
    root = safe_fromstring(raw)
    mutate(root)
    return tostring(root, encoding="utf-8")


def parent_of(root: Element, child: Element) -> Element:
    """Return `child`'s parent within `root` (stdlib elements have no `getparent`)."""
    for candidate in root.iter():
        if child in list(candidate):
            return candidate
    raise LookupError("element is not under root")


def remove_signatures(root: Element) -> None:
    """Strip every `ds:Signature` from the tree."""
    for signature in list(root.iter(f"{{{NS_DS}}}Signature")):
        parent_of(root, signature).remove(signature)


def deepcopy_element(element: Element) -> Element:
    """Deep copy helper for XSW constructions."""
    return copy.deepcopy(element)
