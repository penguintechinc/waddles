"""SAML 2.0 Web Browser SSO service provider (SP-initiated, HTTP-Redirect -> HTTP-POST).

Scope: hub-api acts purely as the SP. It builds an `AuthnRequest` (HTTP-Redirect
binding), publishes SP metadata, and validates the IdP's `Response` posted to the
Assertion Consumer Service (HTTP-POST binding). Signing the `AuthnRequest`,
IdP-initiated (unsolicited) responses, encrypted assertions, SLO and artifact
binding are deliberately NOT supported and fail loudly rather than degrade
silently -- see `docs/SSO.md` "Known limitations".

Response validation is the security-critical part, and is written to defeat the
classic SAML attack families:

* **Signature wrapping (XSW).** Trust decisions are made ONLY on the XML element
  `signxml` returns as the *signed* element (the "see what is signed" rule) --
  never on the document as posted. The signature must sit at an exact location
  (directly under the Response, or directly under a direct-child Assertion), the
  signed element must have the expected tag, and a Response carrying more than
  one `Assertion` anywhere (siblings, or nested/wrapped) is rejected outright.
* **Unsigned / weakly signed responses.** A valid signature from one of the
  tenant's pinned IdP certificates is mandatory (the cert in the message's own
  `KeyInfo` is never trusted); only RSA/ECDSA with SHA-2 are accepted -- SHA-1,
  DSA and HMAC are not.
* **XML attacks.** Documents containing a DOCTYPE/ENTITY declaration are
  rejected before parsing; the parser never resolves entities, loads DTDs or
  touches the network; comments and processing instructions are stripped, and
  any that survive inside a `NameID`/attribute value cause rejection (the
  "comment truncation" identity-confusion attack).
* **Replay / confusion.** `InResponseTo` must equal the ID of the AuthnRequest
  bound to this browser's flow (the state is single-use), the assertion ID is
  recorded once, `Recipient`/`Destination` must equal our ACS URL, and the
  `Audience` must contain our SP entity ID.
* **Time.** `NotBefore`/`NotOnOrAfter` on Conditions and on the bearer
  SubjectConfirmationData are enforced with a small, configurable skew.
* **Identifier stability.** A `transient` NameID cannot identify a returning user
  and is refused instead of silently minting a new account per login.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import secrets
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import urlencode, urlparse

from cryptography import x509
from cryptography.exceptions import InvalidSignature as CryptoInvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from lxml import etree
from signxml.algorithms import DigestAlgorithm, SignatureMethod
from signxml.exceptions import SignXMLException
from signxml.verifier import SignatureConfiguration, XMLVerifier

from services.sso_telemetry import saml_validation_counter, sso_span
from services.sso_types import (
    ExternalIdentity,
    SamlSettings,
    SsoConfigError,
    SsoProtocolError,
)

# signxml's `processor` logs the canonicalised XML it signs/verifies -- i.e. the entire
# assertion, NameID and attributes included -- at DEBUG. With the platform's "over-log at
# DEBUG when switched on" policy that would write user identity data to the log stream, so the
# library's logger is pinned to INFO regardless of the root level (regression test:
# `tests/sso/test_sso_regression.py::TestLogHygiene`).
logging.getLogger("signxml").setLevel(logging.INFO)

NS_SAMLP: Final = "urn:oasis:names:tc:SAML:2.0:protocol"
NS_SAML: Final = "urn:oasis:names:tc:SAML:2.0:assertion"
NS_DS: Final = "http://www.w3.org/2000/09/xmldsig#"
NS_MD: Final = "urn:oasis:names:tc:SAML:2.0:metadata"

BINDING_REDIRECT: Final = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
BINDING_POST: Final = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
STATUS_SUCCESS: Final = "urn:oasis:names:tc:SAML:2.0:status:Success"
CM_BEARER: Final = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
NAMEID_EMAIL: Final = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
NAMEID_PERSISTENT: Final = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"
NAMEID_TRANSIENT: Final = "urn:oasis:names:tc:SAML:2.0:nameid-format:transient"
NAMEID_UNSPECIFIED: Final = "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"

#: NameID formats an admin may require of the IdP.
ALLOWED_NAMEID_FORMATS: Final[frozenset[str]] = frozenset(
    {NAMEID_EMAIL, NAMEID_PERSISTENT, NAMEID_UNSPECIFIED}
)

#: Hard cap on the base64 `SAMLResponse` form field we will decode (bytes of base64 text).
MAX_RESPONSE_B64_BYTES: Final = 2_000_000

_ALLOWED_SIGNATURE_METHODS: Final = frozenset(
    {
        SignatureMethod.RSA_SHA256,
        SignatureMethod.RSA_SHA384,
        SignatureMethod.RSA_SHA512,
        SignatureMethod.ECDSA_SHA256,
        SignatureMethod.ECDSA_SHA384,
        SignatureMethod.ECDSA_SHA512,
    }
)
_ALLOWED_DIGESTS: Final = frozenset(
    {DigestAlgorithm.SHA256, DigestAlgorithm.SHA384, DigestAlgorithm.SHA512}
)

#: Attribute names tried (in order) for the user's email when none is configured.
_DEFAULT_EMAIL_ATTRIBUTES: Final[tuple[str, ...]] = (
    "email",
    "mail",
    "emailaddress",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "urn:oid:0.9.2342.19200300.100.1.3",
)
_DEFAULT_NAME_ATTRIBUTES: Final[tuple[str, ...]] = (
    "displayname",
    "name",
    "cn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "urn:oid:2.16.840.1.113730.3.1.241",
)
_ALLOWED_CONDITIONS: Final = frozenset(
    {
        f"{{{NS_SAML}}}AudienceRestriction",
        f"{{{NS_SAML}}}OneTimeUse",
        f"{{{NS_SAML}}}ProxyRestriction",
    }
)
_MAX_SUBJECT_LEN: Final = 512
_MAX_NAME_LEN: Final = 255
_DOCTYPE_RE: Final = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class IdpMetadata:
    """The IdP facts extracted from a pasted metadata document."""

    entity_id: str
    sso_url: str
    certs_pem: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class ValidatedAssertion:
    """A fully validated assertion: the vouched identity plus replay-cache bookkeeping."""

    identity: ExternalIdentity
    assertion_id: str
    not_on_or_after: datetime


def _fail(code: str, message: str) -> SsoProtocolError:
    saml_validation_counter.add(1, {"code": code})
    return SsoProtocolError(code, message)


def new_request_id() -> str:
    """Return a fresh SAML ID (an NCName: must not start with a digit)."""
    return "_" + secrets.token_hex(20)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_instant(value: str | None, code: str) -> datetime:
    if not value:
        raise _fail(code, "SAML timestamp is missing")
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _fail(code, "SAML timestamp is malformed") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def secure_parser() -> etree.XMLParser:
    """An lxml parser that never resolves entities, loads DTDs or touches the network."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
        collect_ids=False,
    )


def parse_xml(data: bytes, *, what: str) -> etree._Element:
    """Parse untrusted XML safely; reject DOCTYPE/ENTITY declarations outright."""
    if _DOCTYPE_RE.search(data):
        raise _fail("saml_doctype_rejected", f"{what} contains a DOCTYPE/ENTITY declaration")
    try:
        # S320: false positive -- `secure_parser()` disables entity resolution, DTD loading,
        # network access and huge trees, and DOCTYPE/ENTITY was rejected above.
        return etree.fromstring(data, secure_parser())  # noqa: S320
    except etree.XMLSyntaxError as exc:
        raise _fail("saml_xml_malformed", f"{what} is not well-formed XML") from exc


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _text_of(element: etree._Element, *, what: str) -> str:
    """Return `element`'s text, refusing mixed content / comments (identity confusion)."""
    if len(element) != 0:
        raise _fail("saml_value_has_children", f"{what} must be a plain text value")
    return (element.text or "").strip()


def _cert_from_der_b64(b64: str) -> x509.Certificate:
    compact = "".join(b64.split())
    try:
        return x509.load_der_x509_certificate(base64.b64decode(compact, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise SsoConfigError(
            "saml_cert_invalid", "IdP certificate is not a valid X.509 cert"
        ) from exc


def normalize_certificate(pem_or_b64: str) -> str:
    """Validate an IdP signing certificate and return it as canonical PEM.

    Accepts PEM or bare base64 DER (as found in metadata). Rejects expired
    certificates and weak keys (RSA < 2048 bits, non-P-256+ EC) at save time so
    a misconfiguration surfaces to the admin, not to every user at login.
    """
    text = pem_or_b64.strip()
    if "BEGIN CERTIFICATE" in text:
        try:
            cert = x509.load_pem_x509_certificate(text.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SsoConfigError(
                "saml_cert_invalid", "IdP certificate is not a valid PEM X.509 cert"
            ) from exc
    else:
        cert = _cert_from_der_b64(text)

    if cert.not_valid_after_utc < datetime.now(UTC):
        raise SsoConfigError("saml_cert_expired", "IdP signing certificate has expired")
    public_key = cert.public_key()
    if isinstance(public_key, rsa.RSAPublicKey):
        if public_key.key_size < 2048:
            raise SsoConfigError("saml_cert_weak", "IdP RSA key must be at least 2048 bits")
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        if public_key.curve.key_size < 256:
            raise SsoConfigError("saml_cert_weak", "IdP EC key must be P-256 or stronger")
    else:
        raise SsoConfigError("saml_cert_weak", "IdP signing key type is not supported")
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def parse_idp_metadata(xml: bytes) -> IdpMetadata:
    """Extract entityID, HTTP-Redirect SSO URL and signing certs from IdP metadata XML."""
    root = parse_xml(xml, what="IdP metadata")
    if root.tag == _q(NS_MD, "EntitiesDescriptor"):
        descriptors = root.findall(_q(NS_MD, "EntityDescriptor"))
        if len(descriptors) != 1:
            raise SsoConfigError(
                "saml_metadata_invalid", "metadata must describe exactly one EntityDescriptor"
            )
        root = descriptors[0]
    if root.tag != _q(NS_MD, "EntityDescriptor"):
        raise SsoConfigError("saml_metadata_invalid", "metadata root must be an EntityDescriptor")
    entity_id = (root.get("entityID") or "").strip()
    if not entity_id:
        raise SsoConfigError("saml_metadata_invalid", "metadata has no entityID")
    idp = root.find(_q(NS_MD, "IDPSSODescriptor"))
    if idp is None:
        raise SsoConfigError("saml_metadata_invalid", "metadata has no IDPSSODescriptor")

    sso_url = None
    for service in idp.findall(_q(NS_MD, "SingleSignOnService")):
        if service.get("Binding") == BINDING_REDIRECT and service.get("Location"):
            sso_url = service.get("Location", "").strip()
            break
    if sso_url is None:
        raise SsoConfigError(
            "saml_metadata_no_redirect",
            "IdP metadata offers no HTTP-Redirect SingleSignOnService "
            "(HTTP-POST SSO is unsupported)",
        )

    certs: list[str] = []
    for key_descriptor in idp.findall(_q(NS_MD, "KeyDescriptor")):
        if key_descriptor.get("use") not in (None, "signing"):
            continue
        for cert_el in key_descriptor.iter(_q(NS_DS, "X509Certificate")):
            certs.append(normalize_certificate(_text_of(cert_el, what="X509Certificate")))
    if not certs:
        raise SsoConfigError(
            "saml_metadata_no_cert", "IdP metadata contains no signing certificate"
        )
    return IdpMetadata(entity_id=entity_id, sso_url=sso_url, certs_pem=tuple(dict.fromkeys(certs)))


def build_sp_metadata(*, entity_id: str, acs_url: str, name_id_format: str) -> bytes:
    """Render the SP metadata document an IdP admin imports (no secrets; safe to publish)."""
    nsmap = {"md": NS_MD}
    root = etree.Element(_q(NS_MD, "EntityDescriptor"), nsmap=nsmap, entityID=entity_id)
    sp = etree.SubElement(
        root,
        _q(NS_MD, "SPSSODescriptor"),
        AuthnRequestsSigned="false",
        WantAssertionsSigned="true",
        protocolSupportEnumeration=NS_SAMLP,
    )
    etree.SubElement(sp, _q(NS_MD, "NameIDFormat")).text = name_id_format
    etree.SubElement(
        sp,
        _q(NS_MD, "AssertionConsumerService"),
        Binding=BINDING_POST,
        Location=acs_url,
        index="0",
        isDefault="true",
    )
    metadata: bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    return metadata


def build_authn_request(
    saml: SamlSettings, *, sp_entity_id: str, acs_url: str, relay_state: str, request_id: str
) -> tuple[str, str]:
    """Build the HTTP-Redirect AuthnRequest for `request_id`; returns `(request_id, redirect_url)`.

    The caller mints `request_id` (`new_request_id`) first so it can be stored
    in the single-use login state whose token doubles as `relay_state`.
    """
    root = etree.Element(
        _q(NS_SAMLP, "AuthnRequest"),
        nsmap={"samlp": NS_SAMLP, "saml": NS_SAML},
        ID=request_id,
        Version="2.0",
        IssueInstant=_iso(datetime.now(UTC)),
        Destination=saml.idp_sso_url,
        ProtocolBinding=BINDING_POST,
        AssertionConsumerServiceURL=acs_url,
    )
    if saml.force_authn:
        root.set("ForceAuthn", "true")
    etree.SubElement(root, _q(NS_SAML, "Issuer")).text = sp_entity_id
    etree.SubElement(
        root, _q(NS_SAMLP, "NameIDPolicy"), Format=saml.name_id_format, AllowCreate="true"
    )
    xml = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
    compressor = zlib.compressobj(level=9, wbits=-15)
    deflated = compressor.compress(xml) + compressor.flush()
    params = {
        "SAMLRequest": base64.b64encode(deflated).decode("ascii"),
        "RelayState": relay_state,
    }
    separator = "&" if urlparse(saml.idp_sso_url).query else "?"
    return request_id, f"{saml.idp_sso_url}{separator}{urlencode(params)}"


def _verify_signed_element(
    raw: bytes, certs_pem: tuple[str, ...], *, location: str
) -> etree._Element | None:
    """Return the signed element if any pinned cert verifies a signature at `location`."""
    config = SignatureConfiguration(
        require_x509=True,
        location=location,
        expect_references=1,
        signature_methods=_ALLOWED_SIGNATURE_METHODS,
        digest_algorithms=_ALLOWED_DIGESTS,
    )
    for cert_pem in certs_pem:
        try:
            result = XMLVerifier().verify(
                raw, x509_cert=cert_pem, expect_config=config, id_attribute="ID"
            )
        except (SignXMLException, CryptoInvalidSignature, ValueError, etree.LxmlError):
            continue
        signed = result[0].signed_xml if isinstance(result, list) else result.signed_xml
        if isinstance(signed, etree._Element):
            return signed
    return None


def validate_response(
    saml: SamlSettings,
    *,
    saml_response_b64: str,
    expected_request_id: str,
    sp_entity_id: str,
    acs_url: str,
    clock_skew_s: int,
    now: datetime | None = None,
) -> ValidatedAssertion:
    """Validate a posted `SAMLResponse` end to end; return the vouched identity.

    Raises `SsoProtocolError` (fixed codes, no attacker-controlled text) on any
    deviation. Pure and synchronous apart from the signature check; the caller
    handles the replay cache with the returned assertion ID/expiry.
    """
    with sso_span("sso.saml.validate", **{"sso.protocol": "saml"}):
        return _validate_response(
            saml,
            saml_response_b64=saml_response_b64,
            expected_request_id=expected_request_id,
            sp_entity_id=sp_entity_id,
            acs_url=acs_url,
            skew=timedelta(seconds=clock_skew_s),
            now=now or datetime.now(UTC),
        )


def _validate_response(
    saml: SamlSettings,
    *,
    saml_response_b64: str,
    expected_request_id: str,
    sp_entity_id: str,
    acs_url: str,
    skew: timedelta,
    now: datetime,
) -> ValidatedAssertion:
    if not saml_response_b64 or len(saml_response_b64) > MAX_RESPONSE_B64_BYTES:
        raise _fail("saml_response_size", "SAMLResponse is missing or too large")
    try:
        raw = base64.b64decode(saml_response_b64, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise _fail("saml_response_encoding", "SAMLResponse is not valid base64") from exc

    structure = parse_xml(raw, what="SAMLResponse")
    if structure.tag != _q(NS_SAMLP, "Response"):
        raise _fail("saml_not_a_response", "document root is not a samlp:Response")
    if structure.find(f".//{_q(NS_SAML, 'EncryptedAssertion')}") is not None:
        raise _fail(
            "saml_encrypted_assertion",
            "encrypted assertions are not supported; configure the IdP to send signed plaintext",
        )
    assertion_count = len(list(structure.iter(_q(NS_SAML, "Assertion"))))
    if assertion_count != 1:
        raise _fail("saml_assertion_count", "SAMLResponse must contain exactly one Assertion")

    # Trust only what signxml says was signed -- never `structure` itself.
    signed_response = _verify_signed_element(raw, saml.idp_certs_pem, location="./")
    signed_assertion: etree._Element | None = None
    if signed_response is None:
        signed_assertion = _verify_signed_element(
            raw, saml.idp_certs_pem, location=f"./{_q(NS_SAML, 'Assertion')}/"
        )
        if signed_assertion is None:
            raise _fail(
                "saml_signature_invalid",
                "no valid signature from a pinned IdP certificate over the Response or Assertion",
            )
        if signed_assertion.tag != _q(NS_SAML, "Assertion"):
            raise _fail("saml_signed_element", "signature does not cover the Assertion")
        assertion = signed_assertion
    else:
        if signed_response.tag != _q(NS_SAMLP, "Response"):
            raise _fail("saml_signed_element", "signature does not cover the Response")
        found = [e for e in signed_response.iter(_q(NS_SAML, "Assertion"))]
        if len(found) != 1:
            raise _fail(
                "saml_assertion_count", "signed Response must contain exactly one Assertion"
            )
        assertion = found[0]
        _check_response_envelope(
            signed_response,
            expected_request_id=expected_request_id,
            acs_url=acs_url,
            idp_entity_id=saml.idp_entity_id,
            require_destination=True,
        )

    if signed_assertion is not None:
        # Only the Assertion is signed, so the Response envelope is untrusted --
        # but a failure status or a visibly wrong destination still means "stop".
        _check_response_envelope(
            structure,
            expected_request_id=expected_request_id,
            acs_url=acs_url,
            idp_entity_id=saml.idp_entity_id,
            require_destination=False,
            trusted=False,
        )

    return _validate_assertion(
        assertion,
        saml,
        expected_request_id=expected_request_id,
        sp_entity_id=sp_entity_id,
        acs_url=acs_url,
        skew=skew,
        now=now,
    )


def _check_response_envelope(
    response: etree._Element,
    *,
    expected_request_id: str,
    acs_url: str,
    idp_entity_id: str,
    require_destination: bool,
    trusted: bool = True,
) -> None:
    status = response.find(f"{_q(NS_SAMLP, 'Status')}/{_q(NS_SAMLP, 'StatusCode')}")
    if status is None or status.get("Value") != STATUS_SUCCESS:
        raise _fail("saml_status_not_success", "IdP reported a non-success status")
    destination = response.get("Destination")
    if destination is None:
        if require_destination:
            raise _fail("saml_destination_missing", "signed Response has no Destination")
    elif destination != acs_url:
        raise _fail("saml_destination_mismatch", "Response Destination is not our ACS URL")
    in_response_to = response.get("InResponseTo")
    if in_response_to is not None and in_response_to != expected_request_id:
        raise _fail("saml_in_response_to", "Response is not for this login flow")
    if trusted:
        issuer = response.find(_q(NS_SAML, "Issuer"))
        if issuer is not None and _text_of(issuer, what="Issuer") != idp_entity_id:
            raise _fail("saml_issuer_mismatch", "Response issuer is not the configured IdP")


def _validate_assertion(
    assertion: etree._Element,
    saml: SamlSettings,
    *,
    expected_request_id: str,
    sp_entity_id: str,
    acs_url: str,
    skew: timedelta,
    now: datetime,
) -> ValidatedAssertion:
    if assertion.get("Version") != "2.0":
        raise _fail("saml_version", "Assertion is not SAML 2.0")
    assertion_id = (assertion.get("ID") or "").strip()
    if not assertion_id:
        raise _fail("saml_assertion_id", "Assertion has no ID")

    issuer = assertion.find(_q(NS_SAML, "Issuer"))
    if issuer is None or _text_of(issuer, what="Issuer") != saml.idp_entity_id:
        raise _fail("saml_issuer_mismatch", "Assertion issuer is not the configured IdP")

    subject = assertion.find(_q(NS_SAML, "Subject"))
    if subject is None:
        raise _fail("saml_no_subject", "Assertion has no Subject")
    name_id_el = subject.find(_q(NS_SAML, "NameID"))
    if name_id_el is None:
        raise _fail("saml_no_nameid", "Assertion Subject has no NameID")
    name_id = _text_of(name_id_el, what="NameID")
    if not name_id or len(name_id) > _MAX_SUBJECT_LEN:
        raise _fail("saml_bad_nameid", "NameID is empty or oversized")
    name_id_format = name_id_el.get("Format") or NAMEID_UNSPECIFIED
    if name_id_format == NAMEID_TRANSIENT:
        raise _fail(
            "saml_nameid_transient",
            "IdP sent a transient NameID; configure a persistent or emailAddress NameID",
        )

    expiry = _validate_subject_confirmation(
        subject,
        expected_request_id=expected_request_id,
        acs_url=acs_url,
        skew=skew,
        now=now,
    )
    _validate_conditions(assertion, sp_entity_id=sp_entity_id, skew=skew, now=now)

    authn = assertion.findall(_q(NS_SAML, "AuthnStatement"))
    if not authn:
        raise _fail("saml_no_authn_statement", "Assertion has no AuthnStatement")
    for statement in authn:
        session_end = statement.get("SessionNotOnOrAfter")
        if session_end and _parse_instant(session_end, "saml_session_time") <= now - skew:
            raise _fail("saml_session_expired", "IdP session has already ended")

    attributes = _collect_attributes(assertion)
    email = _pick_email(attributes, saml, name_id=name_id, name_id_format=name_id_format)
    display_name = _pick_first(
        attributes, ((saml.name_attribute,) if saml.name_attribute else _DEFAULT_NAME_ATTRIBUTES)
    )
    identity = ExternalIdentity(
        subject=name_id,
        email=email,
        # The IdP the tenant admin pinned is authoritative for its own directory.
        email_verified=email is not None,
        display_name=display_name[:_MAX_NAME_LEN] if display_name else None,
    )
    return ValidatedAssertion(identity=identity, assertion_id=assertion_id, not_on_or_after=expiry)


def _validate_subject_confirmation(
    subject: etree._Element,
    *,
    expected_request_id: str,
    acs_url: str,
    skew: timedelta,
    now: datetime,
) -> datetime:
    last_error: SsoProtocolError | None = None
    for confirmation in subject.findall(_q(NS_SAML, "SubjectConfirmation")):
        if confirmation.get("Method") != CM_BEARER:
            continue
        data = confirmation.find(_q(NS_SAML, "SubjectConfirmationData"))
        if data is None:
            last_error = _fail("saml_confirmation_data", "bearer confirmation has no data")
            continue
        if data.get("Recipient") != acs_url:
            last_error = _fail("saml_recipient", "bearer Recipient is not our ACS URL")
            continue
        if data.get("InResponseTo") != expected_request_id:
            last_error = _fail("saml_in_response_to", "assertion is not for this login flow")
            continue
        not_on_or_after = _parse_instant(data.get("NotOnOrAfter"), "saml_confirmation_time")
        if not_on_or_after <= now - skew:
            last_error = _fail("saml_confirmation_expired", "bearer confirmation has expired")
            continue
        not_before = data.get("NotBefore")
        if not_before and _parse_instant(not_before, "saml_confirmation_time") > now + skew:
            last_error = _fail("saml_confirmation_early", "bearer confirmation not yet valid")
            continue
        return not_on_or_after
    raise last_error or _fail("saml_no_bearer", "Assertion has no bearer SubjectConfirmation")


def _validate_conditions(
    assertion: etree._Element, *, sp_entity_id: str, skew: timedelta, now: datetime
) -> None:
    conditions = assertion.find(_q(NS_SAML, "Conditions"))
    if conditions is None:
        raise _fail("saml_no_conditions", "Assertion has no Conditions")
    for child in conditions:
        if child.tag not in _ALLOWED_CONDITIONS:
            raise _fail("saml_unknown_condition", "Assertion carries an unsupported Condition")
    not_before = conditions.get("NotBefore")
    if not_before and _parse_instant(not_before, "saml_conditions_time") > now + skew:
        raise _fail("saml_not_yet_valid", "Assertion is not yet valid")
    not_on_or_after = _parse_instant(conditions.get("NotOnOrAfter"), "saml_conditions_time")
    if not_on_or_after <= now - skew:
        raise _fail("saml_expired", "Assertion has expired")

    restrictions = conditions.findall(_q(NS_SAML, "AudienceRestriction"))
    if not restrictions:
        raise _fail("saml_no_audience", "Assertion has no AudienceRestriction")
    for restriction in restrictions:
        audiences = [
            _text_of(a, what="Audience") for a in restriction.findall(_q(NS_SAML, "Audience"))
        ]
        if sp_entity_id not in audiences:
            raise _fail("saml_audience", "Assertion is not addressed to this service provider")


def _collect_attributes(assertion: etree._Element) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {}
    for statement in assertion.findall(_q(NS_SAML, "AttributeStatement")):
        for attribute in statement.findall(_q(NS_SAML, "Attribute")):
            name = (attribute.get("Name") or "").strip().lower()
            if not name:
                continue
            values = [
                _text_of(v, what="AttributeValue")
                for v in attribute.findall(_q(NS_SAML, "AttributeValue"))
            ]
            collected.setdefault(name, []).extend(v for v in values if v)
    return collected


def _pick_first(attributes: dict[str, list[str]], names: tuple[str, ...]) -> str | None:
    for name in names:
        values = attributes.get(name.lower())
        if values:
            return values[0]
    return None


def _pick_email(
    attributes: dict[str, list[str]],
    saml: SamlSettings,
    *,
    name_id: str,
    name_id_format: str,
) -> str | None:
    names = (saml.email_attribute,) if saml.email_attribute else _DEFAULT_EMAIL_ATTRIBUTES
    candidate = _pick_first(attributes, names)
    if candidate is None and name_id_format == NAMEID_EMAIL:
        candidate = name_id
    if candidate is None:
        return None
    candidate = candidate.strip().lower()
    return candidate if candidate.count("@") == 1 and not candidate.startswith("@") else None
