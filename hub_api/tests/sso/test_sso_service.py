"""`services/sso_service.py` edge cases, defensive branches and race handling.

The API/flow suites cover the main behaviour through HTTP; these drive the service layer
directly for the paths HTTP cannot easily reach (corrupt rows, lost races, vanished users,
audit-sink failure, impossible-by-constraint states) -- each of which must fail LOUDLY.
"""

from __future__ import annotations

import logging
import types
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from services import sso_crypto, sso_service, sso_state
from services.errors import ApiError
from services.sso_service import ConnectionInput, SsoContext, TenantRef
from services.sso_types import (
    PROTOCOL_GOOGLE,
    PROTOCOL_OIDC,
    PROTOCOL_SAML,
    ExternalIdentity,
    OidcSettings,
    SamlSettings,
    SsoConfigError,
    SsoConnection,
    SsoPolicyError,
    SsoProtocolError,
    flag_for_protocol,
)
from tests.conftest import TENANT_SLUG
from tests.sso.conftest import FakeRedis, hub_config
from tests.sso.kit import Kit


@pytest.fixture
def ctx(sso_db: Any, sso_settings: Any, entitlements: Any) -> SsoContext:
    return SsoContext(
        install_dal=sso_db.install_dal,
        async_dal=sso_db,
        dal=sso_db.dal,
        cfg=hub_config(),
        settings=sso_settings,
    )


@pytest.fixture
def tenant() -> TenantRef:
    return TenantRef(id=1, slug=TENANT_SLUG, is_active=True)


def _oidc_input(**kw: Any) -> ConnectionInput:
    base: dict[str, Any] = {
        "display_name": "Svc",
        "allowed_domains": ["acme.test"],
        "issuer": "https://idp.example.com",
        "client_id": "cid",
        "client_secret": "sec",
        "enabled": True,
    }
    base.update(kw)
    return ConnectionInput(**base)


class TestFlagMapping:
    def test_protocol_to_flag(self) -> None:
        assert flag_for_protocol("saml") == "waddles.auth.sso_saml"
        assert flag_for_protocol("oidc") == "waddles.auth.sso_saml"
        assert flag_for_protocol("google") == "waddles.auth.sso_google"

    def test_unknown_protocol_is_a_loud_error(self) -> None:
        with pytest.raises(SsoConfigError) as exc:
            flag_for_protocol("ldap")
        assert exc.value.code == "unknown_protocol"


class TestRowMapping:
    def _row(self, **kw: Any) -> Any:
        base: dict[str, Any] = {
            "id": 1,
            "public_id": "p",
            "tenant_id": 1,
            "protocol": "oidc",
            "display_name": "n",
            "enabled": True,
            "config": {},
            "secret_ciphertext": None,
            "created_at": None,
            "updated_at": None,
        }
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_unknown_protocol_row_fails_loudly(self) -> None:
        with pytest.raises(SsoConfigError) as exc:
            sso_service._row_to_connection(self._row(protocol="ldap"))
        assert exc.value.code == "unknown_protocol"

    def test_non_dict_config_degrades_to_defaults_not_a_crash(self) -> None:
        conn = sso_service._row_to_connection(self._row(config="garbage"))
        assert conn.oidc is not None
        assert conn.oidc.issuer == ""
        assert conn.oidc.scopes == ("openid", "email", "profile")
        assert conn.allowed_domains == ()

    def test_saml_row(self) -> None:
        conn = sso_service._row_to_connection(
            self._row(protocol="saml", config={"idp_entity_id": "e", "allowed_domains": ["a.test"]})
        )
        assert conn.saml is not None
        assert conn.saml.idp_entity_id == "e"
        assert conn.allowed_domains == ("a.test",)

    def test_connection_without_either_body_has_no_domains(self) -> None:
        bare = SsoConnection(1, "p", 1, "oidc", "n", True, None, None)
        assert bare.allowed_domains == ()
        with pytest.raises(SsoConfigError):
            sso_service._oidc_of(bare)
        with pytest.raises(SsoConfigError):
            sso_service._saml_of(bare)
        with pytest.raises(SsoConfigError):
            sso_service._settings_to_config("oidc", None, None)

    def test_secret_ciphertext_never_appears_in_repr(self) -> None:
        conn = SsoConnection(
            1, "p", 1, "oidc", "n", True, None, None, secret_ciphertext="v1:SECRETBLOB"
        )
        assert "SECRETBLOB" not in repr(conn)


class TestInputCleaning:
    def test_required_text_variants(self) -> None:
        with pytest.raises(ApiError):
            sso_service._require_text(None, field_name="f", max_len=5)
        with pytest.raises(ApiError):
            sso_service._require_text("   ", field_name="f", max_len=5)
        with pytest.raises(ApiError):
            sso_service._require_text("toolong", field_name="f", max_len=5)
        assert sso_service._clean_text(None, field_name="f", max_len=5) is None
        assert sso_service._clean_text("  ", field_name="f", max_len=5) is None
        assert sso_service._clean_text(" ok ", field_name="f", max_len=5) == "ok"

    def test_too_many_scopes(self) -> None:
        with pytest.raises(ApiError):
            sso_service._clean_scopes(["openid"] + [f"s{i}" for i in range(25)])

    def test_domain_normalisation(self) -> None:
        assert sso_service._clean_domains(
            [" @ACME.test ", "acme.test", "B.example"], protocol="oidc"
        ) == (
            "acme.test",
            "b.example",
        )


class TestCrudEdges:
    async def test_unknown_protocol_is_a_400_before_any_entitlement_work(
        self, ctx: SsoContext, tenant: TenantRef
    ) -> None:
        with pytest.raises(ApiError) as exc:
            await sso_service.create_connection(
                ctx, tenant=tenant, actor_id=1, protocol="ldap", data=_oidc_input()
            )
        assert exc.value.status_code == 400

    async def test_non_conflict_insert_errors_are_re_raised_not_mislabelled(
        self, ctx: SsoContext, tenant: TenantRef, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = ctx.install_dal.sso_connections

        async def boom(**_kw: Any) -> None:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(type(table), "async_insert", lambda self, **kw: boom(**kw))
        with pytest.raises(RuntimeError):
            await sso_service.create_connection(
                ctx, tenant=tenant, actor_id=1, protocol=PROTOCOL_OIDC, data=_oidc_input()
            )

    async def test_audit_sink_failure_is_logged_loudly_and_never_aborts_the_change(
        self, ctx: SsoContext, tenant: TenantRef, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with ctx.install_dal.engine.begin() as conn:
            await conn.execute(text("DROP TABLE audit_log"))
        await ctx.install_dal.reflect()
        with caplog.at_level(logging.ERROR):
            created = await sso_service.create_connection(
                ctx, tenant=tenant, actor_id=1, protocol=PROTOCOL_OIDC, data=_oidc_input()
            )
        assert created.display_name == "Svc"  # the change itself succeeded
        assert "sso.audit.write_failed" in caplog.text  # ...and the sink failure is visible
        assert "err_type=" in caplog.text
        assert "File " in caplog.text  # frame-only traceback present

    async def test_updating_a_google_connection_to_platform_client_without_support(
        self, ctx: SsoContext, tenant: TenantRef
    ) -> None:
        conn = await sso_service.create_connection(
            ctx,
            tenant=tenant,
            actor_id=1,
            protocol=PROTOCOL_GOOGLE,
            data=ConnectionInput(
                display_name="G",
                allowed_domains=["acme.test"],
                client_id="c",
                client_secret="s",
                enabled=True,
            ),
        )
        with pytest.raises(ApiError) as exc:
            await sso_service.update_connection(
                ctx,
                tenant=tenant,
                actor_id=1,
                public_id=conn.public_id,
                patch=ConnectionInput(use_platform_client=True),
            )
        assert exc.value.status_code == 422  # no shared client configured


class TestLoginResolution:
    async def test_missing_tenant_makes_the_connection_unusable(
        self, ctx: SsoContext, tenant: TenantRef, sso_db: Any
    ) -> None:
        conn = await sso_service.create_connection(
            ctx, tenant=tenant, actor_id=1, protocol=PROTOCOL_OIDC, data=_oidc_input()
        )
        await sso_db.delete_async(sso_db.dal.tenants.slug == TENANT_SLUG)
        with pytest.raises(SsoPolicyError) as exc:
            await sso_service._resolve_loginable(ctx, conn.public_id)
        assert exc.value.code == "tenant_unavailable"

    async def test_options_for_an_inactive_tenant_is_empty(
        self, ctx: SsoContext, sso_db: Any
    ) -> None:
        await sso_db.update_async(sso_db.dal.tenants.slug == TENANT_SLUG, is_active=False)
        assert await sso_service.list_login_options(ctx, TENANT_SLUG) == []

    async def test_completing_the_wrong_protocol_fails_loudly(
        self, ctx: SsoContext, tenant: TenantRef
    ) -> None:
        saml = await sso_service.create_connection(
            ctx,
            tenant=tenant,
            actor_id=1,
            protocol=PROTOCOL_SAML,
            data=ConnectionInput(display_name="S", allowed_domains=["acme.test"]),
        )
        await sso_service.create_connection(
            ctx,
            tenant=tenant,
            actor_id=1,
            protocol=PROTOCOL_OIDC,
            data=ConnectionInput(display_name="O", allowed_domains=["acme.test"]),
        )
        # drafts are not loginable at all:
        with pytest.raises(SsoPolicyError):
            await sso_service.begin_login(ctx, saml.public_id)

    @pytest.mark.parametrize("flow", ["oidc", "saml"])
    async def test_incomplete_state_is_refused(
        self, ctx: SsoContext, tenant: TenantRef, fake_redis: FakeRedis, flow: str, sso_app: Any
    ) -> None:
        kit_body = (
            _oidc_input()
            if flow == "oidc"
            else ConnectionInput(
                display_name="S",
                allowed_domains=["acme.test"],
                enabled=True,
                idp_entity_id="https://i",
                idp_sso_url="https://saml-idp.example.com/sso",
                idp_certificates=[_cert()],
            )
        )
        conn = await sso_service.create_connection(
            ctx, tenant=tenant, actor_id=1, protocol=flow, data=kit_body
        )
        async with sso_app.app_context():
            state = await sso_state.create_state(
                sso_state.SsoStatePayload(connection_public_id=conn.public_id, protocol=flow),
                ttl_s=60,
            )
            binder = sso_crypto.binder_value(state)
            with pytest.raises(SsoProtocolError) as exc:
                if flow == "oidc":
                    await sso_service.complete_oidc(
                        ctx, conn.public_id, code="c", state=state, binder_cookie=binder
                    )
                else:
                    await sso_service.complete_saml(
                        ctx,
                        conn.public_id,
                        saml_response="x",
                        relay_state=state,
                        binder_cookie=binder,
                    )
        assert exc.value.code == "state_incomplete"

    async def test_wrong_protocol_completion(
        self, ctx: SsoContext, tenant: TenantRef, sso_app: Any
    ) -> None:
        oidc = await sso_service.create_connection(
            ctx, tenant=tenant, actor_id=1, protocol=PROTOCOL_OIDC, data=_oidc_input()
        )
        async with sso_app.app_context():
            with pytest.raises(SsoConfigError) as exc:
                await sso_service.complete_saml(
                    ctx, oidc.public_id, saml_response="x", relay_state="y", binder_cookie=None
                )
        assert exc.value.code == "wrong_protocol"


def _cert() -> str:
    from tests.sso.idp_fakes import FakeSamlIdp

    return FakeSamlIdp().cert_pem


class TestCrashHandling:
    async def test_unexpected_exception_is_logged_with_type_and_frames_then_propagates(
        self,
        kit: Kit,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        public_id = await kit.create(kit.oidc_body())

        # Built at runtime: the traceback echoes SOURCE lines (code, not data), so the
        # PII-looking text must not appear literally in any source line.
        leaked = "alice@" + "acme" + ".test leaked in a driver message"

        async def explode(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError(leaked)

        monkeypatch.setattr(sso_service, "_finish_login", explode)
        with caplog.at_level(logging.ERROR):
            response = await kit.oidc_login(public_id)
        # The browser gets the generic reason, never the exception.
        assert kit.error_reason(response) == "sso_unavailable"
        assert "sso.login.crashed" in caplog.text
        assert "err_type=RuntimeError" in caplog.text
        assert "File " in caplog.text
        assert leaked not in caplog.text  # exception TEXT is withheld

    async def test_saml_crash_path(
        self, kit: Kit, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        public_id = await kit.create(kit.saml_body())

        leaked = "secret" + " detail" + " 12345"

        async def explode(*_a: Any, **_k: Any) -> Any:
            raise ValueError(leaked)

        monkeypatch.setattr(sso_service, "_finish_login", explode)
        with caplog.at_level(logging.ERROR):
            response = await kit.saml_login(public_id)
        assert kit.error_reason(response) == "sso_unavailable"
        assert "err_type=ValueError" in caplog.text
        assert leaked not in caplog.text


class TestIdentityRaces:
    """A first-login race on (connection, subject): exactly one user must survive."""

    async def _setup(self, kit: Kit) -> tuple[SsoContext, Any, ExternalIdentity]:
        public_id = await kit.create(kit.oidc_body())
        ctx = SsoContext(
            install_dal=kit.db.install_dal,
            async_dal=kit.db,
            dal=kit.db.dal,
            cfg=hub_config(),
            settings=kit.app.config["sso_settings"],
        )
        conn = await sso_service.load_connection(ctx, public_id)
        identity = ExternalIdentity(
            subject="race-subject", email="race@acme.test", email_verified=True
        )
        return ctx, conn, identity

    async def test_loser_adopts_the_winners_user_and_discards_its_own(
        self, kit: Kit, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx, conn, identity = await self._setup(kit)
        winner_id = await kit.db.insert_async(
            kit.db.dal.hub_users,
            email="winner@acme.test",
            username=None,
            is_active=True,
            email_verified=True,
        )
        await ctx.install_dal.sso_identities.async_insert(
            connection_id=conn.id,
            subject="race-subject",
            hub_user_id=int(winner_id),
            created_at=datetime.now(UTC),
        )
        real_find = sso_service._find_identity_link
        calls = {"n": 0}

        async def blind_first(c: SsoContext, conn_id: int, subject: str) -> Any:
            calls["n"] += 1
            return None if calls["n"] == 1 else await real_find(c, conn_id, subject)

        monkeypatch.setattr(sso_service, "_find_identity_link", blind_first)
        with caplog.at_level(logging.WARNING):
            row, created = await sso_service._resolve_user(ctx, conn, identity)
        assert created is False
        assert int(row.id) == int(winner_id)
        assert await kit.users_by_email("race@acme.test") == []  # the loser's orphan was removed
        assert "sso.login.identity_race" in caplog.text

    async def test_insert_failure_without_a_winner_propagates_and_leaves_no_orphan(
        self, kit: Kit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, conn, identity = await self._setup(kit)

        async def failing_insert(self: Any, **_kw: Any) -> Any:
            raise IntegrityError("stmt", {}, Exception("fk"))

        monkeypatch.setattr(type(ctx.install_dal.sso_identities), "async_insert", failing_insert)
        with pytest.raises(IntegrityError):
            await sso_service._resolve_user(ctx, conn, identity)
        assert await kit.users_by_email("race@acme.test") == []

    async def test_link_pointing_at_a_deleted_user_is_a_loud_error(self, kit: Kit) -> None:
        ctx, conn, _ = await self._setup(kit)
        link = types.SimpleNamespace(id=1, hub_user_id=987654)
        with pytest.raises(SsoConfigError) as exc:
            await sso_service._existing_user(ctx, conn, link)
        assert exc.value.code == "identity_orphaned"

    async def test_created_user_that_cannot_be_read_back_is_a_loud_error(
        self, kit: Kit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, conn, identity = await self._setup(kit)

        async def nothing(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(sso_service, "_user_row", nothing)
        with pytest.raises(SsoConfigError) as exc:
            await sso_service._resolve_user(ctx, conn, identity)
        assert exc.value.code == "user_vanished"


class TestDomainPolicyHelpers:
    def test_oidc_and_saml_settings_domain_resolution(self) -> None:
        oidc = SsoConnection(
            1,
            "p",
            1,
            "oidc",
            "n",
            True,
            OidcSettings(issuer="i", client_id="c", discovery_url="d", allowed_domains=("a.test",)),
            None,
        )
        saml = SsoConnection(
            1,
            "p",
            1,
            "saml",
            "n",
            True,
            None,
            SamlSettings(
                idp_entity_id="e", idp_sso_url="u", idp_certs_pem=(), allowed_domains=("b.test",)
            ),
        )
        assert oidc.allowed_domains == ("a.test",)
        assert saml.allowed_domains == ("b.test",)

    def test_email_domain_helper(self) -> None:
        assert sso_service._email_domain("Bob@ACME.test") == "acme.test"
