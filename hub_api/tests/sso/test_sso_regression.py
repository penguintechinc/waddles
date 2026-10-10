"""Cross-cutting regression and policy guards for enterprise SSO.

Index of the defect classes pinned here (each was either found while building the
feature or is a standing house rule the feature must never regress on):

* regression: sso-protocol-order  -- an unknown `protocol` reached the entitlement lookup
  and surfaced as a 503 instead of a 400 (found by the admin-API validation matrix).
* regression: sso-pydal-reference -- pydal returns a `Reference` from `insert_async`;
  handing it to penguin-dal raised `TypeError` mid-login (found by the first end-to-end run).
* regression: sso-disable-expired-cert -- switching a connection OFF must work even when its
  stored config no longer validates (expired IdP certificate).
* regression: sso-no-email-adoption -- an IdP-asserted email must never adopt an existing
  global `hub_users` row (cross-tenant account takeover).
* house rules -- PII-free logs, exceptions logged by type + frames (never message text),
  OTel metrics/traces emitted, explicit response DTOs, tier-catalog alignment.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from flask_core.auth import SCOPE_BUNDLES
from flask_core.authz import has_required_scopes
from flask_core.exc_log_audit import audit_paths
from flask_core.tier_catalog import FEATURE_MIN_TIERS

import blueprints.v1.sso as sso_bp
from blueprints.v1.sso import (
    SsoConnectionDTO,
    SsoConnectionListResponse,
    SsoConnectionResponse,
    SsoDeleteResponse,
    SsoOptionDTO,
    SsoOptionsResponse,
    SsoStartResponse,
)
from services import sso_http, sso_saml, sso_service, sso_telemetry
from services.sso_types import (
    FLAG_SSO_ENTERPRISE,
    FLAG_SSO_GOOGLE,
    SCOPE_SSO_ADMIN,
    flag_for_protocol,
)
from tests.sso.conftest import build_sso_metadata
from tests.sso.idp_fakes import FakeOidcIdp
from tests.sso.kit import Kit

HUB_API = Path(__file__).resolve().parents[2]
SSO_SOURCES = sorted([*(HUB_API / "services").glob("sso_*.py"), HUB_API / "blueprints/v1/sso.py"])


class TestTierAndScopeAlignment:
    def test_catalog_maps_the_two_flags_to_the_documented_tiers(self) -> None:
        assert FEATURE_MIN_TIERS["waddles.auth.sso_saml"] == "enterprise"
        assert FEATURE_MIN_TIERS["waddles.auth.sso_google"] == "professional"

    def test_protocols_map_to_the_catalog_flags(self) -> None:
        assert flag_for_protocol("saml") == flag_for_protocol("oidc") == FLAG_SSO_ENTERPRISE
        assert flag_for_protocol("google") == FLAG_SSO_GOOGLE
        assert {FLAG_SSO_ENTERPRISE, FLAG_SSO_GOOGLE} <= set(FEATURE_MIN_TIERS)

    def test_only_the_tenant_admin_bundle_grants_the_sso_scope(self) -> None:
        assert SCOPE_SSO_ADMIN in SCOPE_BUNDLES["tenant"]["admin"]
        for level, bundles in SCOPE_BUNDLES.items():
            for name, scopes in bundles.items():
                if (level, name) != ("tenant", "admin"):
                    assert SCOPE_SSO_ADMIN not in scopes, (level, name)

    def test_global_admin_reaches_it_through_the_action_wildcard(self) -> None:
        granted = frozenset(SCOPE_BUNDLES["global"]["admin"])
        assert has_required_scopes(granted, (SCOPE_SSO_ADMIN,))
        assert not has_required_scopes(
            frozenset(SCOPE_BUNDLES["global"]["viewer"]), (SCOPE_SSO_ADMIN,)
        )

    def test_tenant_admin_bundle_still_excludes_platform_only_scopes(self) -> None:
        assert "users:admin" not in SCOPE_BUNDLES["tenant"]["admin"]


class TestResponseShapesAreExplicit:
    """security.md Output Validation: the field set of every response is pinned."""

    def test_connection_dto(self) -> None:
        assert set(SsoConnectionDTO.__dataclass_fields__) == {
            "id", "protocol", "displayName", "enabled", "entitled", "allowedDomains", "issuer",
            "discoveryUrl", "clientId", "hasClientSecret", "usePlatformClient", "hostedDomain",
            "scopes", "idpEntityId", "idpSsoUrl", "idpCertificateCount", "nameIdFormat",
            "emailAttribute", "nameAttribute", "forceAuthn", "spEntityId", "acsUrl", "callbackUrl",
            "metadataUrl", "loginUrl", "createdAt", "updatedAt",
        }  # fmt: skip

    def test_no_dto_can_carry_a_secret(self) -> None:
        for dto in (SsoConnectionDTO, SsoConnectionResponse, SsoConnectionListResponse, SsoOptionDTO,
                    SsoOptionsResponse, SsoStartResponse, SsoDeleteResponse):  # fmt: skip
            names = " ".join(dto.__dataclass_fields__)
            assert not re.search(
                r"secret(?!ci)|ciphertext|password|token", names, re.I
            ) or names.count("hasClientSecret"), dto

    def test_other_dtos_have_stable_shapes(self) -> None:
        assert set(SsoOptionDTO.__dataclass_fields__) == {"id", "displayName", "protocol"}
        assert set(SsoOptionsResponse.__dataclass_fields__) == {"success", "options"}
        assert set(SsoStartResponse.__dataclass_fields__) == {"success", "redirectUrl", "protocol"}
        assert set(SsoDeleteResponse.__dataclass_fields__) == {"success"}

    async def test_start_response_has_exactly_the_documented_keys(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        body = await (await kit.start(public_id)).get_json()
        assert set(body) == {"success", "redirectUrl", "protocol"}


class TestSecretsNeverReprd:
    """A stray `%r` / f-string of a request or domain object must not print a secret."""

    def test_request_dtos_hide_the_client_secret(self) -> None:
        from blueprints.v1.sso import CreateConnectionRequest, UpdateConnectionRequest

        marker = "S3CR3T-" + "MARKER"
        create = CreateConnectionRequest(
            protocol="oidc", displayName="x", allowedDomains=["a.test"], clientSecret=marker
        )
        update = UpdateConnectionRequest(clientSecret=marker)
        assert marker not in repr(create) + repr(update) + str(create) + str(update)

    def test_service_inputs_and_identities_hide_pii_and_secrets(self) -> None:
        from services.sso_service import ConnectionInput
        from services.sso_types import ExternalIdentity, LoginOutcome

        marker = "S3CR3T-" + "MARKER"
        blob = repr(ConnectionInput(client_secret=marker))
        blob += repr(ExternalIdentity(subject=marker, email=marker, display_name=marker))
        blob += repr(LoginOutcome(marker, "uuid", "pid", "oidc", False))
        assert marker not in blob


class TestRegressions:
    async def test_unknown_protocol_is_400_not_503(self, kit: Kit) -> None:
        # regression: sso-protocol-order
        response = await kit.post_connection(kit.oidc_body(protocol="ldap"))
        assert response.status_code == 400

    async def test_first_login_inserts_identity_with_a_plain_int_user_id(self, kit: Kit) -> None:
        # regression: sso-pydal-reference
        public_id = await kit.create(kit.oidc_body())
        assert (await kit.oidc_login(public_id)).status_code == 303
        rows = list(await kit.db.install_dal(kit.db.install_dal.sso_identities.id > 0).select())
        assert type(rows[0].hub_user_id) is int

    async def test_connection_can_always_be_turned_off(self, kit: Kit) -> None:
        # regression: sso-disable-expired-cert (full matrix lives in test_sso_admin_api.py)
        public_id = await kit.create(kit.oidc_body())
        assert (await kit.patch_connection(public_id, {"enabled": False})).status_code == 200

    async def test_email_never_adopts_an_existing_account(self, kit: Kit) -> None:
        # regression: sso-no-email-adoption (the account-takeover guard, asserted end to end)
        await kit.db.insert_async(
            kit.db.dal.hub_users,
            email="alice@acme.test",
            username="alice",
            is_active=True,
            email_verified=True,
            password_hash="$2b$04$x",
        )
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id)
        assert kit.error_reason(response) == "sso_account_conflict"
        assert "auth/callback" not in kit.location(response)


class TestLogHygiene:
    """No PII, token, secret or protocol payload ever reaches a log line (any level)."""

    @staticmethod
    def _all_log_text(caplog: pytest.LogCaptureFixture) -> str:
        parts: list[str] = []
        for record in caplog.records:
            # Database/HTTP *driver* debug chatter (bound SQL parameters) is a property of
            # whichever driver the deployment runs at DEBUG, not of this feature's logging;
            # every application logger -- ours and flask_core's -- is in scope.
            if record.name.split(".")[0] in {
                "aiosqlite",
                "sqlalchemy",
                "asyncio",
                "httpcore",
                "hpack",
            }:
                continue
            parts.append(record.getMessage())
            if record.exc_text:
                parts.append(record.exc_text)
            parts.extend(
                str(v) for v in getattr(record, "__dict__", {}).values() if isinstance(v, str)
            )
        return "\n".join(parts)

    async def test_successful_oidc_login_logs_no_sensitive_values(
        self, kit: Kit, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        started = await kit.start(public_id)
        redirect_url = (await started.get_json())["redirectUrl"]
        code, state = kit.oidc.authorize(redirect_url)
        response = await kit.client.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string={"code": code, "state": state}
        )
        exchange = parse_qs(urlparse(kit.location(response)).query)["code"][0]
        payload = await kit.redeem(response)
        logs = self._all_log_text(caplog)
        sensitive = {
            "email": "alice@acme.test",
            "display name": "Alice Example",
            "subject": "idp-subject-1",
            "client secret": str(kit.oidc.client_secret),
            "auth code": code,
            "state": state,
            "nonce": parse_qs(urlparse(redirect_url).query)["nonce"][0],
            "pkce challenge": parse_qs(urlparse(redirect_url).query)["code_challenge"][0],
            "exchange code": exchange,
            "session jwt": str(payload["_token"]),
        }
        leaked = {label for label, value in sensitive.items() if value in logs}
        assert not leaked, f"sensitive values reached the logs: {leaked}"
        # ...while the operational facts an on-call engineer needs ARE there:
        assert f"connection={public_id}" in logs
        assert "sso.login.succeeded" in logs
        assert "user_uuid=" in logs

    async def test_failed_logins_log_type_code_and_frames_but_no_payloads(
        self, kit: Kit, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        public_id = await kit.create(kit.oidc_body())
        bad_aud = "audience-we-did-not-ask-for"
        kit.oidc.claims = {"aud": bad_aud, "email": "mallory@evil.test"}
        response = await kit.oidc_login(public_id)
        assert kit.error_reason(response) == "sso_invalid_response"
        logs = self._all_log_text(caplog)
        assert bad_aud not in logs
        assert "mallory@evil.test" not in logs
        assert "sso.login.failed" in logs
        assert "err_type=SsoProtocolError" in logs
        assert "err_code=oidc_id_token_invalid" in logs
        assert 'File "' in logs  # frame-only traceback is rendered into the message itself

    async def test_saml_login_and_forgery_logs_carry_no_assertion_content(
        self, kit: Kit, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        public_id = await kit.create(kit.saml_body())
        authn, relay = await kit.saml_start(public_id)
        good = kit.saml.build_response(
            authn, name_id="carol@acme.test", attributes={"displayName": ["Carol Q"]}
        )
        assert (await kit.post_acs(public_id, good, relay)).status_code == 303
        authn2, relay2 = await kit.saml_start(public_id)
        forged = kit.saml.build_response(authn2, sign="none", name_id="admin@acme.test")
        assert (
            kit.error_reason(await kit.post_acs(public_id, forged, relay2))
            == "sso_invalid_response"
        )
        logs = self._all_log_text(caplog)
        for value in (
            "carol@acme.test",
            "Carol Q",
            "admin@acme.test",
            kit.saml.encode(good),
            kit.saml.encode(forged),
        ):
            assert value not in logs
        assert "err_code=saml_signature_invalid" in logs

    async def test_exception_text_of_third_party_errors_is_withheld(
        self, kit: Kit, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        public_id = await kit.create(kit.oidc_body())
        # A hostile IdP puts PII-looking text in an error the HTTP/JWT libraries would echo.
        kit.oidc.token_response_override = (
            400,
            {"error": "invalid_grant", "error_description": "victim@acme.test"},
        )
        await kit.oidc_login(public_id)
        assert "victim@acme.test" not in self._all_log_text(caplog)


class TestTelemetry:
    @pytest.fixture
    def recorders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, list[tuple[float, dict[str, Any]]]]:
        calls: dict[str, list[tuple[float, dict[str, Any]]]] = {}

        class _Instrument:
            def __init__(self, name: str) -> None:
                self.name = name
                calls[name] = []

            def add(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
                calls[self.name].append((amount, attributes or {}))

            def record(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
                calls[self.name].append((amount, attributes or {}))

        for module, attr in (
            (sso_service, "login_counter"),
            (sso_service, "login_duration"),
            (sso_service, "start_counter"),
            (sso_service, "connection_change_counter"),
            (sso_http, "idp_request_duration"),
            (sso_saml, "saml_validation_counter"),
        ):
            monkeypatch.setattr(
                module, attr, _Instrument(f"{module.__name__.split('.')[-1]}.{attr}")
            )
        return calls

    async def test_oidc_metrics(self, kit: Kit, recorders: dict[str, Any]) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.oidc_login(public_id)
        kit.oidc.claims = {"aud": "nope"}
        await kit.oidc_login(public_id)
        logins = recorders["sso_service.login_counter"]
        assert (1, {"protocol": "oidc", "outcome": "success", "reason": "none"}) in logins
        assert (
            1,
            {"protocol": "oidc", "outcome": "failure", "reason": "oidc_id_token_invalid"},
        ) in logins
        assert len(recorders["sso_service.login_duration"]) == 2  # histogram recorded per attempt
        assert all(v >= 0 for v, _ in recorders["sso_service.login_duration"])
        assert recorders["sso_service.start_counter"] == [
            (1, {"protocol": "oidc"}),
            (1, {"protocol": "oidc"}),
        ]
        ops = {a["operation"] for _, a in recorders["sso_http.idp_request_duration"]}
        assert {"discovery", "token", "jwks"} <= ops

    async def test_saml_metrics(self, kit: Kit, recorders: dict[str, Any]) -> None:
        public_id = await kit.create(kit.saml_body())
        await kit.saml_login(public_id)
        await kit.saml_login(public_id, sign="none")
        logins = recorders["sso_service.login_counter"]
        assert (1, {"protocol": "saml", "outcome": "success", "reason": "none"}) in logins
        assert (
            1,
            {"protocol": "saml", "outcome": "failure", "reason": "saml_signature_invalid"},
        ) in logins
        assert (1, {"code": "saml_signature_invalid"}) in recorders[
            "sso_saml.saml_validation_counter"
        ]

    async def test_connection_change_metrics(self, kit: Kit, recorders: dict[str, Any]) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.patch_connection(public_id, {"displayName": "R"})
        await kit.patch_connection(public_id, {"enabled": False})
        await kit.delete_connection(public_id)
        ops = [a["operation"] for _, a in recorders["sso_service.connection_change_counter"]]
        assert ops == ["create", "update", "disable", "delete"]

    async def test_metric_labels_are_bounded_and_pii_free(
        self, kit: Kit, recorders: dict[str, Any]
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.oidc_login(public_id)
        blob = repr(recorders)
        assert "alice" not in blob
        assert public_id not in blob  # no per-connection label cardinality
        assert TENANT_NAME not in blob


TENANT_NAME = "acme-corp"


class TestTraces:
    @pytest.fixture
    def spans(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(sso_telemetry, "_tracer", provider.get_tracer("test"))
        return exporter

    async def test_oidc_login_emits_the_expected_span_tree(self, kit: Kit, spans: Any) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.oidc_login(public_id)
        names = {s.name for s in spans.get_finished_spans()}
        assert {
            "sso.login.start", "sso.oidc.discovery", "sso.idp.discovery", "sso.oidc.token_exchange",
            "sso.idp.token", "sso.idp.jwks", "sso.login.provision",
        } <= names  # fmt: skip

    async def test_saml_validation_span_and_error_status_without_exception_text(
        self, kit: Kit, spans: Any
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        await kit.saml_login(public_id)
        await kit.saml_login(public_id, sign="none")
        validate = [s for s in spans.get_finished_spans() if s.name == "sso.saml.validate"]
        assert len(validate) == 2
        failed = [s for s in validate if s.status.status_code.name == "ERROR"]
        assert len(failed) == 1
        assert failed[0].status.description == "SsoProtocolError"  # type name only
        assert all("sso.protocol" in s.attributes for s in validate)

    async def test_spans_carry_only_opaque_identifiers(self, kit: Kit, spans: Any) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.oidc_login(public_id)
        blob = repr([dict(s.attributes or {}) for s in spans.get_finished_spans()])
        assert "alice" not in blob
        assert "acme.test" not in blob


class TestStaticGuards:
    def test_sources_were_actually_found(self) -> None:
        assert len(SSO_SOURCES) >= 10  # a mis-pointed scan must not report "clean"

    def test_exc_log_audit_finds_no_unsafe_exception_logging(self) -> None:
        report = audit_paths(SSO_SOURCES)
        assert report.files_examined >= len(SSO_SOURCES)
        assert not report.findings, [f.render() for f in report.findings]

    @pytest.mark.parametrize("path", SSO_SOURCES, ids=lambda p: p.name)
    def test_no_print_or_basic_config(self, path: Path) -> None:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                assert name not in {"print", "basicConfig"}, f"{path.name}:{node.lineno}"

    @pytest.mark.parametrize("path", SSO_SOURCES, ids=lambda p: p.name)
    def test_no_stub_markers_or_swallowed_exceptions(self, path: Path) -> None:
        text = path.read_text()
        assert not re.search(r"\b(TODO|FIXME|XXX|NotImplementedError)\b", text)
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                body = node.body
                assert not (
                    len(body) == 1 and isinstance(body[0], ast.Pass)
                ), f"{path.name}:{node.lineno}"

    def test_no_hardcoded_secrets_or_unsafe_primitives(self) -> None:
        for path in SSO_SOURCES:
            text = path.read_text()
            assert "verify=False" not in text, path.name
            assert (
                not re.search(r"(?i)\b(md5|sha1)\(", text) or path.name == "sso_saml.py"
            ), path.name
            assert "eval(" not in text and "exec(" not in text, path.name

    def test_every_route_function_is_async(self) -> None:
        tree = ast.parse((HUB_API / "blueprints/v1/sso.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                decorated = any(
                    isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route"
                    for d in node.decorator_list
                )
                assert not decorated, f"sync route {node.name}"

    def test_blueprint_registers_both_blueprints_for_auto_discovery(self) -> None:
        assert [bp.name for bp in sso_bp.BLUEPRINTS] == ["v1_sso_public", "v1_sso_admin"]


class TestMigrationParity:
    """The sqlite test mirror must match migration 0049 column for column."""

    @staticmethod
    def _migration_columns(table: str) -> set[str]:
        sql = (HUB_API.parent / "alembic/versions/0049_sso_connections.py").read_text()
        block = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n        \)", sql, re.S)
        assert block, table
        cols: set[str] = set()
        for line in block.group(1).splitlines():
            m = re.match(
                r"\s+([a-z_]+)\s+(?:BIGSERIAL|VARCHAR|INTEGER|BIGINT|BOOLEAN|JSONB|TEXT|TIMESTAMPTZ)",
                line,
            )
            if m:
                cols.add(m.group(1))
        return cols

    @pytest.mark.parametrize("table", ["sso_connections", "sso_identities"])
    def test_columns_match(self, table: str) -> None:
        mirror = {c.name for c in build_sso_metadata().tables[table].columns}
        assert mirror == self._migration_columns(table)

    def test_rbac_matrix_lists_both_tables_with_hub_api_as_sole_writer(self) -> None:
        import yaml

        matrix = yaml.safe_load((HUB_API.parent / "config/postgres/rbac-matrix.yaml").read_text())
        assert {"sso_connections", "sso_identities"} <= set(matrix["tables"])
        for table in ("sso_connections", "sso_identities"):
            rows = [g for g in matrix["grants"] if g["table"] == table]
            assert {g["role"] for g in rows} == set(matrix["roles"])  # explicit row per role
            writers = {g["role"] for g in rows if "INSERT" in g["privileges"]}
            assert writers == {"hub_api", "migration_runner"}


def test_fake_idp_is_not_shipped_in_production_code() -> None:
    assert FakeOidcIdp.__module__.startswith("tests.")
