"""Tests for the bundle.yaml v2 `commands:` declaration field (slash-command registry phase 1)."""

from __future__ import annotations

from typing import Any

import pytest

from services.bundle_approval_service import _reparse_trusted
from services.bundle_manifest_v2 import (
    BundleManifestV2,
    CommandDeclaration,
    CommandType,
    ManifestV2Error,
    parse_bundle_manifest_v2,
)

_BASE: dict[str, Any] = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station Song Request",
    "version": "3.0.0",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "bundles.social_music_process:transform",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        },
    },
    "egress": [{"host": "api.spotify.com", "methods": ["GET", "POST"]}],
    "data": {"tables": ["music_queue"]},
    "limits": {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 10},
}


def base() -> dict[str, Any]:
    """Return a fresh minimal valid manifest dict."""
    return dict(_BASE)


def _parse(raw: dict[str, Any], **kwargs: Any) -> BundleManifestV2:
    return parse_bundle_manifest_v2(
        raw,
        known_custom_platforms=kwargs.get("known_custom_platforms", frozenset()),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


def _reason(commands: Any, **kwargs: Any) -> str:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({**base(), "commands": commands}, **kwargs)
    return exc.value.reason


def test_manifest_without_commands_has_empty_declarations() -> None:
    assert _parse(base()).command_declarations == ()


def test_command_defaults_to_standard_no_platforms() -> None:
    m = _parse({**base(), "commands": [{"name": "ping"}]})
    assert m.command_declarations == (CommandDeclaration("ping", CommandType.STANDARD, None, None),)


def test_command_type_values_match_db_bitmask() -> None:
    assert int(CommandType.STANDARD) == 1
    assert int(CommandType.WEBHOOK) == 2


def test_types_and_platforms_parsed() -> None:
    d = _parse(
        {
            **base(),
            "commands": [
                {
                    "name": "ping",
                    "types": ["standard", "webhook"],
                    "platforms": ["discord"],
                    "description": "Pong",
                }
            ],
        }
    ).command_declarations[0]
    assert d.types == CommandType.STANDARD | CommandType.WEBHOOK
    assert d.platforms == ("discord",)
    assert d.description == "Pong"


def test_wildcard_is_standard_only() -> None:
    _parse({**base(), "commands": [{"name": "*"}]})
    assert _reason([{"name": "*", "types": ["webhook"]}]) == "invalid_command_type"
    assert _reason([{"name": "*", "types": ["standard", "webhook"]}]) == "invalid_command_type"


@pytest.mark.parametrize("bad", ["Ping", "!ping", "a b", "", "x" * 33, 5, "ping\n"])
def test_bad_name(bad: Any) -> None:
    assert _reason([{"name": bad}]) == "invalid_command_name"


def test_missing_name() -> None:
    assert _reason([{"types": ["standard"]}]) == "invalid_command_name"


def test_entry_not_a_mapping() -> None:
    assert _reason(["ping"]) == "invalid_commands"


def test_duplicate_command() -> None:
    assert _reason([{"name": "ping"}, {"name": "ping"}]) == "duplicate_command"


@pytest.mark.parametrize("types", [["slash"], [], "standard", [1]])
def test_bad_types(types: Any) -> None:
    assert _reason([{"name": "ping", "types": types}]) == "invalid_command_type"


def test_unknown_platform() -> None:
    assert _reason([{"name": "ping", "platforms": ["myspace"]}]) == "invalid_command_platforms"


@pytest.mark.parametrize("platforms", [[], "discord", [3]])
def test_malformed_platforms(platforms: Any) -> None:
    assert _reason([{"name": "ping", "platforms": platforms}]) == "invalid_command_platforms"


def test_custom_platform_registered_ok_and_unregistered_rejected() -> None:
    m = _parse(
        {**base(), "commands": [{"name": "ping", "platforms": ["custom:acme"]}]},
        known_custom_platforms=frozenset({"acme"}),
    )
    assert m.command_declarations[0].platforms == ("custom:acme",)
    assert _reason([{"name": "ping", "platforms": ["custom:acme"]}]) == "invalid_command_platforms"


def test_platform_outside_supported_platforms() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            {
                **base(),
                "supported_platforms": ["twitch"],
                "commands": [{"name": "ping", "platforms": ["discord"]}],
            }
        )
    assert exc.value.reason == "command_platform_unsupported"


def test_platform_inside_supported_platforms_ok() -> None:
    m = _parse(
        {
            **base(),
            "supported_platforms": ["twitch", "discord"],
            "commands": [{"name": "ping", "platforms": ["discord"]}],
        }
    )
    assert m.command_declarations[0].platforms == ("discord",)


def test_description_bounds() -> None:
    assert _reason([{"name": "ping", "description": "x" * 101}]) == "invalid_command_description"
    assert _reason([{"name": "ping", "description": ""}]) == "invalid_command_description"
    assert _reason([{"name": "ping", "description": 7}]) == "invalid_command_description"
    m = _parse({**base(), "commands": [{"name": "ping", "description": "x" * 100}]})
    assert m.command_declarations[0].description == "x" * 100


def test_commands_not_a_list() -> None:
    assert _reason("ping") == "invalid_commands"


def test_reparse_trusted_round_trips_commands() -> None:
    raw = {
        **base(),
        "supported_platforms": ["twitch", "discord", "custom:acme"],
        "commands": [
            {"name": "ping", "types": ["standard", "webhook"], "platforms": ["discord"]},
            {"name": "*", "platforms": ["custom:acme"], "description": "catch-all"},
        ],
    }
    parsed = _parse(raw, known_custom_platforms=frozenset({"acme"}))
    assert parsed.command_declarations  # non-empty guard against a vacuous comparison
    assert len(parsed.command_declarations) == 2
    assert _reparse_trusted(raw).command_declarations == parsed.command_declarations
