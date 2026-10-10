"""`services/sso_oidc.py` against a protocol-faithful fake IdP (only the socket is mocked).

Covers discovery validation, PKCE+nonce authorize URLs, token-endpoint client
authentication, and the ID-token validation matrix: every way a forged,
replayed, mis-addressed or downgraded token must be refused.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from services import sso_oidc
from services.sso_http import SsoHttp
from services.sso_oidc import OidcClient, new_nonce, new_pkce_pair
from services.sso_settings import GOOGLE_DISCOVERY_URL, GOOGLE_ISSUER, SsoSettings
from services.sso_types import (
    PROTOCOL_GOOGLE,
    PROTOCOL_OIDC,
    OidcSettings,
    SsoConfigError,
    SsoIdpUnavailableError,
    SsoProtocolError,
)
from tests.sso.idp_fakes import FakeOidcIdp

REDIRECT = "https://hub.example.com/api/v1/auth/sso/c1/callback"


def _settings_for(idp: FakeOidcIdp, **kw: Any) -> OidcSettings:
    return OidcSettings(
        issuer=idp.issuer,
        client_id=idp.client_id,
        discovery_url=idp.discovery_url,
        allowed_domains=("acme.test",),
        **kw,
    )


def _client(
    idp: FakeOidcIdp, *, settings: SsoSettings | None = None, clock: Any = time.time
) -> OidcClient:
    s = settings or SsoSettings()
    return OidcClient(SsoHttp(s, protocol="oidc", transport=idp.transport), s, clock=clock)


async def _login(
    idp: FakeOidcIdp,
    oidc: OidcSettings,
    *,
    client: OidcClient | None = None,
    protocol: str = PROTOCOL_OIDC,
    secret: str | None = "use-idp",
    nonce_override: str | None = None,
    verifier_override: str | None = None,
    **claims: Any,
) -> Any:
    c = client or _client(idp)
    nonce = new_nonce()
    verifier, challenge = new_pkce_pair()
    url = await c.authorization_url(
        oidc,
        client_id=idp.client_id,
        redirect_uri=REDIRECT,
        state="st",
        nonce=nonce,
        code_challenge=challenge,
    )
    code, _state = idp.authorize(url, **claims)
    return await c.complete(
        oidc,
        protocol=protocol,
        client_id=idp.client_id,
        client_secret=idp.client_secret if secret == "use-idp" else secret,
        redirect_uri=REDIRECT,
        code=code,
        code_verifier=verifier_override or verifier,
        expected_nonce=nonce_override or nonce,
    )


class TestPkceAndNonce:
    def test_pkce_pair_is_s256_and_unique(self) -> None:
        import base64
        import hashlib

        v1, c1 = new_pkce_pair()
        v2, _ = new_pkce_pair()
        assert v1 != v2
        assert 43 <= len(v1) <= 128  # RFC 7636 4.1
        expect = base64.urlsafe_b64encode(hashlib.sha256(v1.encode()).digest()).decode().rstrip("=")
        assert c1 == expect

    def test_nonces_are_unique(self) -> None:
        assert len({new_nonce() for _ in range(50)}) == 50


class TestDiscovery:
    async def test_authorization_url_carries_pkce_nonce_state_and_scopes(self) -> None:
        idp = FakeOidcIdp()
        oidc = _settings_for(idp)
        url = await _client(idp).authorization_url(
            oidc,
            client_id="client-123",
            redirect_uri=REDIRECT,
            state="the-state",
            nonce="the-nonce",
            code_challenge="the-challenge",
        )
        parsed = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{idp.issuer}/authorize"
        assert q["response_type"] == "code"
        assert q["client_id"] == "client-123"
        assert q["redirect_uri"] == REDIRECT
        assert q["scope"] == "openid email profile"
        assert q["state"] == "the-state"
        assert q["nonce"] == "the-nonce"
        assert q["code_challenge"] == "the-challenge"
        assert q["code_challenge_method"] == "S256"
        assert "hd" not in q

    async def test_hosted_domain_hint_is_added(self) -> None:
        idp = FakeOidcIdp()
        oidc = _settings_for(idp, hosted_domain="acme.test")
        url = await _client(idp).authorization_url(
            oidc, client_id="c", redirect_uri=REDIRECT, state="s", nonce="n", code_challenge="c"
        )
        assert parse_qs(urlparse(url).query)["hd"] == ["acme.test"]

    async def test_existing_authorize_query_params_are_preserved(self) -> None:
        idp = FakeOidcIdp()
        client = _client(idp)
        oidc = _settings_for(idp)
        meta = await client.metadata(oidc)
        sso_oidc._METADATA_CACHE[oidc.discovery_url].value = type(meta)(
            issuer=meta.issuer,
            authorization_endpoint=f"{idp.issuer}/authorize?tenant=abc",
            token_endpoint=meta.token_endpoint,
            jwks_uri=meta.jwks_uri,
        )
        url = await client.authorization_url(
            oidc, client_id="c", redirect_uri=REDIRECT, state="s", nonce="n", code_challenge="c"
        )
        q = parse_qs(urlparse(url).query)
        assert q["tenant"] == ["abc"]
        assert q["state"] == ["s"]

    async def test_metadata_is_cached_within_ttl_and_refetched_after(self) -> None:
        idp = FakeOidcIdp()
        now = [1000.0]
        client = _client(idp, clock=lambda: now[0])
        oidc = _settings_for(idp)
        await client.metadata(oidc)
        await client.metadata(oidc)
        assert idp.discovery_calls == 1
        now[0] += 301
        await client.metadata(oidc)
        assert idp.discovery_calls == 2

    async def test_issuer_mismatch_in_discovery_is_rejected(self) -> None:
        idp = FakeOidcIdp(discovery_issuer_override="https://evil.example.com")
        with pytest.raises(SsoProtocolError) as exc:
            await _client(idp).metadata(_settings_for(idp))
        assert exc.value.code == "oidc_issuer_mismatch"

    @pytest.mark.parametrize("missing", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
    async def test_missing_endpoint_is_rejected(self, missing: str) -> None:
        idp = FakeOidcIdp(omit_endpoints=(missing,))
        with pytest.raises(SsoProtocolError) as exc:
            await _client(idp).metadata(_settings_for(idp))
        assert exc.value.code == "oidc_metadata_invalid"

    async def test_non_https_endpoints_are_rejected(self) -> None:
        import httpx

        idp = FakeOidcIdp()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "issuer": idp.issuer,
                    "authorization_endpoint": "http://idp.example.com/authorize",
                    "token_endpoint": f"{idp.issuer}/token",
                    "jwks_uri": f"{idp.issuer}/jwks",
                },
            )

        s = SsoSettings()
        client = OidcClient(SsoHttp(s, protocol="oidc", transport=httpx.MockTransport(handler)), s)
        with pytest.raises(SsoProtocolError) as exc:
            await client.metadata(_settings_for(idp))
        assert exc.value.code == "oidc_metadata_insecure"

    async def test_non_object_discovery_is_rejected(self) -> None:
        import httpx

        idp = FakeOidcIdp()
        s = SsoSettings()
        client = OidcClient(
            SsoHttp(
                s,
                protocol="oidc",
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[1])),
            ),
            s,
        )
        with pytest.raises(SsoProtocolError):
            await client.metadata(_settings_for(idp))

    async def test_unreachable_discovery_is_idp_unavailable(self) -> None:
        import httpx

        idp = FakeOidcIdp()
        s = SsoSettings()
        client = OidcClient(
            SsoHttp(
                s, protocol="oidc", transport=httpx.MockTransport(lambda r: httpx.Response(503))
            ),
            s,
        )
        with pytest.raises(SsoIdpUnavailableError):
            await client.metadata(_settings_for(idp))

    async def test_ssrf_guard_applies_to_the_discovery_url(self) -> None:
        idp = FakeOidcIdp(issuer="https://10.0.0.9")
        with pytest.raises(SsoConfigError) as exc:
            await _client(idp).metadata(_settings_for(idp))
        assert exc.value.code == "idp_url_blocked"


class TestHappyPath:
    async def test_full_code_exchange_returns_the_vouched_identity(self) -> None:
        idp = FakeOidcIdp()
        identity = await _login(idp, _settings_for(idp))
        assert identity.subject == "idp-subject-1"
        assert identity.email == "alice@acme.test"
        assert identity.email_verified is True
        assert identity.display_name == "Alice Example"
        assert "alice" not in repr(identity)  # PII-safe repr
        assert idp.token_calls == 1

    async def test_client_secret_basic_when_idp_supports_it(self) -> None:
        idp = FakeOidcIdp(advertised_auth_methods=["client_secret_basic", "client_secret_post"])
        await _login(idp, _settings_for(idp))
        token_req = [r for r in idp.requests if r.url.path == "/token"][0]
        assert token_req.headers["authorization"].startswith("Basic ")
        assert b"client_secret" not in token_req.content

    async def test_client_secret_post_when_idp_only_supports_it(self) -> None:
        idp = FakeOidcIdp(advertised_auth_methods=["client_secret_post"])
        await _login(idp, _settings_for(idp))
        token_req = [r for r in idp.requests if r.url.path == "/token"][0]
        assert "authorization" not in token_req.headers
        assert b"client_secret=" in token_req.content

    async def test_public_client_sends_client_id_and_relies_on_pkce(self) -> None:
        idp = FakeOidcIdp(client_secret=None)
        await _login(idp, _settings_for(idp), secret=None)
        token_req = [r for r in idp.requests if r.url.path == "/token"][0]
        assert "authorization" not in token_req.headers
        assert b"client_id=client-123" in token_req.content

    async def test_pkce_is_actually_enforced_by_the_idp(self) -> None:
        idp = FakeOidcIdp()
        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _login(idp, _settings_for(idp), verifier_override="x" * 50)
        assert "invalid_grant" in exc.value.message

    async def test_wrong_client_secret_is_rejected_by_the_idp(self) -> None:
        idp = FakeOidcIdp()
        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _login(idp, _settings_for(idp), secret="wrong")
        assert "invalid_client" in exc.value.message

    async def test_missing_or_blank_optional_claims(self) -> None:
        idp = FakeOidcIdp()
        identity = await _login(idp, _settings_for(idp), email=None, name=None, email_verified=None)
        assert identity.email is None
        assert identity.display_name is None
        assert identity.email_verified is False

    async def test_email_is_normalised_and_string_true_is_accepted(self) -> None:
        idp = FakeOidcIdp()
        identity = await _login(
            idp, _settings_for(idp), email="  Alice@ACME.test ", email_verified="true"
        )
        assert identity.email == "alice@acme.test"
        assert identity.email_verified is True

    async def test_string_false_is_not_verified(self) -> None:
        idp = FakeOidcIdp()
        identity = await _login(idp, _settings_for(idp), email_verified="false")
        assert identity.email_verified is False

    async def test_hosted_domain_claim_is_surfaced_lowercased(self) -> None:
        idp = FakeOidcIdp()
        identity = await _login(idp, _settings_for(idp), hd="ACME.test")
        assert identity.hosted_domain == "acme.test"


class TestIdTokenValidation:
    async def _expect(self, idp: FakeOidcIdp, code: str, **kw: Any) -> SsoProtocolError:
        with pytest.raises(SsoProtocolError) as exc:
            await _login(idp, _settings_for(idp), **kw)
        assert exc.value.code == code, exc.value.message
        return exc.value

    async def test_wrong_issuer(self) -> None:
        await self._expect(
            FakeOidcIdp(claims={"iss": "https://evil.example.com"}), "oidc_id_token_invalid"
        )

    async def test_wrong_audience(self) -> None:
        await self._expect(FakeOidcIdp(claims={"aud": "someone-else"}), "oidc_id_token_invalid")

    async def test_expired_token(self) -> None:
        idp = FakeOidcIdp(id_token_ttl=-3600)
        await self._expect(idp, "oidc_id_token_invalid")

    async def test_token_issued_in_the_future_beyond_skew(self) -> None:
        idp = FakeOidcIdp(iat_offset=3600, id_token_ttl=7200)
        await self._expect(idp, "oidc_id_token_invalid")

    async def test_clock_skew_within_tolerance_is_accepted(self) -> None:
        idp = FakeOidcIdp(iat_offset=60)
        await _login(idp, _settings_for(idp))

    @pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub"])
    async def test_required_claim_missing(self, claim: str) -> None:
        await self._expect(FakeOidcIdp(claims={claim: None}), "oidc_id_token_invalid")

    async def test_nonce_mismatch_is_rejected(self) -> None:
        await self._expect(FakeOidcIdp(), "oidc_nonce_mismatch", nonce_override="a-different-nonce")

    async def test_missing_nonce_claim_is_rejected(self) -> None:
        await self._expect(FakeOidcIdp(claims={"nonce": None}), "oidc_nonce_mismatch")

    async def test_alg_none_is_rejected(self) -> None:
        await self._expect(FakeOidcIdp(alg="none"), "oidc_alg_rejected")

    async def test_hmac_alg_confusion_is_rejected(self) -> None:
        await self._expect(FakeOidcIdp(alg="HS256"), "oidc_alg_rejected")

    async def test_token_signed_by_an_attacker_key_is_rejected(self) -> None:
        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        await self._expect(FakeOidcIdp(signing_key_override=attacker), "oidc_id_token_invalid")

    async def test_unknown_kid_is_rejected(self) -> None:
        idp = FakeOidcIdp()
        # Sign with the known key but advertise a kid the JWKS does not have.
        idp.kid = "rotated-away"
        idp.jwks_keys_override = [idp.public_jwk(idp.key, "kid-1")]
        await self._expect(idp, "oidc_unknown_kid")

    async def test_no_kid_with_multiple_keys_is_ambiguous(self) -> None:
        idp = FakeOidcIdp(
            include_kid=False, extra_keys=[("kid-2", rsa.generate_private_key(65537, 2048))]
        )
        await self._expect(idp, "oidc_unknown_kid")

    async def test_no_kid_with_single_key_is_accepted(self) -> None:
        idp = FakeOidcIdp(include_kid=False)
        await _login(idp, _settings_for(idp))

    async def test_multi_audience_requires_matching_azp(self) -> None:
        idp = FakeOidcIdp(claims={"aud": ["client-123", "other"], "azp": "other"})
        await self._expect(idp, "oidc_azp_mismatch")

    async def test_multi_audience_with_correct_azp_is_accepted(self) -> None:
        idp = FakeOidcIdp(claims={"aud": ["client-123", "other"], "azp": "client-123"})
        await _login(idp, _settings_for(idp))

    async def test_multi_audience_without_azp_is_rejected(self) -> None:
        await self._expect(
            FakeOidcIdp(claims={"aud": ["client-123", "other"]}), "oidc_azp_mismatch"
        )

    async def test_single_audience_with_foreign_azp_is_rejected(self) -> None:
        await self._expect(FakeOidcIdp(claims={"azp": "other"}), "oidc_azp_mismatch")

    @pytest.mark.parametrize("sub", ["", "s" * 256])
    async def test_empty_or_oversized_subject(self, sub: str) -> None:
        await self._expect(FakeOidcIdp(claims={"sub": sub}), "oidc_bad_subject")

    async def test_no_id_token_in_response(self) -> None:
        idp = FakeOidcIdp(token_response_override=(200, {"access_token": "x"}))
        await self._expect(idp, "oidc_no_id_token")

    async def test_non_object_token_response(self) -> None:
        idp = FakeOidcIdp(token_response_override=(200, []))  # type: ignore[arg-type]
        await self._expect(idp, "oidc_token_invalid")

    async def test_malformed_id_token(self) -> None:
        idp = FakeOidcIdp(token_response_override=(200, {"id_token": "not.a.jwt"}))
        await self._expect(idp, "oidc_id_token_malformed")

    async def test_error_text_from_pyjwt_is_not_echoed(self) -> None:
        err = await self._expect(
            FakeOidcIdp(claims={"aud": "sensitive-aud-value"}), "oidc_id_token_invalid"
        )
        assert "sensitive-aud-value" not in err.message


class TestJwks:
    async def test_jwks_is_cached_and_reused_across_logins(self) -> None:
        idp = FakeOidcIdp()
        client = _client(idp)
        oidc = _settings_for(idp)
        await _login(idp, oidc, client=client)
        await _login(idp, oidc, client=client)
        assert idp.jwks_calls == 1

    async def test_key_rotation_triggers_one_refetch(self) -> None:
        idp = FakeOidcIdp()
        now = [1000.0]
        client = _client(idp, clock=lambda: now[0])
        oidc = _settings_for(idp)
        await _login(idp, oidc, client=client)
        # IdP rotates: new key + kid, old one retired from the JWKS.
        new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        idp.key, idp.kid = new_key, "kid-2"
        now[0] += 60  # past the 30 s refetch floor
        await _login(idp, oidc, client=client)
        assert idp.jwks_calls == 2

    async def test_unknown_kid_refetch_is_rate_limited(self) -> None:
        idp = FakeOidcIdp()
        now = [1000.0]
        client = _client(idp, clock=lambda: now[0])
        oidc = _settings_for(idp)
        await _login(idp, oidc, client=client)
        idp.kid = "never-published"
        for _ in range(3):
            with pytest.raises(SsoProtocolError):
                await _login(idp, oidc, client=client)
        assert idp.jwks_calls == 1  # an attacker spraying bad kids cannot hammer the IdP

    @pytest.mark.parametrize(
        "bad_doc",
        [[], {"nokeys": 1}, {"keys": []}, {"keys": [{"kty": "oct", "k": "AAAA"}]}, {"keys": ["x"]}],
    )
    async def test_unusable_jwks_documents(self, bad_doc: Any) -> None:
        idp = FakeOidcIdp(jwks_document_override=bad_doc)
        with pytest.raises(SsoProtocolError) as exc:
            await _login(idp, _settings_for(idp))
        assert exc.value.code == "oidc_jwks_invalid"

    async def test_symmetric_and_encryption_keys_are_skipped(self) -> None:
        idp = FakeOidcIdp()
        good = idp.public_jwk(idp.key, "kid-1")
        idp.jwks_keys_override = [
            {"kty": "oct", "kid": "kid-1", "k": "AAAA"},
            {**good, "use": "enc", "kid": "enc-key"},
            {"kty": "RSA", "kid": "broken", "n": "x", "e": "y"},
            good,
        ]
        await _login(idp, _settings_for(idp))


class TestGoogle:
    async def test_google_accepts_both_documented_issuer_spellings(self) -> None:
        for iss in (GOOGLE_ISSUER, "accounts.google.com"):
            idp = FakeOidcIdp(issuer=GOOGLE_ISSUER, claims={"iss": iss, "hd": "acme.test"})
            oidc = OidcSettings(
                issuer=GOOGLE_ISSUER,
                client_id=idp.client_id,
                discovery_url=idp.discovery_url,
                hosted_domain="acme.test",
            )
            identity = await _login(idp, oidc, protocol=PROTOCOL_GOOGLE)
            assert identity.hosted_domain == "acme.test"
            sso_oidc.clear_caches()

    async def test_generic_oidc_rejects_the_other_spelling(self) -> None:
        idp = FakeOidcIdp(claims={"iss": "idp.example.com"})
        with pytest.raises(SsoProtocolError):
            await _login(idp, _settings_for(idp))

    def test_google_constants(self) -> None:
        assert GOOGLE_DISCOVERY_URL == f"{GOOGLE_ISSUER}/.well-known/openid-configuration"


class TestSettingsValidation:
    def test_valid(self) -> None:
        sso_oidc.validate_oidc_settings(
            OidcSettings(issuer="https://i", client_id="c", discovery_url="https://i/.well-known")
        )

    def test_platform_client_needs_no_client_id(self) -> None:
        sso_oidc.validate_oidc_settings(
            OidcSettings(
                issuer="https://i", client_id="", discovery_url="d", use_platform_client=True
            )
        )

    @pytest.mark.parametrize(
        "kw",
        [
            {"issuer": "", "client_id": "c"},
            {"issuer": "https://i", "client_id": ""},
            {"issuer": "https://i", "client_id": "c", "scopes": ("email",)},
        ],
    )
    def test_invalid(self, kw: dict[str, Any]) -> None:
        with pytest.raises(SsoConfigError):
            sso_oidc.validate_oidc_settings(OidcSettings(discovery_url="d", **kw))


def test_allowed_algorithms_never_include_symmetric_or_none() -> None:
    assert not [a for a in sso_oidc.ALLOWED_ID_TOKEN_ALGS if a.startswith("HS") or a == "none"]
    assert "RS256" in sso_oidc.ALLOWED_ID_TOKEN_ALGS
    assert jwt.get_algorithm_by_name("RS256") is not None
