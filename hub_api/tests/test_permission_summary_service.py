"""Tests for build_permission_summary()/canonical_json()/permission_hash()."""

from __future__ import annotations

from services.bundle_manifest_v2 import BundleManifestV2, ConsumeRule, EgressRule, Limits
from services.permission_summary_service import (
    build_permission_summary,
    canonical_json,
    permission_hash,
)

_MANIFEST = BundleManifestV2(
    schema_version=2,
    app_id="waddles.socials.music.default",
    name="Music Station",
    version="3.0.0",
    feature="waddles.socials.music",
    module="socials",
    provider="builtin",
    language="python",
    artifact="source",
    execution_model="native",
    is_default=True,
    stages={"process": {}},
    egress=(EgressRule(host="api.spotify.com", methods=("GET", "POST")),),
    data_tables=("music_queue",),
    limits=Limits(timeout_ms=2000, memory_mb=64, egress_rps=10),
    permissions=(),
    routes_to=("waddles.community.forums.default",),
    consumes=(
        ConsumeRule(platform="twitch", source_id=None, event_types=("chat.message",), filters={}),
    ),
)


def _summary() -> dict:
    return build_permission_summary(
        _MANIFEST,
        grant_labels=[
            {"platform": "twitch", "sourceId": "tw-channelA", "label": "Twitch #channelA"}
        ],
        component_capabilities=frozenset({"http", "db", "kv"}),
        min_tier="free",
        flag_key="waddles.socials.music",
        allow_private_hosts=False,
    )


def test_summary_lists_grant_labels_in_words() -> None:
    summary = _summary()
    assert summary["streams"] == [
        {"platform": "twitch", "sourceId": "tw-channelA", "label": "Twitch #channelA"}
    ]


def test_summary_lists_routes_to() -> None:
    summary = _summary()
    assert summary["routesTo"] == ["waddles.community.forums.default"]


def test_summary_lists_capabilities_actually_imported() -> None:
    summary = _summary()
    assert set(summary["capabilities"]) == {"http", "db", "kv"}


def test_two_calls_with_identical_inputs_produce_the_same_hash() -> None:
    hash1 = permission_hash(_summary())
    hash2 = permission_hash(_summary())
    assert hash1 == hash2
    assert hash1.startswith("sha256:")
    assert len(hash1) == len("sha256:") + 64


def test_canonical_json_has_sorted_keys_and_no_insignificant_whitespace() -> None:
    text = canonical_json({"b": 1, "a": 2})
    assert text == '{"a":2,"b":1}'


def test_widened_capability_changes_the_hash() -> None:
    narrow = permission_hash(_summary())
    summary = build_permission_summary(
        _MANIFEST,
        grant_labels=[
            {"platform": "twitch", "sourceId": "tw-channelA", "label": "Twitch #channelA"}
        ],
        component_capabilities=frozenset({"http", "db", "kv", "relay"}),  # widened
        min_tier="free",
        flag_key="waddles.socials.music",
        allow_private_hosts=False,
    )
    assert permission_hash(summary) != narrow


def test_unusual_flags_a_private_hosts_egress_request() -> None:
    summary = build_permission_summary(
        _MANIFEST,
        grant_labels=[],
        component_capabilities=frozenset({"http"}),
        min_tier="free",
        flag_key="waddles.socials.music",
        allow_private_hosts=True,
    )
    assert "allow_private_hosts" in summary["unusual"]


def test_unusual_flags_routes_to() -> None:
    summary = _summary()
    assert "routes_to" in summary["unusual"]


def test_unusual_empty_when_nothing_unusual() -> None:
    plain_manifest = BundleManifestV2(
        schema_version=2,
        app_id="waddles.socials.music.default",
        name="Music Station",
        version="3.0.0",
        feature="waddles.socials.music",
        module="socials",
        provider="builtin",
        language="python",
        artifact="source",
        execution_model="native",
        is_default=True,
        stages={"process": {}},
        egress=(),
        data_tables=(),
        limits=Limits(timeout_ms=2000, memory_mb=64, egress_rps=10),
        permissions=(),
        routes_to=(),
        consumes=(),
    )
    summary = build_permission_summary(
        plain_manifest,
        grant_labels=[],
        component_capabilities=frozenset({"kv"}),
        min_tier="free",
        flag_key="waddles.socials.music",
        allow_private_hosts=False,
    )
    assert summary["unusual"] == []
