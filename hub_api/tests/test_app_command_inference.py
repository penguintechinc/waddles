"""Tests for the pure slash-command inference layer (`services.app_command_service`)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from services.app_command_service import (
    DesiredCommand,
    infer_commands,
    resolve_desired_commands,
    verb_from_prefix,
)
from services.bundle_manifest_v2 import (
    BundleManifestV2,
    CommandDeclaration,
    CommandType,
    ConsumeRule,
    Limits,
    parse_bundle_manifest_v2,
)

_APP_ID = "waddles.core.ping.default"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _rule(
    platform: str,
    prefixes: tuple[str, ...] | None = None,
    event_types: tuple[str, ...] = ("chat.message",),
) -> ConsumeRule:
    filters: dict[str, Any] = {} if prefixes is None else {"command_prefix": list(prefixes)}
    return ConsumeRule(platform=platform, source_id=None, event_types=event_types, filters=filters)


def _manifest(
    rules: tuple[ConsumeRule, ...],
    *,
    supported: tuple[str, ...] | None = None,
    declarations: tuple[CommandDeclaration, ...] = (),
) -> BundleManifestV2:
    return BundleManifestV2(
        schema_version=2,
        app_id=_APP_ID,
        name="Ping",
        version="1.0.0",
        feature="waddles.core.ping",
        module="core",
        provider="builtin",
        language="python",
        artifact="source",
        execution_model="native",
        is_default=False,
        stages={},
        egress=(),
        data_tables=(),
        limits=Limits(timeout_ms=2000, memory_mb=64, egress_rps=10),
        permissions=(),
        routes_to=(),
        consumes=rules,
        supported_platforms=supported,
        command_declarations=declarations,
    )


def test_prefix_rule_infers_standard_row_per_verb() -> None:
    m = _manifest((_rule("twitch", ("!about", "!bot")), _rule("discord", ("!about", "!bot"))))
    assert infer_commands(m) == (
        DesiredCommand("about", CommandType.STANDARD, ("discord", "twitch"), None),
        DesiredCommand("bot", CommandType.STANDARD, ("discord", "twitch"), None),
    )


def test_bare_rule_infers_wildcard() -> None:
    m = _manifest((_rule("twitch"), _rule("discord")))
    assert infer_commands(m) == (
        DesiredCommand("*", CommandType.STANDARD, ("discord", "twitch"), None),
    )


def test_mixed_rules_per_platform() -> None:
    m = _manifest((_rule("discord", ("!ping",)), _rule("twitch")))
    assert infer_commands(m) == (
        DesiredCommand("*", CommandType.STANDARD, ("twitch",), None),
        DesiredCommand("ping", CommandType.STANDARD, ("discord",), None),
    )


def test_bare_non_chat_event_type_is_wildcard() -> None:
    m = _manifest((_rule("discord", event_types=("member.join",)),))
    assert infer_commands(m) == (DesiredCommand("*", CommandType.STANDARD, ("discord",), None),)


def test_unsupported_platform_rule_skipped() -> None:
    m = _manifest(
        (_rule("discord", ("!ping",)), _rule("twitch", ("!ping",))), supported=("discord",)
    )
    assert infer_commands(m) == (DesiredCommand("ping", CommandType.STANDARD, ("discord",), None),)


@pytest.mark.parametrize("p", ["!Ping", "!", "ping", "!a b", "!" + "x" * 33, "!ping\n"])
def test_unusable_prefix_skipped_and_warned(p: str, caplog: pytest.LogCaptureFixture) -> None:
    m = _manifest((_rule("discord", (p,)),))
    with caplog.at_level(logging.WARNING):
        result = infer_commands(m)
    assert result == ()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unusable command_prefix" in r.getMessage() for r in warnings)
    record = next(r for r in warnings if "unusable command_prefix" in r.getMessage())
    assert record.app_id == _APP_ID
    assert record.count == 1
    assert p not in record.getMessage()  # prefix text is never logged


def test_explicit_declaration_overrides_same_name() -> None:
    decl = CommandDeclaration(
        name="ping",
        types=CommandType.STANDARD | CommandType.WEBHOOK,
        platforms=("discord",),
        description="Ping the bot",
    )
    m = _manifest((_rule("discord", ("!ping",)), _rule("twitch", ("!ping",))), declarations=(decl,))
    assert resolve_desired_commands(m) == (
        DesiredCommand(
            "ping", CommandType.STANDARD | CommandType.WEBHOOK, ("discord",), "Ping the bot"
        ),
    )


def test_explicit_declaration_augments() -> None:
    decl = CommandDeclaration(
        name="slashonly", types=CommandType.WEBHOOK, platforms=("discord",), description=None
    )
    m = _manifest((_rule("discord", ("!ping",)),), declarations=(decl,))
    assert resolve_desired_commands(m) == (
        DesiredCommand("ping", CommandType.STANDARD, ("discord",), None),
        DesiredCommand("slashonly", CommandType.WEBHOOK, ("discord",), None),
    )


def test_explicit_default_platforms_follow_consumes() -> None:
    decl = CommandDeclaration(
        name="x", types=CommandType.STANDARD, platforms=None, description=None
    )
    m = _manifest(
        (_rule("twitch", ("!a",)), _rule("discord", ("!a",)), _rule("slack", ("!a",))),
        supported=("discord", "twitch"),
        declarations=(decl,),
    )
    row = next(r for r in resolve_desired_commands(m) if r.command == "x")
    assert row.platforms == ("discord", "twitch")


def test_explicit_default_platforms_fall_back_to_supported_then_wildcard() -> None:
    decl = CommandDeclaration(
        name="x", types=CommandType.STANDARD, platforms=None, description=None
    )
    assert resolve_desired_commands(_manifest((), supported=("kick",), declarations=(decl,))) == (
        DesiredCommand("x", CommandType.STANDARD, ("kick",), None),
    )
    assert resolve_desired_commands(_manifest((), declarations=(decl,))) == (
        DesiredCommand("x", CommandType.STANDARD, ("*",), None),
    )


def test_verb_from_prefix_cases() -> None:
    assert verb_from_prefix("!ping") == "ping"
    assert verb_from_prefix("?x") == "x"
    assert verb_from_prefix("ping") is None


def test_every_core_hub_manifest_infers_a_row() -> None:
    paths = sorted((_REPO_ROOT / "bundles").glob("*/*/hub-manifest.yaml"))
    examined = 0
    for path in paths:
        raw = yaml.safe_load(path.read_text())
        manifest = parse_bundle_manifest_v2(
            raw,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
        examined += 1
        assert resolve_desired_commands(manifest), f"{path}: resolved zero rows"
        expected = {
            p[1:]
            for rule in manifest.consumes
            if manifest.supports_platform(rule.platform)
            for p in (rule.filters.get("command_prefix") or [])
        }
        inferred = {r.command for r in infer_commands(manifest) if r.command != "*"}
        assert inferred == expected, f"{path}: inferred {inferred} != declared {expected}"
    assert examined == len(paths), f"examined {examined} != globbed {len(paths)}"
    assert examined >= 80, f"parity guard: examined only {examined} manifests (expected >= 80)"
    print(f"examined {examined} hub-manifest.yaml files")
