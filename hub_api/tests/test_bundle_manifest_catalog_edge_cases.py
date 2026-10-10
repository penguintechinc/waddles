"""Edge-case coverage for `bundle_manifest_v2` + `bundle_permission_catalog`.

Targets the branches the primary suites leave dark: bare-string vs V2-dict permission shapes,
`supported_platforms` validation, per-permission `params` mutual-exclusion rules, the
env-configured cluster CIDR deny-list (a security boundary: bundles must never egress to the
platform's own pod/service/node ranges), and instance-policy family collapsing.
"""

from __future__ import annotations

import pytest

from services import bundle_permission_catalog as cat
from services.bundle_manifest_v2 import (
    ManifestV2Error,
    parse_bundle_manifest_v2,
    parse_permission_declarations,
)

_BASE = {
    "schema_version": 2,
    "app_id": "waddles.socials.edge.default",
    "name": "Edge",
    "version": "1.0.0",
    "feature": "waddles.socials.edge",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "bundles.edge:transform",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        },
    },
}


def _parse(**extra: object) -> object:
    return parse_bundle_manifest_v2(
        {**_BASE, **extra},
        known_custom_platforms=frozenset({"mygame"}),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


class TestPermissionShapes:
    def test_bare_string_permissions_parse_to_no_structured_declarations(self) -> None:
        m = _parse(permissions=["storage.kv"])
        assert m.permissions == ("storage.kv",)  # type: ignore[attr-defined]
        assert m.permission_declarations == ()  # type: ignore[attr-defined]

    def test_v2_dict_permissions_parse_to_declarations_with_risk(self) -> None:
        m = _parse(permissions=[{"id": "storage.kv", "justification": "counters"}])
        assert m.permissions == ()  # type: ignore[attr-defined]
        decl = m.permission_declarations  # type: ignore[attr-defined]
        assert [(d.id, d.risk) for d in decl] == [("storage.kv", "normal")]

    def test_mixed_shapes_keep_each_in_its_own_field(self) -> None:
        m = _parse(permissions=["legacy.thing", {"id": "storage.kv", "justification": "x"}])
        assert m.permissions == ("legacy.thing",)  # type: ignore[attr-defined]
        assert [d.id for d in m.permission_declarations] == ["storage.kv"]  # type: ignore[attr-defined]

    def test_unknown_permission_id_is_rejected_loudly(self) -> None:
        with pytest.raises(ManifestV2Error) as exc:
            parse_permission_declarations([{"id": "totally.made.up", "justification": "x"}])
        assert exc.value.reason == "unknown_permission"

    def test_missing_or_oversized_justification_rejected(self) -> None:
        with pytest.raises(ManifestV2Error) as exc:
            parse_permission_declarations([{"id": "storage.kv"}])
        assert exc.value.reason == "missing_justification"
        with pytest.raises(ManifestV2Error):
            parse_permission_declarations([{"id": "storage.kv", "justification": "x" * 5000}])

    def test_methods_param_only_valid_on_net_http(self) -> None:
        with pytest.raises(ManifestV2Error) as exc:
            parse_permission_declarations(
                [{"id": "storage.kv", "justification": "x", "params": {"methods": ["GET"]}}]
            )
        assert exc.value.reason == "invalid_net_http_method"

    def test_net_http_requires_valid_method_subset(self) -> None:
        for bad in (None, [], ["TRACE"]):
            with pytest.raises(ManifestV2Error) as exc:
                parse_permission_declarations(
                    [
                        {
                            "id": "net.http.fqdn:api.example.com",
                            "justification": "x",
                            "params": {"methods": bad},
                        }
                    ]
                )
            assert exc.value.reason == "invalid_net_http_method"

    def test_storage_tables_schema_mutual_requirement(self) -> None:
        with pytest.raises(ManifestV2Error) as exc:
            parse_permission_declarations([{"id": "storage.tables", "justification": "x"}])
        assert exc.value.reason == "storage_tables_requires_schema"
        with pytest.raises(ManifestV2Error) as exc2:
            parse_permission_declarations(
                [{"id": "storage.kv", "justification": "x", "params": {"schema": "s"}}]
            )
        assert exc2.value.reason == "storage_tables_requires_schema"
        ok = parse_permission_declarations(
            [{"id": "storage.tables", "justification": "x", "params": {"schema": "s"}}]
        )
        assert ok[0].params == {"schema": "s"}


class TestSupportedPlatforms:
    def test_absent_means_every_platform(self) -> None:
        m = _parse()
        assert m.supported_platforms is None  # type: ignore[attr-defined]
        assert m.supports_platform("anything")  # type: ignore[attr-defined]

    def test_listed_platforms_gate_support(self) -> None:
        m = _parse(supported_platforms=["twitch", "custom:mygame"])
        assert m.supports_platform("twitch")  # type: ignore[attr-defined]
        assert not m.supports_platform("discord")  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "bad", [[], "twitch", [1], ["custom:unregistered"]], ids=["empty", "str", "int", "custom"]
    )
    def test_malformed_lists_rejected(self, bad: object) -> None:
        with pytest.raises(ManifestV2Error) as exc:
            _parse(supported_platforms=bad)
        assert exc.value.reason == "invalid_supported_platforms"


class TestConsumesWildcards:
    def test_wildcard_platform_rejected_when_not_allowed(self) -> None:
        stages = {
            "process": {
                "entry": "bundles.edge:transform",
                "consumes": [{"platform": "*", "event_types": ["chat.message"]}],
            }
        }
        with pytest.raises(ManifestV2Error) as exc:
            _parse(stages=stages)
        assert exc.value.reason == "wildcard_consumes_not_allowed"

    def test_double_star_event_type_rejected_when_not_allowed(self) -> None:
        stages = {
            "process": {
                "entry": "bundles.edge:transform",
                "consumes": [{"platform": "twitch", "event_types": ["chat.**"]}],
            }
        }
        with pytest.raises(ManifestV2Error) as exc:
            _parse(stages=stages)
        assert exc.value.reason == "wildcard_consumes_not_allowed"

    def test_unregistered_custom_consumes_platform_rejected(self) -> None:
        stages = {
            "process": {
                "entry": "bundles.edge:transform",
                "consumes": [{"platform": "custom:nope", "event_types": ["x"]}],
            }
        }
        with pytest.raises(ManifestV2Error) as exc:
            _parse(stages=stages)
        assert exc.value.reason == "unknown_consumes_platform"


class TestClusterDenyNetworks:
    def test_env_cidrs_parsed_and_malformed_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLUSTER_POD_CIDR", "10.42.0.0/16, not-a-cidr")
        monkeypatch.setenv("CLUSTER_SERVICE_CIDR", "10.43.0.0/16")
        monkeypatch.setenv("CLUSTER_NODE_CIDR", "")
        nets = {str(n) for n in cat._cluster_deny_networks()}
        assert nets == {"10.42.0.0/16", "10.43.0.0/16"}

    def test_unset_env_yields_no_cluster_networks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("CLUSTER_POD_CIDR", "CLUSTER_SERVICE_CIDR", "CLUSTER_NODE_CIDR"):
            monkeypatch.delenv(var, raising=False)
        assert cat._cluster_deny_networks() == ()

    def test_private_ip_inside_cluster_cidr_is_not_grantable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLUSTER_POD_CIDR", "10.42.0.0/16")
        assert not cat.is_valid_private_ip_or_cidr("10.42.1.5")
        assert cat.resolve_risk("net.http.private-ip:10.42.1.5") is None

    @pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "fe80::1", "::1"])
    def test_always_denied_ranges_never_valid_private_targets(self, ip: str) -> None:
        assert not cat.is_valid_private_ip_or_cidr(ip)


class TestFamilyAndRisk:
    @pytest.mark.parametrize(
        ("pid", "family"),
        [
            ("storage.objects", "storage.objects"),
            ("net.http.fqdn:api.example.com", "net.http.fqdn"),
            ("chat.send:twitch", "chat.send"),
            ("moderation.discord", "moderation"),
            # family collapse is shape-only; provider validity is `resolve_risk`'s job (below)
            ("chat.send:notaprovider", "chat.send"),
        ],
    )
    def test_permission_family_collapses_parameterized_ids(self, pid: str, family: str) -> None:
        assert cat.permission_family(pid) == family

    def test_chat_send_normal_moderation_dangerous_for_relay_providers_only(self) -> None:
        assert cat.resolve_risk("chat.send:twitch") == "normal"
        assert cat.resolve_risk("moderation.twitch") == "dangerous"
        assert cat.resolve_risk("chat.send:notaprovider") is None
        assert cat.resolve_risk("moderation.notaprovider") is None
