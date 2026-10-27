"""The provider-agnostic adapter contract: spec validation, metadata/credential checks, registry."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from services.provider_connections.adapter import (
    INSTALL_KIND_CREDENTIALS,
    MAX_TOKEN_LENGTH,
    AdapterRegistry,
    AdapterSpec,
    CredentialField,
    MetadataField,
    validate_credentials_shape,
    validate_metadata,
    validate_token_shape,
)
from services.provider_connections.errors import InvalidConnectionInput, UnknownProvider
from services.provider_connections.twitch import TWITCH_SPEC, TwitchAdapter


def _spec(**overrides: Any) -> AdapterSpec:
    base: dict[str, Any] = {
        "provider": "fakechat",
        "display_name": "FakeChat",
        "platform": "fakechat",
        "resource_type": "fake_room",
        "install_kind": INSTALL_KIND_CREDENTIALS,
        "capabilities": frozenset({"chat_ingest"}),
        "required_scopes": (),
        "credential_fields": (CredentialField("access_token", True, "token"),),
        "metadata_fields": (
            MetadataField("room", True, "room", pattern=r"[a-z]{2,10}", max_length=10),
            MetadataField("note", False, "free text", max_length=8),
        ),
        "public_metadata_keys": frozenset({"room"}),
        "runtime_metadata_keys": frozenset({"room"}),
        "refreshable": False,
    }
    base.update(overrides)
    return AdapterSpec(**base)


class TestSpecValidation:
    def test_a_well_formed_spec_is_accepted(self) -> None:
        assert _spec().provider == "fakechat"

    @pytest.mark.parametrize("provider", ["", "Twitch", "1abc", "a", "has space", "x" * 31, "a-b"])
    def test_bad_provider_ids_are_rejected(self, provider: str) -> None:
        with pytest.raises(ValueError, match="provider id"):
            _spec(provider=provider)

    @pytest.mark.parametrize("resource_type", ["", "Room", "r", "has space"])
    def test_bad_resource_types_are_rejected(self, resource_type: str) -> None:
        with pytest.raises(ValueError, match="resource_type"):
            _spec(resource_type=resource_type)

    def test_empty_platform_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="platform"):
            _spec(platform="")

    def test_bad_field_names_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="field name"):
            _spec(metadata_fields=(MetadataField("Bad-Name", True, "x"),))

    def test_an_invalid_pattern_is_caught_at_registration_not_at_first_request(self) -> None:
        with pytest.raises(Exception, match="unterminated|missing"):
            _spec(metadata_fields=(MetadataField("room", True, "x", pattern="[a-"),))


class TestValidateMetadata:
    def test_valid_input_is_normalised(self) -> None:
        spec = _spec()
        assert validate_metadata(spec, {"room": "lobby", "note": "hi"}) == {
            "room": "lobby",
            "note": "hi",
        }

    def test_unknown_keys_are_refused_by_name(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="unknown metadata field"):
            validate_metadata(_spec(), {"room": "lobby", "evil": "x"})

    def test_missing_required_field(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="'room' is required"):
            validate_metadata(_spec(), {})

    def test_non_string_value(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="must be a string"):
            validate_metadata(_spec(), {"room": 5})

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_required_value(self, value: str) -> None:
        with pytest.raises(InvalidConnectionInput, match="must not be empty"):
            validate_metadata(_spec(), {"room": value})

    def test_an_absent_optional_field_is_simply_absent(self) -> None:
        assert validate_metadata(_spec(), {"room": "lobby"}) == {"room": "lobby"}

    def test_blank_optional_value_is_dropped(self) -> None:
        assert validate_metadata(_spec(), {"room": "lobby", "note": "  "}) == {"room": "lobby"}

    def test_too_long(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="too long"):
            validate_metadata(_spec(), {"room": "lobby", "note": "123456789"})

    def test_pattern_mismatch_never_echoes_the_value(self) -> None:
        with pytest.raises(InvalidConnectionInput) as info:
            validate_metadata(_spec(), {"room": "BAD VALUE!"})
        assert "BAD VALUE" not in info.value.message
        assert "invalid format" in info.value.message

    def test_lowercase_and_strip_prefix(self) -> None:
        result = validate_metadata(TWITCH_SPEC, {"channel_login": "  #PenguinzPlays "})
        assert result == {"channel_login": "penguinzplays"}

    @pytest.mark.parametrize("login", ["ab", "x" * 26, "bad name", "ünï", "a-b-c"])
    def test_twitch_channel_pattern(self, login: str) -> None:
        with pytest.raises(InvalidConnectionInput):
            validate_metadata(TWITCH_SPEC, {"channel_login": login})


class TestTokenShape:
    def test_ok(self) -> None:
        assert validate_token_shape("access_token", "abc.DEF-123_x") == "abc.DEF-123_x"

    @pytest.mark.parametrize("value", ["", None, 5])
    def test_required(self, value: Any) -> None:
        with pytest.raises(InvalidConnectionInput, match="required"):
            validate_token_shape("access_token", value)

    def test_too_long(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="too long"):
            validate_token_shape("access_token", "a" * (MAX_TOKEN_LENGTH + 1))
        assert validate_token_shape("access_token", "a" * MAX_TOKEN_LENGTH)

    @pytest.mark.parametrize(
        "value",
        [
            "has space",
            "tab\there",
            "new\nline",
            "nul\x00byte",
            "trailing\n",
            "unicod\u00e9",
            "\x7fdel",
        ],
    )
    def test_whitespace_and_control_characters(self, value: str) -> None:
        with pytest.raises(InvalidConnectionInput, match="printable ASCII"):
            validate_token_shape("access_token", value)

    def test_the_offending_value_is_never_echoed(self) -> None:
        with pytest.raises(InvalidConnectionInput) as info:
            validate_token_shape("access_token", "secret value with spaces")
        assert "secret" not in info.value.message


class TestCredentialsShape:
    def test_optional_refresh_token(self) -> None:
        creds = validate_credentials_shape(TWITCH_SPEC, "a", None)
        assert (creds.access_token, creds.refresh_token) == ("a", None)
        creds = validate_credentials_shape(TWITCH_SPEC, "a", "r")
        assert creds.refresh_token == "r"

    def test_required_refresh_token_is_enforced_when_the_spec_demands_it(self) -> None:
        spec = replace(
            _spec(),
            credential_fields=(
                CredentialField("access_token", True, "t"),
                CredentialField("refresh_token", True, "r"),
            ),
        )
        with pytest.raises(InvalidConnectionInput, match="refresh_token is required"):
            validate_credentials_shape(spec, "a", None)

    def test_credentials_repr_never_contains_a_secret(self) -> None:
        creds = validate_credentials_shape(TWITCH_SPEC, "topsecretaccess", "topsecretrefresh")
        assert "topsecret" not in repr(creds)


class TestRegistry:
    def test_lookup_listing_and_membership(self) -> None:
        registry = AdapterRegistry([TwitchAdapter()])
        assert "twitch" in registry
        assert "nope" not in registry
        assert registry.get("twitch").spec is TWITCH_SPEC
        assert [spec.provider for spec in registry.specs()] == ["twitch"]

    def test_unknown_provider_fails_loudly(self) -> None:
        with pytest.raises(UnknownProvider):
            AdapterRegistry().get("slack")

    def test_double_registration_is_a_programming_error(self) -> None:
        registry = AdapterRegistry([TwitchAdapter()])
        with pytest.raises(ValueError, match="already registered"):
            registry.register(TwitchAdapter())

    def test_specs_are_ordered_by_provider_id(self) -> None:
        class _Stub:
            def __init__(self, provider: str) -> None:
                self.spec = _spec(provider=provider, platform=provider)

        registry = AdapterRegistry()
        for name in ("zeta", "alpha", "mid"):
            registry.register(_Stub(name))  # type: ignore[arg-type]
        assert [s.provider for s in registry.specs()] == ["alpha", "mid", "zeta"]
