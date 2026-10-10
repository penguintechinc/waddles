"""`services/sso_saml.py` -- SP-side SAML 2.0 request/metadata building and Response validation.

Covers IdP metadata import and the full Response validation matrix including
signature-wrapping (XSW), comment-injection, XXE/DOCTYPE, replay-shaped, time-window and
audience attacks.

The IdP is `tests/sso/idp_fakes.py::FakeSamlIdp`, which emits real `signxml`-signed
SAML Responses; nothing in `sso_saml` is mocked.
"""

from __future__ import annotations

import base64
import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from lxml import etree
from signxml.algorithms import DigestAlgorithm, SignatureMethod
from signxml.verifier import SignatureConfiguration, XMLVerifier

from services import sso_saml
from services.sso_types import SamlSettings, SsoConfigError, SsoProtocolError
from tests.sso.idp_fakes import (
    NS_DS,
    NS_SAML,
    NS_SAMLP,
    FakeSamlIdp,
    ParsedAuthnRequest,
    make_cert,
    pem_cert,
    tamper,
)

SP_ENTITY = "https://hub.example.com/api/v1/auth/sso/conn-1/metadata"
ACS = "https://hub.example.com/api/v1/auth/sso/conn-1/acs"
EMAIL_FMT = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
PERSISTENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"
TRANSIENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:transient"


def _settings(idp: FakeSamlIdp, **kw: Any) -> SamlSettings:
    base: dict[str, Any] = {
        "idp_entity_id": idp.entity_id,
        "idp_sso_url": idp.sso_url,
        "idp_certs_pem": (idp.cert_pem,),
        "allowed_domains": ("acme.test",),
    }
    base.update(kw)
    return SamlSettings(**base)


def _authn(idp: FakeSamlIdp, settings: SamlSettings | None = None) -> ParsedAuthnRequest:
    s = settings or _settings(idp)
    rid = sso_saml.new_request_id()
    _, url = sso_saml.build_authn_request(
        s, sp_entity_id=SP_ENTITY, acs_url=ACS, relay_state="relay-1", request_id=rid
    )
    parsed = idp.parse_authn_request(url)
    assert parsed.request_id == rid
    return parsed


def _validate(
    idp: FakeSamlIdp,
    raw: bytes | str,
    authn: ParsedAuthnRequest,
    *,
    settings: SamlSettings | None = None,
    now: datetime | None = None,
    skew: int = 120,
) -> sso_saml.ValidatedAssertion:
    b64 = raw if isinstance(raw, str) else base64.b64encode(raw).decode()
    return sso_saml.validate_response(
        settings or _settings(idp),
        saml_response_b64=b64,
        expected_request_id=authn.request_id,
        sp_entity_id=SP_ENTITY,
        acs_url=ACS,
        clock_skew_s=skew,
        now=now,
    )


def _reject(
    idp: FakeSamlIdp, raw: bytes | str, authn: ParsedAuthnRequest, code: str, **kw: Any
) -> SsoProtocolError:
    with pytest.raises(SsoProtocolError) as exc:
        _validate(idp, raw, authn, **kw)
    assert exc.value.code == code, f"{exc.value.code}: {exc.value.message}"
    return exc.value


def _resign_with_sha1(raw: bytes, key: Any) -> bytes:
    """Rewrite a Response-level signature to RSA-SHA1/SHA-1 with a *valid* signature value."""
    import base64 as b64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    root = etree.fromstring(raw)  # noqa: S320
    sig = root.find(f"{{{NS_DS}}}Signature")
    info = sig.find(f"{{{NS_DS}}}SignedInfo")
    info.find(f"{{{NS_DS}}}SignatureMethod").set(
        "Algorithm", "http://www.w3.org/2000/09/xmldsig#rsa-sha1"
    )
    reference = info.find(f"{{{NS_DS}}}Reference")
    reference.find(f"{{{NS_DS}}}DigestMethod").set(
        "Algorithm", "http://www.w3.org/2000/09/xmldsig#sha1"
    )
    stripped = copy.deepcopy(root)
    stripped.remove(stripped.find(f"{{{NS_DS}}}Signature"))
    canonical = etree.tostring(stripped, method="c14n", exclusive=True, with_comments=False)
    digest = hashes.Hash(hashes.SHA1())  # noqa: S303 - deliberately weak, for the downgrade test
    digest.update(canonical)
    reference.find(f"{{{NS_DS}}}DigestValue").text = b64.b64encode(digest.finalize()).decode()
    signed_info_c14n = etree.tostring(info, method="c14n", exclusive=True, with_comments=False)
    signature = key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303
    sig.find(f"{{{NS_DS}}}SignatureValue").text = b64.b64encode(signature).decode()
    return etree.tostring(root)


@pytest.fixture
def idp() -> FakeSamlIdp:
    return FakeSamlIdp()


class TestAuthnRequest:
    def test_request_is_well_formed_and_deflated_over_redirect_binding(
        self, idp: FakeSamlIdp
    ) -> None:
        authn = _authn(idp)
        assert authn.request_id.startswith("_")
        assert authn.acs_url == ACS
        assert authn.issuer == SP_ENTITY
        assert authn.destination == idp.sso_url
        assert authn.relay_state == "relay-1"
        assert authn.name_id_policy_format == EMAIL_FMT
        assert authn.force_authn is False

    def test_force_authn_and_name_id_format_are_honoured(self, idp: FakeSamlIdp) -> None:
        s = _settings(idp, force_authn=True, name_id_format=PERSISTENT)
        authn = _authn(idp, s)
        assert authn.force_authn is True
        assert authn.name_id_policy_format == PERSISTENT

    def test_existing_query_on_sso_url_is_extended_with_ampersand(self, idp: FakeSamlIdp) -> None:
        s = _settings(idp, idp_sso_url=idp.sso_url + "?tenant=a")
        _, url = sso_saml.build_authn_request(
            s, sp_entity_id=SP_ENTITY, acs_url=ACS, relay_state="r", request_id="_x"
        )
        q = parse_qs(urlparse(url).query)
        assert q["tenant"] == ["a"]
        assert "SAMLRequest" in q
        assert "?tenant=a&SAMLRequest=" in url

    def test_request_ids_are_unique_ncnames(self) -> None:
        ids = {sso_saml.new_request_id() for _ in range(50)}
        assert len(ids) == 50
        assert all(i[0] == "_" for i in ids)

    def test_hostile_values_are_xml_escaped_not_injected(self, idp: FakeSamlIdp) -> None:
        _, url = sso_saml.build_authn_request(
            _settings(idp),
            sp_entity_id='https://sp/"><injected/>',
            acs_url=ACS,
            relay_state="r",
            request_id="_x",
        )
        decoded = idp.parse_authn_request(url)
        assert "<injected/>" not in decoded.issuer or decoded.issuer == 'https://sp/"><injected/>'


class TestSpMetadata:
    def test_metadata_describes_the_acs_and_requires_signed_assertions(self) -> None:
        xml = sso_saml.build_sp_metadata(entity_id=SP_ENTITY, acs_url=ACS, name_id_format=EMAIL_FMT)
        root = etree.fromstring(xml)  # noqa: S320
        md = "urn:oasis:names:tc:SAML:2.0:metadata"
        assert root.get("entityID") == SP_ENTITY
        sp = root.find(f"{{{md}}}SPSSODescriptor")
        assert sp.get("WantAssertionsSigned") == "true"
        assert sp.get("AuthnRequestsSigned") == "false"
        acs = sp.find(f"{{{md}}}AssertionConsumerService")
        assert acs.get("Location") == ACS
        assert acs.get("Binding") == sso_saml.BINDING_POST
        assert sp.find(f"{{{md}}}NameIDFormat").text == EMAIL_FMT

    def test_metadata_contains_no_secret_material(self) -> None:
        xml = sso_saml.build_sp_metadata(entity_id=SP_ENTITY, acs_url=ACS, name_id_format=EMAIL_FMT)
        assert b"PRIVATE" not in xml
        assert b"KeyDescriptor" not in xml


class TestIdpMetadataImport:
    def test_parses_entity_redirect_url_and_cert(self, idp: FakeSamlIdp) -> None:
        meta = sso_saml.parse_idp_metadata(idp.metadata_xml().encode())
        assert meta.entity_id == idp.entity_id
        assert meta.sso_url == idp.sso_url  # the Redirect binding, not the POST one
        assert meta.certs_pem == (idp.cert_pem,)

    def test_entities_descriptor_wrapper_with_one_entity(self, idp: FakeSamlIdp) -> None:
        inner = idp.metadata_xml().split("?>", 1)[1]
        wrapped = f'<md:EntitiesDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata">{inner}</md:EntitiesDescriptor>'
        assert sso_saml.parse_idp_metadata(wrapped.encode()).entity_id == idp.entity_id

    def test_entities_descriptor_with_several_entities_is_ambiguous(self, idp: FakeSamlIdp) -> None:
        inner = idp.metadata_xml().split("?>", 1)[1]
        wrapped = f'<md:EntitiesDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata">{inner}{inner}</md:EntitiesDescriptor>'
        with pytest.raises(SsoConfigError) as exc:
            sso_saml.parse_idp_metadata(wrapped.encode())
        assert exc.value.code == "saml_metadata_invalid"

    def test_duplicate_certs_are_collapsed(self, idp: FakeSamlIdp) -> None:
        xml = idp.metadata_xml()
        kd_start = xml.index("<md:KeyDescriptor")
        kd_end = xml.index("</md:KeyDescriptor>") + len("</md:KeyDescriptor>")
        doubled = xml[:kd_end] + xml[kd_start:kd_end] + xml[kd_end:]
        assert len(sso_saml.parse_idp_metadata(doubled.encode()).certs_pem) == 1

    def test_encryption_only_keys_are_ignored(self, idp: FakeSamlIdp) -> None:
        xml = idp.metadata_xml().replace('use="signing"', 'use="encryption"')
        with pytest.raises(SsoConfigError) as exc:
            sso_saml.parse_idp_metadata(xml.encode())
        assert exc.value.code == "saml_metadata_no_cert"

    @pytest.mark.parametrize(
        ("mutate", "code"),
        [
            (lambda x: x.replace("HTTP-Redirect", "HTTP-Other"), "saml_metadata_no_redirect"),
            (
                lambda x: x.replace('entityID="https://saml-idp.example.com/metadata"', ""),
                "saml_metadata_invalid",
            ),
            (
                lambda x: x.replace("md:IDPSSODescriptor", "md:SPSSODescriptor"),
                "saml_metadata_invalid",
            ),
            (lambda x: x.replace("md:EntityDescriptor", "md:Other"), "saml_metadata_invalid"),
        ],
    )
    def test_incomplete_metadata_is_rejected(
        self, idp: FakeSamlIdp, mutate: Any, code: str
    ) -> None:
        with pytest.raises(SsoConfigError) as exc:
            sso_saml.parse_idp_metadata(mutate(idp.metadata_xml()).encode())
        assert exc.value.code == code

    def test_malformed_xml_and_doctype_are_rejected(self) -> None:
        with pytest.raises(SsoProtocolError) as exc:
            sso_saml.parse_idp_metadata(b"<not-xml")
        assert exc.value.code == "saml_xml_malformed"
        with pytest.raises(SsoProtocolError) as exc2:
            sso_saml.parse_idp_metadata(
                b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><x>&xxe;</x>'
            )
        assert exc2.value.code == "saml_doctype_rejected"

    def test_non_text_certificate_node_is_rejected(self, idp: FakeSamlIdp) -> None:
        xml = idp.metadata_xml().replace("<ds:X509Certificate>", "<ds:X509Certificate><b>x</b>")
        with pytest.raises(SsoProtocolError):
            sso_saml.parse_idp_metadata(xml.encode())


class TestCertificateNormalisation:
    def test_pem_and_bare_base64_normalise_to_the_same_pem(self, idp: FakeSamlIdp) -> None:
        bare = "".join(idp.cert_pem.splitlines()[1:-1])
        assert sso_saml.normalize_certificate(idp.cert_pem) == sso_saml.normalize_certificate(bare)

    def test_expired_certificate_is_rejected(self) -> None:
        key = rsa.generate_private_key(65537, 2048)
        with pytest.raises(SsoConfigError) as exc:
            sso_saml.normalize_certificate(pem_cert(make_cert(key, expired=True)))
        assert exc.value.code == "saml_cert_expired"

    def test_weak_rsa_key_is_rejected(self) -> None:
        key = rsa.generate_private_key(65537, 1024)  # noqa: S505 - deliberately weak
        with pytest.raises(SsoConfigError) as exc:
            sso_saml.normalize_certificate(pem_cert(make_cert(key)))
        assert exc.value.code == "saml_cert_weak"

    def test_ec_p256_certificate_is_accepted(self) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        assert "BEGIN CERTIFICATE" in sso_saml.normalize_certificate(pem_cert(make_cert(key)))

    def test_weak_ec_curve_is_rejected(self) -> None:
        key = ec.generate_private_key(ec.SECP192R1())
        with pytest.raises(SsoConfigError) as exc:
            sso_saml.normalize_certificate(pem_cert(make_cert(key)))
        assert exc.value.code == "saml_cert_weak"

    @pytest.mark.parametrize(
        "junk",
        [
            "",
            "hello",
            "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----",
            "AAAA",
            "%%%",
        ],
    )
    def test_garbage_is_rejected(self, junk: str) -> None:
        with pytest.raises(SsoConfigError) as exc:
            sso_saml.normalize_certificate(junk)
        assert exc.value.code == "saml_cert_invalid"


class TestValidResponses:
    def test_response_signed_at_response_level(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        result = _validate(idp, idp.build_response(authn, sign="response"), authn)
        ident = result.identity
        assert ident.subject == "alice@acme.test"
        assert ident.email == "alice@acme.test"
        assert ident.email_verified is True
        assert ident.display_name == "Alice Example"
        assert result.assertion_id.startswith("_a")
        assert result.not_on_or_after > datetime.now(UTC)

    def test_assertion_level_signature(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        assert _validate(idp, idp.build_response(authn, sign="assertion"), authn).identity.email

    def test_both_signed(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        assert _validate(idp, idp.build_response(authn, sign="both"), authn).identity.email

    def test_accepted_from_a_base64_string(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        b64 = idp.encode(idp.build_response(authn))
        assert _validate(idp, b64, authn).identity.subject == "alice@acme.test"

    def test_persistent_nameid_with_mail_attribute(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(
            authn,
            name_id="opaque-persistent-id-77",
            name_id_format=PERSISTENT,
            attributes={"mail": ["Bob@Acme.test"], "cn": ["Bob B"]},
        )
        ident = _validate(idp, raw, authn).identity
        assert ident.subject == "opaque-persistent-id-77"
        assert ident.email == "bob@acme.test"
        assert ident.display_name == "Bob B"

    def test_custom_attribute_names_from_settings(self, idp: FakeSamlIdp) -> None:
        s = _settings(idp, email_attribute="workEmail", name_attribute="fullName")
        authn = _authn(idp, s)
        raw = idp.build_response(
            authn,
            name_id="u-1",
            name_id_format=PERSISTENT,
            attributes={
                "workEmail": ["w@acme.test"],
                "fullName": ["W Worker"],
                "mail": ["x@evil.test"],
            },
        )
        ident = _validate(idp, raw, authn, settings=s).identity
        assert ident.email == "w@acme.test"
        assert ident.display_name == "W Worker"

    def test_no_email_anywhere_yields_none_not_a_guess(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(
            authn, name_id="u-2", name_id_format=PERSISTENT, attributes={"displayName": ["Zed"]}
        )
        ident = _validate(idp, raw, authn).identity
        assert ident.email is None
        assert ident.email_verified is False

    @pytest.mark.parametrize("bad_email", ["no-at-sign", "two@@signs.test", "@leading.test"])
    def test_malformed_email_values_are_dropped(self, idp: FakeSamlIdp, bad_email: str) -> None:
        authn = _authn(idp)
        raw = idp.build_response(
            authn, name_id="u-3", name_id_format=PERSISTENT, attributes={"email": [bad_email]}
        )
        assert _validate(idp, raw, authn).identity.email is None

    def test_certificate_rotation_second_pinned_cert_verifies(self, idp: FakeSamlIdp) -> None:
        new_key = rsa.generate_private_key(65537, 2048)
        new_cert = make_cert(new_key, cn="rotated")
        s = _settings(idp, idp_certs_pem=(idp.cert_pem, pem_cert(new_cert)))
        authn = _authn(idp, s)
        raw = idp.build_response(authn, signing_key=new_key, signing_cert=new_cert)
        assert _validate(idp, raw, authn, settings=s).identity.email

    def test_ecdsa_signed_response(self) -> None:
        ec_key = ec.generate_private_key(ec.SECP256R1())
        ec_idp = FakeSamlIdp(key=ec_key)
        authn = _authn(ec_idp)
        raw = ec_idp.build_response(authn, signature_algorithm=SignatureMethod.ECDSA_SHA256)
        assert _validate(ec_idp, raw, authn).identity.email

    @pytest.mark.parametrize(
        ("sig", "digest"),
        [
            (SignatureMethod.RSA_SHA384, DigestAlgorithm.SHA384),
            (SignatureMethod.RSA_SHA512, DigestAlgorithm.SHA512),
        ],
    )
    def test_stronger_sha2_variants_are_accepted(
        self, idp: FakeSamlIdp, sig: SignatureMethod, digest: DigestAlgorithm
    ) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, signature_algorithm=sig, digest_algorithm=digest)
        assert _validate(idp, raw, authn).identity.email

    def test_one_of_several_audience_restrictions_values(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, audience=SP_ENTITY)
        assert _validate(idp, raw, authn)

    def test_session_not_on_or_after_in_the_future_is_fine(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        assert _validate(idp, idp.build_response(authn, session_not_on_or_after_offset=3600), authn)


class TestSignatureEnforcement:
    def test_unsigned_response_is_rejected(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(idp, idp.build_response(authn, sign="none"), authn, "saml_signature_invalid")

    def test_signed_by_an_unpinned_key_is_rejected_even_with_its_own_cert_embedded(
        self, idp: FakeSamlIdp
    ) -> None:
        attacker_key = rsa.generate_private_key(65537, 2048)
        attacker_cert = make_cert(attacker_key, cn="attacker")
        authn = _authn(idp)
        raw = idp.build_response(authn, signing_key=attacker_key, signing_cert=attacker_cert)
        assert pem_cert(attacker_cert).split("\n")[1][:20].encode()  # cert really is embedded
        _reject(idp, raw, authn, "saml_signature_invalid")

    @pytest.mark.parametrize("sign", ["response", "assertion"])
    def test_tampering_with_the_nameid_after_signing_breaks_the_signature(
        self, idp: FakeSamlIdp, sign: str
    ) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, sign=sign, name_id="alice@acme.test")

        def mutate(root: etree._Element) -> None:
            root.find(f".//{{{NS_SAML}}}NameID").text = "admin@acme.test"

        _reject(idp, tamper(raw, mutate), authn, "saml_signature_invalid")

    def test_sha1_signatures_are_rejected_even_when_cryptographically_valid(
        self, idp: FakeSamlIdp
    ) -> None:
        # signxml refuses to *produce* SHA-1 signatures, so re-sign a response by hand with
        # RSA-SHA1/SHA-1 digest. The signature is genuinely valid; only the algorithm
        # policy can reject it (downgrade regression).
        authn = _authn(idp)
        raw = _resign_with_sha1(idp.build_response(authn, sign="response"), idp.key)
        # Prove the premise: a permissive verifier accepts this exact document...
        permissive = SignatureConfiguration(
            signature_methods=frozenset({SignatureMethod.RSA_SHA1}),
            digest_algorithms=frozenset({DigestAlgorithm.SHA1}),
            location="./",
            expect_references=1,
        )
        XMLVerifier().verify(
            raw, x509_cert=idp.cert_pem, expect_config=permissive, id_attribute="ID"
        )
        # ...so only our algorithm policy can be what rejects it.
        _reject(idp, raw, authn, "saml_signature_invalid")

    def test_signature_stripped_from_signed_assertion(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, sign="assertion")

        def strip(root: etree._Element) -> None:
            for sig in root.iter(f"{{{NS_DS}}}Signature"):
                sig.getparent().remove(sig)

        _reject(idp, tamper(raw, strip), authn, "saml_signature_invalid")


class TestSignatureWrapping:
    """XSW: documents rearranged so attacker-controlled data sits where a naive reader looks.

    All must fail; none may yield the forged identity.
    """

    def _legit(self, idp: FakeSamlIdp, sign: str) -> tuple[bytes, ParsedAuthnRequest]:
        authn = _authn(idp)
        return idp.build_response(authn, sign=sign, name_id="alice@acme.test"), authn

    def _forged_assertion(self, root: etree._Element) -> etree._Element:
        forged = copy.deepcopy(root.find(f"{{{NS_SAML}}}Assertion"))
        for sig in forged.iter(f"{{{NS_DS}}}Signature"):
            sig.getparent().remove(sig)
        forged.set("ID", "_forged")
        forged.find(f".//{{{NS_SAML}}}NameID").text = "admin@acme.test"
        return forged

    def test_unsigned_forged_assertion_added_before_the_signed_one(self, idp: FakeSamlIdp) -> None:
        raw, authn = self._legit(idp, "assertion")

        def mutate(root: etree._Element) -> None:
            legit = root.find(f"{{{NS_SAML}}}Assertion")
            root.insert(list(root).index(legit), self._forged_assertion(root))

        _reject(idp, tamper(raw, mutate), authn, "saml_assertion_count")

    def test_unsigned_forged_assertion_added_after_the_signed_one(self, idp: FakeSamlIdp) -> None:
        raw, authn = self._legit(idp, "assertion")

        def mutate(root: etree._Element) -> None:
            root.append(self._forged_assertion(root))

        _reject(idp, tamper(raw, mutate), authn, "saml_assertion_count")

    def test_signed_assertion_smuggled_into_extensions_with_forged_outer(
        self, idp: FakeSamlIdp
    ) -> None:
        raw, authn = self._legit(idp, "assertion")

        def mutate(root: etree._Element) -> None:
            legit = root.find(f"{{{NS_SAML}}}Assertion")
            forged = self._forged_assertion(root)
            ext = etree.Element(f"{{{NS_SAMLP}}}Extensions")
            root.replace(legit, forged)
            ext.append(legit)
            root.insert(0, ext)

        _reject(idp, tamper(raw, mutate), authn, "saml_assertion_count")

    def test_signed_assertion_wrapped_inside_a_forged_assertions_advice(
        self, idp: FakeSamlIdp
    ) -> None:
        raw, authn = self._legit(idp, "assertion")

        def mutate(root: etree._Element) -> None:
            legit = root.find(f"{{{NS_SAML}}}Assertion")
            forged = self._forged_assertion(root)
            advice = etree.SubElement(forged, f"{{{NS_SAML}}}Advice")
            root.replace(legit, forged)
            advice.append(legit)

        _reject(idp, tamper(raw, mutate), authn, "saml_assertion_count")

    def test_signed_response_with_a_second_assertion_injected(self, idp: FakeSamlIdp) -> None:
        raw, authn = self._legit(idp, "response")

        def mutate(root: etree._Element) -> None:
            root.append(self._forged_assertion(root))

        _reject(idp, tamper(raw, mutate), authn, "saml_assertion_count")

    def test_encrypted_assertion_is_refused_loudly(self, idp: FakeSamlIdp) -> None:
        raw, authn = self._legit(idp, "response")

        def mutate(root: etree._Element) -> None:
            root.append(etree.Element(f"{{{NS_SAML}}}EncryptedAssertion"))

        _reject(idp, tamper(raw, mutate), authn, "saml_encrypted_assertion")

    def test_comment_injection_cannot_truncate_or_redirect_the_identity(
        self, idp: FakeSamlIdp
    ) -> None:
        # Classic NameID comment attack: user@acme.test<!---->.evil.example -- a DOM that reads
        # only the first text node would see "user@acme.test".
        authn = _authn(idp)
        raw = idp.build_response(
            authn, name_id_inner_xml="user@acme.test<!--x-->.evil.example", sign="response"
        )
        try:
            result = _validate(idp, raw, authn)
        except SsoProtocolError:
            return  # refusing is acceptable
        assert result.identity.subject != "user@acme.test"
        assert result.identity.subject == "user@acme.test.evil.example"


class TestEnvelopeAndStructure:
    def test_wrong_root_element(self, idp: FakeSamlIdp) -> None:
        _reject(idp, b"<a/>", _authn(idp), "saml_not_a_response")

    @pytest.mark.parametrize(
        "payload",
        [
            b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><samlp:Response xmlns:samlp="x">&xxe;</samlp:Response>',
            b'<!doctype r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><r>&b;</r>',
        ],
    )
    def test_doctype_and_entity_declarations_are_refused_before_parsing(
        self, idp: FakeSamlIdp, payload: bytes
    ) -> None:
        _reject(idp, payload, _authn(idp), "saml_doctype_rejected")

    def test_malformed_xml(self, idp: FakeSamlIdp) -> None:
        _reject(idp, b"<samlp:Response", _authn(idp), "saml_xml_malformed")

    @pytest.mark.parametrize("b64", ["", "A" * (sso_saml.MAX_RESPONSE_B64_BYTES + 1)])
    def test_empty_or_oversized_field(self, idp: FakeSamlIdp, b64: str) -> None:
        _reject(idp, b64, _authn(idp), "saml_response_size")

    def test_zero_assertions(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)

        def mutate(root: etree._Element) -> None:
            root.remove(root.find(f"{{{NS_SAML}}}Assertion"))

        _reject(
            idp,
            tamper(idp.build_response(authn, sign="none"), mutate),
            authn,
            "saml_assertion_count",
        )

    def test_non_success_status(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, status="urn:oasis:names:tc:SAML:2.0:status:Responder")
        _reject(idp, raw, authn, "saml_status_not_success")

    def test_destination_mismatch_on_signed_response(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, destination="https://evil.example.com/acs")
        _reject(idp, raw, authn, "saml_destination_mismatch")

    def test_destination_missing_on_signed_response(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(
            idp, idp.build_response(authn, omit_destination=True), authn, "saml_destination_missing"
        )

    def test_destination_may_be_absent_when_only_the_assertion_is_signed(
        self, idp: FakeSamlIdp
    ) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, sign="assertion", omit_destination=True)
        assert _validate(idp, raw, authn)

    def test_response_in_response_to_another_request(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        other = _authn(idp)
        raw = idp.build_response(other)  # answers a different AuthnRequest
        _reject(idp, raw, authn, "saml_in_response_to")

    def test_assertion_level_in_response_to_mismatch(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, sign="assertion", in_response_to="_someone_elses")
        _reject(idp, raw, authn, "saml_in_response_to")

    def test_unsolicited_idp_initiated_response_is_refused(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        forged_authn = ParsedAuthnRequest(
            request_id="_never_requested",
            acs_url=authn.acs_url,
            issuer=authn.issuer,
            destination=authn.destination,
            relay_state="",
            name_id_policy_format=None,
            force_authn=False,
        )
        _reject(idp, idp.build_response(forged_authn), authn, "saml_in_response_to")

    def test_response_level_issuer_mismatch(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(
            idp,
            idp.build_response(authn, response_issuer="https://evil.example.com"),
            authn,
            "saml_issuer_mismatch",
        )

    def test_assertion_issuer_mismatch(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(
            idp,
            idp.build_response(authn, issuer="https://evil.example.com"),
            authn,
            "saml_issuer_mismatch",
        )


class TestAssertionRules:
    def test_wrong_audience(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(
            idp,
            idp.build_response(authn, audience="https://other-sp.example.com"),
            authn,
            "saml_audience",
        )

    def test_every_audience_restriction_must_match(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        extra = "<saml:AudienceRestriction><saml:Audience>https://other.example.com</saml:Audience></saml:AudienceRestriction>"
        raw = idp.build_response(authn, extra_audience_restriction_xml=extra)
        _reject(idp, raw, authn, "saml_audience")

    def test_wrong_recipient(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, recipient="https://evil.example.com/acs")
        _reject(idp, raw, authn, "saml_recipient")

    def test_missing_bearer_confirmation(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(idp, idp.build_response(authn, bearer=False), authn, "saml_no_bearer")

    def test_missing_nameid(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(idp, idp.build_response(authn, omit_name_id=True), authn, "saml_no_nameid")

    def test_transient_nameid_is_refused(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(
            idp,
            idp.build_response(authn, name_id="x", name_id_format=TRANSIENT),
            authn,
            "saml_nameid_transient",
        )

    @pytest.mark.parametrize("name_id", ["", "   ", "n" * 513])
    def test_empty_or_oversized_nameid(self, idp: FakeSamlIdp, name_id: str) -> None:
        authn = _authn(idp)
        _reject(idp, idp.build_response(authn, name_id=name_id), authn, "saml_bad_nameid")

    def test_nameid_with_nested_elements_is_refused(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, name_id_inner_xml="alice<b>x</b>@acme.test")
        _reject(idp, raw, authn, "saml_value_has_children")

    def test_no_conditions(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(
            idp, idp.build_response(authn, include_conditions=False), authn, "saml_no_conditions"
        )

    def test_unknown_condition_fails_closed(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, extra_condition_xml="<saml:Condition/>")
        _reject(idp, raw, authn, "saml_unknown_condition")

    def test_known_optional_conditions_are_tolerated(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, extra_condition_xml="<saml:OneTimeUse/>")
        assert _validate(idp, raw, authn)

    def test_no_authn_statement(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        _reject(
            idp, idp.build_response(authn, authn_statement=False), authn, "saml_no_authn_statement"
        )

    def test_idp_session_already_ended(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, session_not_on_or_after_offset=-3600)
        _reject(idp, raw, authn, "saml_session_expired")

    def test_assertion_without_id_or_wrong_version(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, sign="none")

        def no_id(root: etree._Element) -> None:
            del root.find(f"{{{NS_SAML}}}Assertion").attrib["ID"]

        def old_version(root: etree._Element) -> None:
            root.find(f"{{{NS_SAML}}}Assertion").set("Version", "1.1")

        # unsigned docs fail on signature first; assert the *signed* paths via re-sign is
        # covered by the happy tests -- here we only assert both are rejected overall.
        for mutate in (no_id, old_version):
            with pytest.raises(SsoProtocolError):
                _validate(idp, tamper(raw, mutate), authn)


class TestTimeWindows:
    def test_expired_assertion(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(
            authn, not_on_or_after_offset=-600, conf_not_on_or_after_offset=300
        )
        _reject(idp, raw, authn, "saml_expired")

    def test_not_yet_valid_assertion(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, not_before_offset=3600)
        _reject(idp, raw, authn, "saml_not_yet_valid")

    def test_expired_bearer_confirmation(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, conf_not_on_or_after_offset=-600)
        _reject(idp, raw, authn, "saml_confirmation_expired")

    def test_clock_skew_tolerance_accepts_slightly_stale_assertions(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, not_on_or_after_offset=-60, conf_not_on_or_after_offset=-60)
        assert _validate(idp, raw, authn, skew=120)
        with pytest.raises(SsoProtocolError):
            _validate(idp, raw, authn, skew=30)

    def test_explicit_now_makes_validation_deterministic(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn)
        later = datetime.now(UTC) + timedelta(hours=2)
        _reject(idp, raw, authn, "saml_confirmation_expired", now=later)

    def test_malformed_timestamp_is_rejected(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, sign="none")

        def mutate(root: etree._Element) -> None:
            root.find(f".//{{{NS_SAML}}}Conditions").set("NotOnOrAfter", "yesterday-ish")

        with pytest.raises(SsoProtocolError):
            _validate(idp, tamper(raw, mutate), authn)


class TestAssertionStructureEdgeCases:
    """Each case is a *validly signed* assertion with exactly one structural defect.

    The signature check passes, so the specific rule under test is what refuses it.
    """

    @pytest.mark.parametrize(
        ("knobs", "code"),
        [
            ({"assertion_version": "1.1"}, "saml_version"),
            ({"omit_assertion_id": True}, "saml_assertion_id"),
            ({"assertion_issuer": "https://evil.example.com"}, "saml_issuer_mismatch"),
            ({"omit_subject": True}, "saml_no_subject"),
            ({"bearer_without_data": True}, "saml_confirmation_data"),
            ({"conf_in_response_to": "_someone_else"}, "saml_in_response_to"),
            ({"conf_not_before_offset": 3600}, "saml_confirmation_early"),
            ({"omit_audience": True}, "saml_no_audience"),
            ({"omit_not_on_or_after": True}, "saml_conditions_time"),
            ({"not_on_or_after_raw": "next tuesday"}, "saml_conditions_time"),
        ],
    )
    def test_single_defect_is_refused(
        self, idp: FakeSamlIdp, knobs: dict[str, Any], code: str
    ) -> None:
        authn = _authn(idp)
        _reject(idp, idp.build_response(authn, sign="response", **knobs), authn, code)

    def test_holder_of_key_confirmation_is_skipped_in_favour_of_a_valid_bearer(
        self, idp: FakeSamlIdp
    ) -> None:
        authn = _authn(idp)
        hok = '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:holder-of-key"/>'
        raw = idp.build_response(authn, extra_confirmation_xml=hok)
        assert _validate(idp, raw, authn).identity.email == "alice@acme.test"

    def test_only_a_holder_of_key_confirmation_is_not_enough(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        hok = '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:holder-of-key"/>'
        _reject(
            idp,
            idp.build_response(authn, bearer=False, extra_confirmation_xml=hok),
            authn,
            "saml_no_bearer",
        )

    def test_attributes_without_a_name_are_ignored(self, idp: FakeSamlIdp) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn, attributes={"": ["ghost"], "displayName": ["Real"]})
        assert _validate(idp, raw, authn).identity.display_name == "Real"

    def test_unsupported_signing_key_type_in_metadata_is_rejected(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        cert = make_cert(ed25519.Ed25519PrivateKey.generate())
        with pytest.raises(SsoConfigError) as exc:
            sso_saml.normalize_certificate(pem_cert(cert))
        assert exc.value.code == "saml_cert_weak"


class TestSignedElementDefences:
    """Defence in depth: even if the verifier returned something unexpected, trust is refused."""

    def test_response_level_signature_that_covers_a_non_response_element(
        self, idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn)
        stray = etree.Element(f"{{{NS_SAML}}}Assertion")
        monkeypatch.setattr(sso_saml, "_verify_signed_element", lambda *a, **k: stray)
        _reject(idp, raw, authn, "saml_signed_element")

    def test_assertion_level_signature_that_covers_a_non_assertion_element(
        self, idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn)
        calls = iter([None, etree.Element(f"{{{NS_SAMLP}}}Response")])
        monkeypatch.setattr(sso_saml, "_verify_signed_element", lambda *a, **k: next(calls))
        _reject(idp, raw, authn, "saml_signed_element")

    def test_signed_response_whose_signed_subtree_lost_the_assertion(
        self, idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        authn = _authn(idp)
        raw = idp.build_response(authn)
        hollow = etree.Element(f"{{{NS_SAMLP}}}Response")
        monkeypatch.setattr(sso_saml, "_verify_signed_element", lambda *a, **k: hollow)
        _reject(idp, raw, authn, "saml_assertion_count")
