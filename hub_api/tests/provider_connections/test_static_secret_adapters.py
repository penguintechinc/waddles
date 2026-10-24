"""Static-secret kinds: the int enums, the endpoint/secret validators and the three adapters.

Pure-unit (no database): every hostile-input case a write path must refuse before anything is
sealed or stored, and the adapter invariants (structural-only credential check, never refreshable).
"""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest

from services.provider_connections.adapter import (
    AdapterRegistry,
    Credentials,
    ProviderAdapter,
    validate_metadata,
)
from services.provider_connections.errors import (
    CredentialsRejected,
    InvalidConnectionInput,
    RefreshNotConfigured,
)
from services.provider_connections.kinds import (
    ConnectionKind,
    SecretScheme,
    UnknownConnectionKind,
    UnknownSecretScheme,
)
from services.provider_connections.registry import build_default_registry
from services.provider_connections.static_secret import (
    AwsLambdaAdapter,
    ExternalHttpAdapter,
    GcpFunctionAdapter,
    infer_scheme,
    reject_control_characters,
    validate_aws_region,
    validate_endpoint_url,
    validate_secret_document,
)

BEARER_DOC = {"token": "tok_abc123"}
BASIC_DOC = {"username": "svc", "password": "p@ss w0rd"}
HEADER_DOC = {"header": "X-Api-Key", "value": "k-123"}
AWS_DOC = {"access_key_id": "AKIAEXAMPLE", "secret_access_key": "wJalrEXAMPLEKEY"}
GCP_DOC = {"client_email": "sa@proj.iam.gserviceaccount.com", "private_key": "-----BEGIN-----x"}


class TestEndpointUrl:
    @pytest.mark.parametrize(
        "bad",
        [
            "http://x.example",
            "https://u:p@x.example",
            "https://127.0.0.1/x",
            "https://10.0.0.5/x",
            "https://[::1]/x",
            "https://169.254.169.254/",
            "https://ok.example\n",
            "https://ok.example/#frag",
            "https://" + "a" * 2100 + ".example",
            "https://192.168.1.1/",
            "https://172.16.0.9/",
            "https://100.64.0.1/",
            "https://[fe80::1]/",
            "https://[::ffff:127.0.0.1]/",
            "https://0.0.0.0/",
            "https://224.0.0.1/",
            "ftp://x.example",
            "https://",
            "https:///path",
            "https://x.example:99999/",
            "https://:80/",
            "https://x.example/a b",
            "https://x.example/\x00",
            "",
            " https://x.example",
        ],
    )
    def test_endpoint_validation_rejects_hostile_urls(self, bad: str) -> None:
        with pytest.raises(InvalidConnectionInput):
            validate_endpoint_url(bad)

    @pytest.mark.parametrize(
        "good",
        [
            "https://ok.example",
            "https://api.ok.example/v1/hook?x=1",
            "https://ok.example:8443/path",
            "https://8.8.8.8/x",
            "https://[2606:4700::1111]/x",
        ],
    )
    def test_endpoint_validation_accepts_https_dns_names_and_public_ips(self, good: str) -> None:
        assert validate_endpoint_url(good) == good

    def test_the_error_never_echoes_the_url(self) -> None:
        with pytest.raises(InvalidConnectionInput) as info:
            validate_endpoint_url("https://user:SECRETPW@x.example")
        assert "SECRETPW" not in str(info.value)

    def test_a_non_string_is_rejected(self) -> None:
        with pytest.raises(InvalidConnectionInput):
            validate_endpoint_url(None)  # type: ignore[arg-type]

    def test_the_length_limit_is_exact(self) -> None:
        ok = "https://x.example/" + "a" * (2048 - len("https://x.example/"))
        assert len(ok) == 2048 and validate_endpoint_url(ok) == ok
        with pytest.raises(InvalidConnectionInput):
            validate_endpoint_url(ok + "a")


class TestAwsRegion:
    @pytest.mark.parametrize(
        "region", ["us-east-1\n", "US_EAST", "", "x" * 40, " us-east-1", "us east 1"]
    )
    def test_aws_region_validation_rejects(self, region: str) -> None:
        with pytest.raises(InvalidConnectionInput):
            validate_aws_region(region)

    @pytest.mark.parametrize(
        "region", ["us-east-1", "eu-central-1", "ap-southeast-2", "us-gov-west-1"]
    )
    def test_aws_region_validation_accepts(self, region: str) -> None:
        assert validate_aws_region(region) == region

    def test_a_newline_smuggled_into_metadata_is_rejected_not_stripped(self) -> None:
        adapter = AwsLambdaAdapter()
        with pytest.raises(InvalidConnectionInput):
            reject_control_characters({"region": "us-east-1\n", "function": "fn"})
        # the generic validator would have silently trimmed it; the guard above runs first
        assert validate_metadata(adapter.spec, {"region": "us-east-1\n", "function": "fn"})

    def test_control_characters_guard_ignores_clean_values(self) -> None:
        reject_control_characters({"region": "us-east-1", "n": 3})


class TestSecretDocument:
    @pytest.mark.parametrize(
        ("scheme", "doc"),
        [
            (SecretScheme.BEARER, BEARER_DOC),
            (SecretScheme.BASIC, BASIC_DOC),
            (SecretScheme.API_KEY_HEADER, HEADER_DOC),
            (SecretScheme.AWS_ACCESS_KEY, AWS_DOC),
            (SecretScheme.GCP_SERVICE_ACCOUNT, GCP_DOC),
        ],
    )
    def test_each_scheme_accepts_exactly_its_keys(
        self, scheme: SecretScheme, doc: dict[str, str]
    ) -> None:
        assert validate_secret_document(scheme, doc) == doc
        assert infer_scheme(doc) is scheme

    @pytest.mark.parametrize("scheme", list(SecretScheme))
    def test_secret_document_rejects_unknown_and_missing_keys(self, scheme: SecretScheme) -> None:
        from services.provider_connections import static_secret as module

        required = sorted(module._SCHEME_KEYS[scheme])
        full = dict.fromkeys(required, "v")
        with pytest.raises(InvalidConnectionInput, match="unknown"):
            validate_secret_document(scheme, {**full, "extra": "x"})
        with pytest.raises(InvalidConnectionInput, match="missing"):
            validate_secret_document(scheme, {k: "v" for k in required[1:]})
        with pytest.raises(InvalidConnectionInput):
            validate_secret_document(scheme, {})

    def test_values_must_be_non_empty_strings(self) -> None:
        for bad in ("", None, 5, ["x"], {"a": 1}):
            with pytest.raises(InvalidConnectionInput):
                validate_secret_document(SecretScheme.BEARER, {"token": bad})  # type: ignore[dict-item]

    def test_max_lengths_are_enforced(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="too long"):
            validate_secret_document(SecretScheme.BASIC, {"username": "u" * 257, "password": "p"})
        with pytest.raises(InvalidConnectionInput, match="too long"):
            validate_secret_document(SecretScheme.BEARER, {"token": "t" * 4097})
        assert validate_secret_document(SecretScheme.BEARER, {"token": "t" * 4096})

    def test_header_name_and_bearer_whitespace_are_checked(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="header"):
            validate_secret_document(
                SecretScheme.API_KEY_HEADER, {"header": "Bad Header\n", "value": "v"}
            )
        with pytest.raises(InvalidConnectionInput, match="whitespace"):
            validate_secret_document(SecretScheme.BEARER, {"token": "a b"})

    def test_a_non_mapping_and_non_string_keys_are_rejected(self) -> None:
        with pytest.raises(InvalidConnectionInput):
            validate_secret_document(SecretScheme.BEARER, ["token"])  # type: ignore[arg-type]
        with pytest.raises(InvalidConnectionInput):
            validate_secret_document(SecretScheme.BEARER, {1: "x"})  # type: ignore[dict-item]

    def test_errors_never_echo_a_secret_value(self) -> None:
        with pytest.raises(InvalidConnectionInput) as info:
            validate_secret_document(SecretScheme.BEARER, {"token": "TOPSECRET " * 1000})
        assert "TOPSECRET" not in str(info.value)

    def test_infer_scheme_is_none_for_an_unknown_shape(self) -> None:
        assert infer_scheme({"nope": "x"}) is None


class TestEnums:
    def test_the_codes_are_the_cross_task_contract(self) -> None:
        assert [(k.name, int(k)) for k in ConnectionKind] == [
            ("PLATFORM_OAUTH", 1),
            ("EXTERNAL_HTTP", 2),
            ("AWS_LAMBDA", 3),
            ("GCP_FUNCTION", 4),
        ]
        assert [(s.name, int(s)) for s in SecretScheme] == [
            ("BEARER", 1),
            ("BASIC", 2),
            ("API_KEY_HEADER", 3),
            ("AWS_ACCESS_KEY", 4),
            ("GCP_SERVICE_ACCOUNT", 5),
        ]

    @pytest.mark.parametrize("code", [0, 5, -1, 99, True, "2", None, 2.0])
    def test_connection_kind_from_unknown_code_raises(self, code: Any) -> None:
        with pytest.raises(UnknownConnectionKind):
            ConnectionKind.from_code(code)

    @pytest.mark.parametrize("code", [0, 6, -1, True, "1", None])
    def test_secret_scheme_from_unknown_code_raises(self, code: Any) -> None:
        with pytest.raises(UnknownSecretScheme):
            SecretScheme.from_code(code)

    def test_known_codes_round_trip(self) -> None:
        for kind in ConnectionKind:
            assert ConnectionKind.from_code(int(kind)) is kind
        for scheme in SecretScheme:
            assert SecretScheme.from_code(int(scheme)) is scheme


ADAPTERS = [
    (
        ExternalHttpAdapter,
        "external_http",
        ConnectionKind.EXTERNAL_HTTP,
        [BEARER_DOC, BASIC_DOC, HEADER_DOC],
        AWS_DOC,
    ),
    (AwsLambdaAdapter, "aws_lambda", ConnectionKind.AWS_LAMBDA, [AWS_DOC], BEARER_DOC),
    (GcpFunctionAdapter, "gcp_function", ConnectionKind.GCP_FUNCTION, [GCP_DOC], BEARER_DOC),
]


class TestAdapters:
    @pytest.mark.parametrize(("cls", "provider", "kind", "good", "bad"), ADAPTERS)
    def test_spec_invariants(
        self, cls: Any, provider: str, kind: ConnectionKind, good: Any, bad: Any
    ) -> None:
        adapter = cls()
        assert isinstance(adapter, ProviderAdapter)
        spec = adapter.spec
        assert (spec.provider, spec.platform, spec.resource_type) == (provider, provider, provider)
        assert spec.kind is kind and spec.refreshable is False
        assert spec.capabilities == frozenset({"dispatch"})
        assert spec.credential_fields == () and spec.required_scopes == ()
        # the secret itself can never be advertised as metadata
        assert not (
            {"token", "password", "private_key", "secret_access_key"} & spec.public_metadata_keys
        )

    @pytest.mark.parametrize(("cls", "provider", "kind", "good", "bad"), ADAPTERS)
    async def test_validate_credentials_is_structural_and_makes_no_network_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cls: Any,
        provider: str,
        kind: ConnectionKind,
        good: list[dict[str, str]],
        bad: dict[str, str],
    ) -> None:
        def no_network(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a static adapter must not touch the network at write time")

        monkeypatch.setattr(socket, "getaddrinfo", no_network)
        monkeypatch.setattr(socket.socket, "connect", no_network)
        adapter = cls()
        for doc in good:
            account = await adapter.validate_credentials(Credentials(json.dumps(doc)))
            assert account.expires_at is None and account.scopes == ("dispatch",)
        with pytest.raises(CredentialsRejected):
            await adapter.validate_credentials(Credentials(json.dumps(bad)))

    async def test_validate_credentials_rejects_non_json_and_non_objects(self) -> None:
        adapter = ExternalHttpAdapter()
        for raw in ("not json", "[1]", "null", '"s"', "{}"):
            with pytest.raises(CredentialsRejected):
                await adapter.validate_credentials(Credentials(raw))

    @pytest.mark.parametrize(("cls", "provider", "kind", "good", "bad"), ADAPTERS)
    async def test_refresh_is_never_configured(
        self, cls: Any, provider: str, kind: Any, good: Any, bad: Any
    ) -> None:
        with pytest.raises(RefreshNotConfigured):
            await cls().refresh("anything", {})

    async def test_the_remaining_protocol_methods(self) -> None:
        adapter = ExternalHttpAdapter()
        creds = Credentials("x")
        assert adapter.normalize_credentials(creds) is creds
        assert adapter.granted_capabilities(()) == frozenset({"dispatch"})
        resource = await adapter.resolve_resource(
            creds,
            await adapter.validate_credentials(Credentials(json.dumps(BEARER_DOC))),
            {"url": "https://ok.example/h"},
        )
        assert resource.facts == {"url": "https://ok.example/h"}

    def test_targets_are_validated(self) -> None:
        assert ExternalHttpAdapter().validate_target({"url": "https://ok.example"}) == {
            "url": "https://ok.example"
        }
        with pytest.raises(InvalidConnectionInput):
            ExternalHttpAdapter().validate_target({"url": "https://10.0.0.1"})
        assert AwsLambdaAdapter().validate_target({"region": "us-east-1", "function": "fn_1"}) == {
            "region": "us-east-1",
            "function": "fn_1",
        }
        with pytest.raises(InvalidConnectionInput):
            AwsLambdaAdapter().validate_target({"region": "nope", "function": "fn"})
        assert GcpFunctionAdapter().validate_target(
            {"project": "my-project-1", "region": "us-central1", "function": "fn"}
        ) == {"project": "my-project-1", "region": "us-central1", "function": "fn"}

    @pytest.mark.parametrize(
        "meta",
        [
            {"region": "US_EAST", "function": "f"},
            {"region": "us-east-1", "function": "bad name!"},
            {"region": "us-east-1", "function": "f" * 65},
            {"region": "us-east-1"},
            {"region": "us-east-1", "function": "f", "extra": "x"},
        ],
    )
    def test_aws_metadata_rejects_malformed_targets(self, meta: dict[str, str]) -> None:
        with pytest.raises(InvalidConnectionInput):
            validate_metadata(AwsLambdaAdapter().spec, meta)

    @pytest.mark.parametrize(
        "meta",
        [
            {"project": "Bad_Project", "region": "us-central1", "function": "f"},
            {"project": "my-project-1", "region": "us_central1", "function": "f"},
            {"project": "my-project-1", "region": "us-central1", "function": "9bad"},
        ],
    )
    def test_gcp_metadata_rejects_malformed_targets(self, meta: dict[str, str]) -> None:
        with pytest.raises(InvalidConnectionInput):
            validate_metadata(GcpFunctionAdapter().spec, meta)

    def test_the_base_class_refuses_to_guess_a_target(self) -> None:
        from services.provider_connections.static_secret import StaticSecretAdapter

        with pytest.raises(NotImplementedError):
            StaticSecretAdapter().validate_target({})

    def test_the_default_registry_wires_all_four_providers(self) -> None:
        registry = build_default_registry()
        assert {"twitch", "external_http", "aws_lambda", "gcp_function"} <= {
            s.provider for s in registry.specs()
        }
        assert isinstance(registry, AdapterRegistry)
