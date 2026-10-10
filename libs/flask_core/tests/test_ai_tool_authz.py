"""`flask_core.ai_tool_authz` -- a model-requested tool call can never exceed the invoking user.

regression: sec-llm01-hardening. The real authoriser runs end to end against a real
`ToolRegistry`; the feature-flag backend (PostHog) is the one injected stand-in, exactly as
`flask_core.mcp_server`'s tests inject `check`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from flask_core import ai_tool_authz as authz
from flask_core.ai_tool_authz import (
    DENIAL_REASONS,
    EMPTY_REGISTRY,
    AuthorizedToolCall,
    InvocationContext,
    ToolCallDenied,
    ToolCallRequest,
    ToolParam,
    ToolRegistry,
    ToolSpec,
    authorize_tool_call,
    authorize_tool_calls,
    extract_tool_calls,
    is_reserved_argument,
    reject_unsolicited_tool_calls,
)

TENANT = "acme-corp"
OTHER_TENANT = "other-corp"


async def flags_on(flag: str, *, tenant: str, community: int | None, default: bool) -> bool:
    return True


async def flags_off(flag: str, *, tenant: str, community: int | None, default: bool) -> bool:
    return False


async def flags_boom(flag: str, *, tenant: str, community: int | None, default: bool) -> bool:
    raise ConnectionError("posthog unreachable")


def make_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name="community.announce",
                required_scopes=("announcements:write",),
                parameters=(
                    ToolParam("message", "string", max_length=50),
                    ToolParam("pin", "boolean", required=False),
                    ToolParam("level", "string", required=False, choices=("info", "warn")),
                    ToolParam("code", "string", required=False, pattern=r"[A-Z]{3}-\d{2}"),
                    ToolParam("count", "integer", required=False),
                    ToolParam("ratio", "number", required=False),
                ),
                flag="waddles.ai.tools.announce",
            ),
            ToolSpec(
                name="community.lookup",
                required_scopes=("community:read",),
                parameters=(ToolParam("query", "string"),),
                side_effects=False,
            ),
            ToolSpec(
                name="platform.purge",
                required_scopes=("platform:admin",),
                flag="waddles.ai.tools.purge",
            ),
            ToolSpec(
                name="platform.status",
                required_scopes=("platform:read",),
                side_effects=False,
                community_scoped=False,
            ),
        ]
    )


def make_ctx(**overrides: Any) -> InvocationContext:
    base: dict[str, Any] = {
        "tenant": TENANT,
        "community_id": 7,
        "user_id": 42,
        "granted_scopes": frozenset({"announcements:write", "*:read"}),
    }
    base.update(overrides)
    return InvocationContext(**base)


def call(name: str, **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(name=name, arguments=arguments, call_id="call_1")


async def denied(ctx: InvocationContext, request: ToolCallRequest, **kw: Any) -> ToolCallDenied:
    kw.setdefault("flag_check", flags_on)
    with pytest.raises(ToolCallDenied) as info:
        await authorize_tool_call(ctx, request, make_registry(), **kw)
    return info.value


class TestHappyPath:
    async def test_binds_identity_from_the_context_not_the_model(self) -> None:
        got = await authorize_tool_call(
            make_ctx(), call("community.announce", message="hello", pin=True), make_registry(),
            flag_check=flags_on,
        )  # fmt: skip
        assert isinstance(got, AuthorizedToolCall)
        assert (got.tenant, got.community_id, got.user_id) == (TENANT, 7, 42)
        assert dict(got.arguments) == {"message": "hello", "pin": True}
        assert got.call_id == "call_1"

    async def test_arguments_are_read_only(self) -> None:
        got = await authorize_tool_call(
            make_ctx(),
            call("community.lookup", query="build"),
            make_registry(),
            flag_check=flags_on,
        )
        with pytest.raises(TypeError):
            got.arguments["query"] = "x"  # type: ignore[index]

    async def test_wildcard_resource_scope_covers_a_read_tool(self) -> None:
        got = await authorize_tool_call(
            make_ctx(), call("community.lookup", query="q"), make_registry(), flag_check=flags_on
        )
        assert got.name == "community.lookup"

    async def test_non_community_scoped_tool_binds_no_community(self) -> None:
        ctx = make_ctx(community_id=None, granted_scopes=frozenset({"platform:read"}))
        got = await authorize_tool_call(ctx, call("platform.status"), make_registry())
        assert got.community_id is None and got.tenant == TENANT

    async def test_all_argument_types_validate(self) -> None:
        args = {"message": "m", "level": "warn", "code": "ABC-12", "count": 3, "ratio": 0.5}
        got = await authorize_tool_call(
            make_ctx(), call("community.announce", **args), make_registry(), flag_check=flags_on
        )
        assert dict(got.arguments) == args


class TestScopeAndTenantCannotBeExceeded:
    async def test_missing_scope_is_denied(self) -> None:
        ctx = make_ctx(granted_scopes=frozenset({"community:read"}))
        exc = await denied(ctx, call("community.announce", message="hi"))
        assert exc.reason == authz.REASON_SCOPE_DENIED and exc.tool_known

    async def test_no_scopes_at_all_is_denied(self) -> None:
        exc = await denied(
            make_ctx(granted_scopes=frozenset()), call("community.lookup", query="x")
        )
        assert exc.reason == authz.REASON_SCOPE_DENIED

    async def test_wildcard_read_does_not_cover_a_write_or_admin_tool(self) -> None:
        ctx = make_ctx(granted_scopes=frozenset({"*:read"}))
        assert (await denied(ctx, call("community.announce", message="x"))).reason == "scope_denied"
        assert (await denied(ctx, call("platform.purge"))).reason == "scope_denied"

    async def test_bare_star_scope_grants_nothing(self) -> None:
        ctx = make_ctx(granted_scopes=frozenset({"*", "*:*"}))
        assert (await denied(ctx, call("platform.purge"))).reason == "scope_denied"

    async def test_model_cannot_name_another_tenant(self) -> None:
        # The injected instruction "run this for tenant other-corp" -- refused outright.
        req = call("community.announce", message="hi", tenant=OTHER_TENANT)
        assert (await denied(make_ctx(), req)).reason == authz.REASON_RESERVED_ARGUMENT

    @pytest.mark.parametrize(
        "key",
        [
            "tenant", "tenant_id", "tenantId", "Tenant-ID", "tenant_slug", "community_id",
            "communityId", "community", "user_id", "userId", "user", "actor", "actor_user_id",
            "sub", "scope", "scopes", "role", "roles", "team_id", "org_id", "impersonate",
            "impersonate_user", "on_behalf_of", "as_user", "principal", "claims", "jwt", "token",
        ],
    )  # fmt: skip
    async def test_identity_arguments_are_refused_even_when_they_match_the_caller(
        self, key: str
    ) -> None:
        value: Any = TENANT if "tenant" in key.lower() else 7
        req = call("community.announce", **{"message": "hi", key: value})
        exc = await denied(make_ctx(), req)
        assert exc.reason == authz.REASON_RESERVED_ARGUMENT

    async def test_reserved_check_wins_even_for_an_unknown_tool(self) -> None:
        exc = await denied(make_ctx(), call("nope.nothing", tenant_id=3))
        assert exc.reason == authz.REASON_RESERVED_ARGUMENT and not exc.tool_known

    async def test_scope_escalation_via_arguments_is_refused(self) -> None:
        req = call("community.lookup", query="x", scopes=["platform:admin"])
        assert (await denied(make_ctx(), req)).reason == authz.REASON_RESERVED_ARGUMENT

    async def test_community_scoped_tool_needs_a_community_in_context(self) -> None:
        ctx = make_ctx(community_id=None)
        exc = await denied(ctx, call("community.lookup", query="x"))
        assert exc.reason == authz.REASON_NO_COMMUNITY

    async def test_scope_is_checked_before_the_feature_flag_is_consulted(self) -> None:
        calls: list[str] = []

        async def spy(flag: str, **kw: Any) -> bool:
            calls.append(flag)
            return True

        ctx = make_ctx(granted_scopes=frozenset())
        with pytest.raises(ToolCallDenied):
            await authorize_tool_call(
                ctx, call("community.announce", message="x"), make_registry(), flag_check=spy
            )
        assert calls == []


class TestRegistryMembership:
    async def test_unknown_tool_is_denied(self) -> None:
        exc = await denied(make_ctx(), call("community.delete_everything"))
        assert exc.reason == authz.REASON_UNKNOWN_TOOL and not exc.tool_known
        assert exc.tool == "unknown"

    async def test_empty_registry_exposes_no_tools(self) -> None:
        with pytest.raises(ToolCallDenied) as info:
            await authorize_tool_call(make_ctx(), call("anything"), EMPTY_REGISTRY)
        assert info.value.reason == authz.REASON_NO_TOOLS_EXPOSED

    async def test_non_string_tool_name_never_matches(self) -> None:
        bad = ToolCallRequest(name=123, arguments={})  # type: ignore[arg-type]
        assert (await denied(make_ctx(), bad)).reason == authz.REASON_UNKNOWN_TOOL

    async def test_tool_names_are_case_and_whitespace_exact(self) -> None:
        for name in ("Community.Lookup", " community.lookup", "community.lookup "):
            assert (await denied(make_ctx(), call(name, query="x"))).reason == "unknown_tool"


class TestArgumentSchema:
    async def test_unexpected_argument_is_denied(self) -> None:
        req = call("community.announce", message="hi", webhook="https://evil.example")
        assert (await denied(make_ctx(), req)).reason == authz.REASON_UNKNOWN_ARGUMENT

    async def test_missing_required_argument_is_denied(self) -> None:
        assert (await denied(make_ctx(), call("community.announce"))).reason == "missing_argument"

    @pytest.mark.parametrize(
        "bad",
        [
            {"message": 5},
            {"message": "x" * 51},
            {"message": "ok", "pin": "yes"},
            {"message": "ok", "level": "critical"},
            {"message": "ok", "code": "abc-1"},
            {"message": "ok", "count": True},
            {"message": "ok", "count": 1.5},
            {"message": "ok", "ratio": True},
            {"message": "ok", "ratio": "0.5"},
            {"message": {"nested": "object"}},
            {"message": ["list"]},
        ],
    )
    async def test_wrongly_typed_or_out_of_bounds_values_are_denied(
        self, bad: dict[str, Any]
    ) -> None:
        exc = await denied(make_ctx(), call("community.announce", **bad))
        assert exc.reason == authz.REASON_INVALID_ARGUMENT and exc.tool_known

    async def test_non_string_argument_keys_are_denied(self) -> None:
        req = ToolCallRequest(name="community.lookup", arguments={1: "x"})  # type: ignore[dict-item]
        assert (await denied(make_ctx(), req)).reason == authz.REASON_UNKNOWN_ARGUMENT

    async def test_parse_error_is_malformed(self) -> None:
        req = ToolCallRequest(name="community.lookup", parse_error="malformed_call")
        assert (await denied(make_ctx(), req)).reason == authz.REASON_MALFORMED_CALL


class TestTaintPolicy:
    async def test_tainted_context_refuses_side_effecting_tools(self) -> None:
        ctx = make_ctx().with_taint()
        exc = await denied(ctx, call("community.announce", message="hi"))
        assert exc.reason == authz.REASON_TAINTED_CONTEXT

    async def test_tainted_context_still_allows_a_scoped_read_only_tool(self) -> None:
        ctx = make_ctx().with_taint()
        got = await authorize_tool_call(ctx, call("community.lookup", query="q"), make_registry())
        assert got.name == "community.lookup"

    async def test_taint_does_not_bypass_the_scope_check_on_read_tools(self) -> None:
        ctx = make_ctx(granted_scopes=frozenset(), tainted=True)
        assert (await denied(ctx, call("community.lookup", query="q"))).reason == "scope_denied"

    def test_with_taint_preserves_identity_and_ratchets(self) -> None:
        ctx = make_ctx()
        tainted = ctx.with_taint()
        assert not ctx.tainted and tainted.tainted
        assert (tainted.tenant, tainted.community_id, tainted.user_id) == (TENANT, 7, 42)
        assert tainted.granted_scopes == ctx.granted_scopes
        assert tainted.with_taint().tainted


class TestFeatureFlag:
    async def test_flag_off_denies(self) -> None:
        exc = await denied(
            make_ctx(), call("community.announce", message="x"), flag_check=flags_off
        )
        assert exc.reason == authz.REASON_FEATURE_DISABLED

    async def test_flag_backend_failure_denies_never_allows(self) -> None:
        exc = await denied(
            make_ctx(), call("community.announce", message="x"), flag_check=flags_boom
        )
        assert exc.reason == authz.REASON_FLAG_CHECK_FAILED and exc.tool_known

    async def test_flag_is_evaluated_for_the_invoking_tenant_and_community(self) -> None:
        seen: dict[str, Any] = {}

        async def spy(flag: str, *, tenant: str, community: int | None, default: bool) -> bool:
            seen.update(flag=flag, tenant=tenant, community=community, default=default)
            return True

        await authorize_tool_call(
            make_ctx(), call("community.announce", message="x"), make_registry(), flag_check=spy
        )
        assert seen == {
            "flag": "waddles.ai.tools.announce",
            "tenant": TENANT,
            "community": 7,
            "default": False,
        }

    async def test_default_flag_check_is_the_platform_feature_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from flask_core import feature_flags

        seen: list[str] = []

        async def fake(flag: str, **kw: Any) -> bool:
            seen.append(flag)
            return False

        monkeypatch.setattr(feature_flags, "feature_enabled", fake)
        exc = await denied(make_ctx(), call("community.announce", message="x"), flag_check=None)
        assert exc.reason == authz.REASON_FEATURE_DISABLED and seen == ["waddles.ai.tools.announce"]

    async def test_tool_without_a_flag_skips_the_check(self) -> None:
        async def never(*a: Any, **k: Any) -> bool:  # pragma: no cover - must not run
            raise AssertionError("flag consulted for a flag-less tool")

        got = await authorize_tool_call(
            make_ctx(), call("community.lookup", query="x"), make_registry(), flag_check=never
        )
        assert got.name == "community.lookup"


class TestBatch:
    async def test_empty_batch_returns_empty(self) -> None:
        assert await authorize_tool_calls(make_ctx(), (), make_registry()) == ()

    async def test_all_authorised_calls_are_returned_in_order(self) -> None:
        got = await authorize_tool_calls(
            make_ctx(),
            [call("community.lookup", query="a"), call("community.lookup", query="b")],
            make_registry(),
        )
        assert [dict(g.arguments)["query"] for g in got] == ["a", "b"]

    async def test_one_denied_call_denies_the_whole_batch(self) -> None:
        batch = [call("community.lookup", query="ok"), call("platform.purge")]
        with pytest.raises(ToolCallDenied) as info:
            await authorize_tool_calls(make_ctx(), batch, make_registry())
        assert info.value.reason == authz.REASON_SCOPE_DENIED

    async def test_too_many_calls_is_denied(self) -> None:
        batch = [call("community.lookup", query="x")] * (authz.DEFAULT_MAX_TOOL_CALLS + 1)
        with pytest.raises(ToolCallDenied) as info:
            await authorize_tool_calls(make_ctx(), batch, make_registry())
        assert info.value.reason == authz.REASON_TOO_MANY_CALLS

    async def test_max_calls_is_configurable(self) -> None:
        batch = [call("community.lookup", query="x")] * 2
        with pytest.raises(ToolCallDenied):
            await authorize_tool_calls(make_ctx(), batch, make_registry(), max_calls=1)


class TestExtractToolCalls:
    def test_openai_tool_calls(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_a",
                                "type": "function",
                                "function": {
                                    "name": "community.lookup",
                                    "arguments": '{"query": "x"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        (got,) = extract_tool_calls(payload, provider="openai")
        assert (got.name, dict(got.arguments), got.call_id) == (
            "community.lookup",
            {"query": "x"},
            "call_a",
        )
        assert got.parse_error is None

    def test_openai_legacy_function_call(self) -> None:
        payload = {"choices": [{"message": {"function_call": {"name": "f", "arguments": "{}"}}}]}
        (got,) = extract_tool_calls(payload, provider="openai")
        assert got.name == "f" and dict(got.arguments) == {}

    def test_openai_plain_answer_has_no_calls(self) -> None:
        payload = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        assert extract_tool_calls(payload, provider="openai") == ()

    @pytest.mark.parametrize(
        "payload",
        [
            {"choices": "nope"},
            {"choices": []},
            {"choices": [None]},
            {"choices": [{"message": {"tool_calls": "x"}}]},
            {"choices": [{"message": {"tool_calls": [None]}}]},
            {"choices": [{"message": {"tool_calls": [{"function": "x"}]}}]},
            {
                "choices": [
                    {"message": {"tool_calls": [{"function": {"name": "", "arguments": "{}"}}]}}
                ]
            },
            {"choices": [{"message": {"function_call": "x"}}]},
            {"choices": [{"message": {"content": None}, "finish_reason": "tool_calls"}]},
            {"choices": [{"message": {"content": None}, "finish_reason": "function_call"}]},
        ],
    )
    def test_openai_malformed_tool_turns_become_malformed_requests(self, payload: Any) -> None:
        calls = extract_tool_calls(payload, provider="openai")
        assert calls and all(c.parse_error == authz.REASON_MALFORMED_CALL for c in calls)

    @pytest.mark.parametrize(
        "arguments",
        ['{"query": ', "[1, 2]", '"str"', "x" * (authz.MAX_ARGUMENTS_CHARS + 1), 42],
    )
    def test_undecodable_or_oversized_arguments_are_malformed(self, arguments: Any) -> None:
        payload = {
            "choices": [
                {"message": {"tool_calls": [{"function": {"name": "t", "arguments": arguments}}]}}
            ]
        }
        (got,) = extract_tool_calls(payload, provider="openai")
        assert got.parse_error == authz.REASON_MALFORMED_CALL

    def test_oversized_mapping_arguments_are_malformed(self) -> None:
        big = {"k": "v" * (authz.MAX_ARGUMENTS_CHARS + 1)}
        payload = {"message": {"tool_calls": [{"function": {"name": "t", "arguments": big}}]}}
        (got,) = extract_tool_calls(payload, provider="ollama")
        assert got.parse_error == authz.REASON_MALFORMED_CALL

    def test_overlong_call_id_is_dropped(self) -> None:
        fn = {"name": "t", "arguments": "{}"}
        payload = {"choices": [{"message": {"tool_calls": [{"id": "i" * 500, "function": fn}]}}]}
        (got,) = extract_tool_calls(payload, provider="openai")
        assert got.call_id is None

    def test_anthropic_tool_use_blocks(self) -> None:
        payload = {
            "content": [
                {"type": "text", "text": "ok"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "community.lookup",
                    "input": {"query": "z"},
                },
            ],
            "stop_reason": "tool_use",
        }
        (got,) = extract_tool_calls(payload, provider="anthropic")
        assert (got.name, dict(got.arguments), got.call_id) == (
            "community.lookup",
            {"query": "z"},
            "tu_1",
        )

    def test_missing_arguments_decode_to_an_empty_mapping(self) -> None:
        payload = {"message": {"tool_calls": [{"function": {"name": "platform.status"}}]}}
        (got,) = extract_tool_calls(payload, provider="ollama")
        assert dict(got.arguments) == {} and got.parse_error is None

    def test_anthropic_non_mapping_blocks_are_ignored(self) -> None:
        payload = {
            "content": ["text", None, {"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
        }
        assert extract_tool_calls(payload, provider="anthropic") == ()

    def test_anthropic_non_list_content_has_no_calls(self) -> None:
        assert extract_tool_calls({"content": "plain string"}, provider="anthropic") == ()

    def test_anthropic_text_only_has_no_calls(self) -> None:
        payload = {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}
        assert extract_tool_calls(payload, provider="anthropic") == ()

    @pytest.mark.parametrize(
        "payload",
        [
            {"content": [], "stop_reason": "tool_use"},
            {"content": [{"type": "tool_use", "name": None, "input": {}}]},
            {"content": [{"type": "tool_use", "name": "t", "input": "str"}]},
        ],
    )
    def test_anthropic_malformed_tool_turns(self, payload: Any) -> None:
        calls = extract_tool_calls(payload, provider="anthropic")
        assert calls and all(c.parse_error for c in calls)

    def test_ollama_chat_tool_calls(self) -> None:
        payload = {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "community.lookup", "arguments": {"query": "q"}}}
                ],
            }
        }
        (got,) = extract_tool_calls(payload, provider="ollama")
        assert (got.name, dict(got.arguments)) == ("community.lookup", {"query": "q"})

    @pytest.mark.parametrize(
        "payload",
        [
            {"response": "plain /api/generate answer", "done": True},
            {"message": {"content": "hi"}},
            {"message": "str"},
        ],
    )
    def test_ollama_without_tool_calls(self, payload: Any) -> None:
        assert extract_tool_calls(payload, provider="ollama") == ()

    @pytest.mark.parametrize(
        "payload",
        [{"message": {"tool_calls": "x"}}, {"message": {"tool_calls": [1]}}],
    )
    def test_ollama_malformed(self, payload: Any) -> None:
        calls = extract_tool_calls(payload, provider="ollama")
        assert calls and all(c.parse_error for c in calls)

    @pytest.mark.parametrize("payload", [None, "str", 5, [], ()])
    def test_non_mapping_payload_has_no_calls(self, payload: Any) -> None:
        for provider in ("openai", "anthropic", "ollama"):
            assert extract_tool_calls(payload, provider=provider) == ()  # type: ignore[arg-type]

    def test_openai_payload_without_choices_has_no_calls(self) -> None:
        assert extract_tool_calls({"usage": {}}, provider="openai") == ()


class TestRejectUnsolicited:
    def test_tool_call_from_a_no_tools_surface_is_denied(self) -> None:
        payload = {"message": {"tool_calls": [{"function": {"name": "x", "arguments": {}}}]}}
        with pytest.raises(ToolCallDenied) as info:
            reject_unsolicited_tool_calls(payload, provider="ollama", surface="chat_reply")
        assert info.value.reason == authz.REASON_NO_TOOLS_EXPOSED

    def test_clean_response_passes(self) -> None:
        reject_unsolicited_tool_calls({"response": "hi"}, provider="ollama", surface="research")

    def test_denial_does_not_log_the_model_chosen_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = {
            "choices": [{"message": {"function_call": {"name": "rm_rf_secret", "arguments": "{}"}}}]
        }
        with caplog.at_level(logging.WARNING, logger="flask_core.ai_tool_authz"):
            with pytest.raises(ToolCallDenied):
                reject_unsolicited_tool_calls(payload, provider="openai", surface="chat_reply")
        assert "ai_tool_call_denied" in caplog.text and "rm_rf_secret" not in caplog.text


class TestDeclarations:
    def test_context_requires_a_tenant(self) -> None:
        for bad in ("", "   "):
            with pytest.raises(ValueError, match="tenant"):
                InvocationContext(tenant=bad)

    def test_context_repr_never_shows_scopes(self) -> None:
        text = repr(make_ctx(granted_scopes=frozenset({"platform:admin"})))
        assert "platform:admin" not in text

    def test_tool_needs_a_scope(self) -> None:
        with pytest.raises(ValueError, match="at least one required scope"):
            ToolSpec(name="x.y", required_scopes=(), flag="waddles.x.y")

    def test_side_effecting_tool_must_ship_behind_a_flag(self) -> None:
        with pytest.raises(ValueError, match="must declare a feature flag"):
            ToolSpec(name="x.y", required_scopes=("a:b",))
        ToolSpec(name="x.y", required_scopes=("a:b",), flag="waddles.x.y")
        ToolSpec(name="x.y", required_scopes=("a:b",), side_effects=False)  # read-only: ok

    @pytest.mark.parametrize("scope", ["*", "*:read", "admin", "a:*", "A:b", "a b:c"])
    def test_tool_scopes_must_be_concrete_resource_action(self, scope: str) -> None:
        with pytest.raises(ValueError, match="resource:action"):
            ToolSpec(name="x.y", required_scopes=(scope,), flag="waddles.x.y")

    @pytest.mark.parametrize("name", ["", "Bad", "1x", "a b", "x" * 80])
    def test_tool_names_are_validated(self, name: str) -> None:
        with pytest.raises(ValueError, match="tool name"):
            ToolSpec(name=name, required_scopes=("a:b",), flag="waddles.x.y")

    def test_duplicate_parameters_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate parameters"):
            ToolSpec(
                name="x.y",
                required_scopes=("a:b",),
                parameters=(ToolParam("q"), ToolParam("q")),
                flag="waddles.x.y",
            )

    @pytest.mark.parametrize("name", ["tenant_id", "user_id", "scopes", "roles", "community_id"])
    def test_identity_parameters_cannot_be_declared(self, name: str) -> None:
        with pytest.raises(ValueError, match="bound by the server"):
            ToolParam(name)

    def test_param_name_and_type_are_validated(self) -> None:
        with pytest.raises(ValueError, match="snake_case"):
            ToolParam("Bad-Name")
        with pytest.raises(ValueError, match="unsupported"):
            ToolParam("ok", "object")  # type: ignore[arg-type]
        with pytest.raises(re.error):
            ToolParam("ok", pattern="(")

    def test_registry_rejects_duplicates_and_is_immutable(self) -> None:
        spec = ToolSpec(name="x.y", required_scopes=("a:b",), flag="waddles.x.y")
        with pytest.raises(ValueError, match="duplicate tool"):
            ToolRegistry([spec, spec])
        registry = ToolRegistry([spec])
        assert registry.names() == ("x.y",) and len(registry) == 1
        assert not hasattr(registry, "register")

    def test_from_contract_cannot_widen_the_feature_scopes(self) -> None:
        from flask_core.feature_contract import parse_feature_contract
        from flask_core.mcp_server import tool_name_for_contract

        contract = parse_feature_contract(
            {
                "id": "bot.shoutout",
                "version": 1,
                "module": "bot",
                "requires_scopes": ["bot.shoutout:write"],
                "min_tier": "free",
                "flag": "waddles.bot.shoutout",
            }
        )
        spec = ToolSpec.from_contract(contract, parameters=(ToolParam("target"),))
        assert spec.name == tool_name_for_contract(contract) == "bot.shoutout@1"
        assert spec.required_scopes == ("bot.shoutout:write",)
        assert spec.flag == "waddles.bot.shoutout" and spec.side_effects

    def test_is_reserved_argument_ignores_case_and_punctuation(self) -> None:
        assert is_reserved_argument("Tenant-ID") and is_reserved_argument("tenantId")
        assert not is_reserved_argument("message") and not is_reserved_argument("query")

    def test_denial_reasons_are_a_closed_vocabulary(self) -> None:
        assert authz.REASON_SCOPE_DENIED in DENIAL_REASONS
        assert len(DENIAL_REASONS) == 13


class _Sink:
    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        authz.telemetry.use_providers(None, MeterProvider(metric_readers=[self.reader]))

    def points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        found: list[Any] = []
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        found.extend(metric.data.data_points)
        return found


class TestTelemetryAndLogs:
    async def test_decisions_are_metered_and_model_chosen_names_never_become_labels(self) -> None:
        sink = _Sink()
        try:
            await authorize_tool_calls(
                make_ctx(), [call("community.lookup", query="a")], make_registry()
            )
            with pytest.raises(ToolCallDenied):
                await authorize_tool_calls(
                    make_ctx(), [call("totally_made_up_tool_name")], make_registry()
                )
            with pytest.raises(ToolCallDenied):
                await authorize_tool_calls(make_ctx(), [call("platform.purge")], make_registry())
            decisions = sink.points("waddles.ai.tool_call.decisions")
            durations = sink.points("waddles.ai.guard.duration")
        finally:
            authz.telemetry.use_providers()
        by_label = {
            (p.attributes["decision"], p.attributes["reason"], p.attributes["tool"]): p.value
            for p in decisions
        }
        print(f"telemetry check: decisions={len(decisions)} durations={len(durations)}")
        assert by_label == {
            ("allowed", "authorized", "community.lookup"): 1,
            ("denied", "unknown_tool", "unknown"): 1,
            ("denied", "scope_denied", "platform.purge"): 1,
        }
        assert len(durations) >= 1
        assert "totally_made_up_tool_name" not in json.dumps(
            [dict(p.attributes) for p in decisions]
        )

    async def test_denial_log_is_pii_free(self, caplog: pytest.LogCaptureFixture) -> None:
        req = call("community.announce", message="secret-text-bob@example.com", tenant=OTHER_TENANT)
        with caplog.at_level(logging.DEBUG, logger="flask_core.ai_tool_authz"):
            with pytest.raises(ToolCallDenied):
                await authorize_tool_calls(make_ctx(), [req], make_registry())
        assert "ai_tool_call_denied reason=reserved_argument" in caplog.text
        for leaked in ("secret-text", "bob@example.com", OTHER_TENANT, "community.announce"):
            assert leaked not in caplog.text

    async def test_flag_failure_is_logged_without_the_exception_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="flask_core.ai_tool_authz"):
            await denied(make_ctx(), call("community.announce", message="x"), flag_check=flags_boom)
        assert (
            "ai_tool_flag_check_failed" in caplog.text and "posthog unreachable" not in caplog.text
        )
