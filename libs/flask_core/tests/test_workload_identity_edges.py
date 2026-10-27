"""Edge/failure-path tests for flask_core.workload_identity (parsing, posture, refresh, JWT-SVID).

Complements test_workload_identity.py's peer-authorization tests with the fail-safe
branches: malformed SPIFFE IDs, production-vs-dev posture, refresh semantics, and the
Real Workload API adapter's loud refusal while the SDK is unwired.
"""

import pytest

from flask_core.workload_identity import (
    Degraded,
    IdentityProvider,
    JwtSvid,
    JwtSvidError,
    NoDefaultSvid,
    PeerNotAuthorized,
    RealWorkloadApiSource,
    SpiffeId,
    SpiffeIdError,
    SpiffeIdMatcher,
    TrustBundleSet,
    TrustDomain,
    WorkloadApiSource,
    WorkloadApiUnavailable,
    X509Context,
    X509Svid,
    is_production,
)

TD = "penguintech.io"


def _svid(service="svc-core"):
    return X509Svid(
        spiffe_id=SpiffeId.parse(f"spiffe://{TD}/alpha/{service}"),
        cert_chain_pem=b"cert",
        private_key_pem=b"key",
    )


def _ctx(service="svc-core", bundles=None):
    return X509Context(
        default_svid=_svid(service),
        bundles=bundles if bundles is not None else TrustBundleSet({TD: b"ca"}),
    )


class Source(WorkloadApiSource):
    def __init__(self, ctx=None, jwt=None, error=None):
        self.ctx, self.jwt, self.error = ctx, jwt, error
        self.fetches = 0

    def fetch_x509_context(self):
        self.fetches += 1
        if self.error:
            raise self.error
        return self.ctx

    def fetch_jwt_svid(self, audience):
        if self.error:
            raise self.error
        return self.jwt


class TestSpiffeParsing:
    @pytest.mark.parametrize("name", ["", "a/b", "spiffe://x"])
    def test_invalid_trust_domain(self, name):
        with pytest.raises(SpiffeIdError, match="invalid trust domain"):
            TrustDomain(name)

    def test_trust_domain_str(self):
        assert str(TrustDomain(TD)) == TD

    @pytest.mark.parametrize("uri,fragment", [
        ("https://penguintech.io/x", "not a spiffe:// URI"),
        ("spiffe:///path", "no trust domain"),
        ("spiffe://penguintech.io", "no path"),
        ("", "not a spiffe:// URI"),
    ])
    def test_invalid_spiffe_ids(self, uri, fragment):
        with pytest.raises(SpiffeIdError, match=fragment):
            SpiffeId.parse(uri)

    def test_valid_parse_and_membership(self):
        s = SpiffeId.parse("spiffe://penguintech.io/alpha/svc-core")
        assert s.path == "/alpha/svc-core"
        assert s.is_member_of(TrustDomain(TD)) is True
        assert s.is_member_of(TrustDomain("other.example")) is False


class TestPosture:
    @pytest.mark.parametrize("release_mode,expected", [("false", False), ("FALSE", False), ("true", True), ("", True)])
    def test_release_mode_wins(self, monkeypatch, release_mode, expected):
        monkeypatch.setenv("RELEASE_MODE", release_mode)
        monkeypatch.setenv("WADDLEBOT_ENV", "dev")  # ignored when RELEASE_MODE set
        assert is_production() is expected

    @pytest.mark.parametrize("env_var", ["WADDLEBOT_ENV", "ENVIRONMENT", "NODE_ENV"])
    @pytest.mark.parametrize("value,expected", [("dev", False), ("Local", False), ("test", False),
                                                ("testing", False), ("development", False),
                                                ("prod", True), ("staging", True), ("", True)])
    def test_env_name_downgrades_only_for_dev_like(self, monkeypatch, env_var, value, expected):
        for v in ("RELEASE_MODE", "WADDLEBOT_ENV", "ENVIRONMENT", "NODE_ENV"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv(env_var, value)
        assert is_production() is expected

    def test_unset_environment_fails_closed(self, monkeypatch):
        for v in ("RELEASE_MODE", "WADDLEBOT_ENV", "ENVIRONMENT", "NODE_ENV"):
            monkeypatch.delenv(v, raising=False)
        assert is_production() is True


class TestRealWorkloadApiSource:
    def test_connect_env_requires_socket(self, monkeypatch):
        monkeypatch.delenv("SPIFFE_ENDPOINT_SOCKET", raising=False)
        with pytest.raises(WorkloadApiUnavailable, match="SPIFFE_ENDPOINT_SOCKET is not set"):
            RealWorkloadApiSource.connect_env()

    def test_unwired_sdk_refuses_loudly(self, monkeypatch):
        monkeypatch.setenv("SPIFFE_ENDPOINT_SOCKET", "unix:///tmp/agent.sock")
        src = RealWorkloadApiSource.connect_env()
        with pytest.raises(WorkloadApiUnavailable, match="not wired"):
            src.fetch_x509_context()
        with pytest.raises(WorkloadApiUnavailable, match="not wired"):
            src.fetch_jwt_svid("aud")

    def test_prod_connect_without_socket_hard_fails(self, monkeypatch):
        monkeypatch.delenv("SPIFFE_ENDPOINT_SOCKET", raising=False)
        with pytest.raises(WorkloadApiUnavailable, match="mandatory in production"):
            IdentityProvider.connect(require=True)

    def test_dev_connect_without_socket_degrades(self, monkeypatch):
        monkeypatch.delenv("SPIFFE_ENDPOINT_SOCKET", raising=False)
        p = IdentityProvider.connect(require=False)
        assert p.has_identity() is False
        with pytest.raises(Degraded):
            p.spiffe_id()


class TestConnectPolicy:
    def test_no_default_svid_is_failure_in_prod_and_degrade_in_dev(self):
        empty = Source(ctx=X509Context(default_svid=None, bundles=TrustBundleSet({TD: b"ca"})))
        with pytest.raises(WorkloadApiUnavailable):
            IdentityProvider.connect(source=empty, require=True)
        assert IdentityProvider.connect(source=empty, require=False).has_identity() is False

    def test_connect_holds_identity(self):
        p = IdentityProvider.connect(source=Source(ctx=_ctx()), require=True)
        assert p.has_identity() and str(p.spiffe_id()).endswith("/alpha/svc-core")

    def test_connect_uses_environment_posture_when_require_unset(self, monkeypatch):
        monkeypatch.setenv("RELEASE_MODE", "false")
        monkeypatch.delenv("SPIFFE_ENDPOINT_SOCKET", raising=False)
        assert IdentityProvider.connect().has_identity() is False
        monkeypatch.setenv("RELEASE_MODE", "true")
        with pytest.raises(WorkloadApiUnavailable):
            IdentityProvider.connect()


class TestRefresh:
    def test_refresh_replaces_identity(self):
        src = Source(ctx=_ctx("svc-a"))
        p = IdentityProvider.connect(source=src, require=True)
        src.ctx = _ctx("svc-b")
        p.refresh()
        assert str(p.spiffe_id()).endswith("/svc-b")

    def test_refresh_without_source_raises_degraded(self):
        p = IdentityProvider.connect(require=False)
        with pytest.raises(Degraded, match="cannot refresh"):
            p.refresh()

    def test_failed_refresh_in_production_raises_but_keeps_prior_identity(self, monkeypatch):
        monkeypatch.setenv("RELEASE_MODE", "true")
        src = Source(ctx=_ctx("svc-a"))
        p = IdentityProvider.connect(source=src, require=True)
        src.error = ConnectionError("agent gone")
        with pytest.raises(WorkloadApiUnavailable, match="refresh failed in production"):
            p.refresh()
        assert str(p.spiffe_id()).endswith("/svc-a")

    def test_failed_refresh_in_dev_warns_and_keeps_prior_identity(self, monkeypatch):
        monkeypatch.setenv("RELEASE_MODE", "false")
        src = Source(ctx=_ctx("svc-a"))
        p = IdentityProvider.connect(source=src, require=True)
        src.error = ConnectionError("agent gone")
        p.refresh()  # must not raise
        assert str(p.spiffe_id()).endswith("/svc-a")

    def test_refresh_that_loses_default_svid_is_normalised(self, monkeypatch):
        monkeypatch.setenv("RELEASE_MODE", "true")
        src = Source(ctx=_ctx())
        p = IdentityProvider.connect(source=src, require=True)
        src.ctx = X509Context(default_svid=None, bundles=TrustBundleSet({TD: b"ca"}))
        with pytest.raises(WorkloadApiUnavailable):
            p.refresh()


class TestJwtSvid:
    def test_degraded_provider_cannot_fetch(self):
        with pytest.raises(Degraded):
            IdentityProvider.connect(require=False).fetch_jwt_svid("aud")

    def test_fetch_passthrough(self):
        jwt = JwtSvid.__new__(JwtSvid)
        p = IdentityProvider.connect(source=Source(ctx=_ctx(), jwt=jwt), require=True)
        assert p.fetch_jwt_svid("aud") is jwt

    def test_sdk_errors_are_wrapped_and_identity_errors_pass_through(self):
        src = Source(ctx=_ctx())
        p = IdentityProvider.connect(source=src, require=True)
        src.error = ConnectionError("socket closed")
        with pytest.raises(JwtSvidError, match="failed to fetch JWT-SVID"):
            p.fetch_jwt_svid("aud")
        src.error = NoDefaultSvid("none issued")
        with pytest.raises(NoDefaultSvid):
            p.fetch_jwt_svid("aud")


class TestTlsConfig:
    def test_degraded_provider_cannot_build_tls(self):
        p = IdentityProvider.connect(require=False)
        with pytest.raises(Degraded):
            p.server_tls_config(SpiffeIdMatcher())
        with pytest.raises(Degraded):
            p.client_tls_config(SpiffeIdMatcher())

    def test_server_vs_client_flag(self):
        p = IdentityProvider.connect(source=Source(ctx=_ctx()), require=True)
        assert p.server_tls_config(SpiffeIdMatcher()).is_server is True
        assert p.client_tls_config(SpiffeIdMatcher()).is_server is False

    def test_empty_trust_bundle_refuses(self):
        from flask_core.workload_identity import EmptyTrustBundle

        p = IdentityProvider.connect(source=Source(ctx=_ctx(bundles=TrustBundleSet({}))), require=True)
        with pytest.raises(EmptyTrustBundle):
            p.server_tls_config(SpiffeIdMatcher())


class TestAuthorizePeerInputs:
    def _cfg(self):
        p = IdentityProvider.connect(source=Source(ctx=_ctx()), require=True)
        return p.server_tls_config(SpiffeIdMatcher().allow_exact(f"spiffe://{TD}/alpha/svc-ingest"))

    def test_no_svid_rejected(self):
        with pytest.raises(PeerNotAuthorized, match="no SVID"):
            self._cfg().authorize_peer(None)

    def test_unparseable_svid_rejected(self):
        with pytest.raises(PeerNotAuthorized, match="unparseable"):
            self._cfg().authorize_peer("https://evil.example/x")

    def test_parsed_spiffe_id_object_accepted(self):
        self._cfg().authorize_peer(SpiffeId.parse(f"spiffe://{TD}/alpha/svc-ingest"))

    def test_other_peer_rejected(self):
        with pytest.raises(PeerNotAuthorized, match="not permitted"):
            self._cfg().authorize_peer(f"spiffe://{TD}/alpha/hub-webui")
