"""Server-side re-authorisation of model-requested tool / function calls (OWASP LLM01, LLM06).

The model is an untrusted component: whatever it asks to call, with whatever arguments, is
*input* -- never a decision. This module is the single chokepoint every tool call must pass
between "the provider response says the model wants X" and "something executes X". It re-derives
the answer from facts the model cannot influence:

* the **invoking user's** tenant, community and OIDC scopes, captured once from the verified
  request into an immutable :class:`InvocationContext` -- never from the prompt, the model output
  or the tool arguments;
* a **server-side** :class:`ToolRegistry` of declared tools (each with required scopes, a strict
  flat argument schema, an optional feature flag and a side-effect marker). A tool that is not in
  the registry does not exist, however confidently the model names it.

Properties enforced (and pinned by tests):

* **Fail closed.** Unknown tool, missing scope, malformed or unexpected arguments, an unavailable
  feature-flag backend, a tainted context asking for a side-effecting tool, too many calls: all
  raise :class:`ToolCallDenied`. There is no "log and continue" branch.
* **No identity from the model.** An argument that names a tenant / community / user / scope /
  role (:func:`is_reserved_argument`) is refused outright -- even when it matches the caller's own
  value -- because the only legitimate source for those is :class:`InvocationContext`. Executors
  receive them bound on :class:`AuthorizedToolCall`.
* **All-or-nothing.** One denied call in a batch denies the whole batch, so an injected sequence
  cannot be partially executed.
* **Least privilege under taint.** When untrusted content shaped the request
  (``InvocationContext.tainted``), side-effecting tools are refused; read-only tools remain
  subject to the full scope check.
* **PII-free observability.** Denials log and meter the reason code and whether the tool was
  known -- never argument values, never a model-chosen tool name (unknown names are labelled
  ``unknown``).

There is deliberately **no execution runtime here**: the platform has no App execution runtime
yet (see ``flask_core.mcp_server``'s module docstring). The contract for any future executor is
to accept only :class:`AuthorizedToolCall` values, and to scope every query by the tenant /
community / user bound on them.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from flask_core.ai_telemetry import AITelemetry
from flask_core.authz import has_required_scopes

logger = logging.getLogger(__name__)

#: Decision counters / authorisation latency (PII-free; no-op unless an OTel provider is set).
telemetry = AITelemetry("waddles.flask_core.ai_tool_authz")

#: Hard cap on tool calls honoured from one model response.
DEFAULT_MAX_TOOL_CALLS = 4
#: Hard cap on the serialised size of one call's arguments.
MAX_ARGUMENTS_CHARS = 8192
DEFAULT_MAX_STRING_CHARS = 2000

REASON_NO_TOOLS_EXPOSED = "no_tools_exposed"
REASON_UNKNOWN_TOOL = "unknown_tool"
REASON_SCOPE_DENIED = "scope_denied"
REASON_NO_COMMUNITY = "no_community_context"
REASON_RESERVED_ARGUMENT = "reserved_argument"
REASON_UNKNOWN_ARGUMENT = "unknown_argument"
REASON_MISSING_ARGUMENT = "missing_argument"
REASON_INVALID_ARGUMENT = "invalid_argument"
REASON_MALFORMED_CALL = "malformed_call"
REASON_TAINTED_CONTEXT = "tainted_context"
REASON_FEATURE_DISABLED = "feature_disabled"
REASON_FLAG_CHECK_FAILED = "flag_check_failed"
REASON_TOO_MANY_CALLS = "too_many_calls"

#: Every reason code a denial can carry (closed vocabulary -> safe metric label / audit detail).
DENIAL_REASONS: frozenset[str] = frozenset(
    {
        REASON_NO_TOOLS_EXPOSED,
        REASON_UNKNOWN_TOOL,
        REASON_SCOPE_DENIED,
        REASON_NO_COMMUNITY,
        REASON_RESERVED_ARGUMENT,
        REASON_UNKNOWN_ARGUMENT,
        REASON_MISSING_ARGUMENT,
        REASON_INVALID_ARGUMENT,
        REASON_MALFORMED_CALL,
        REASON_TAINTED_CONTEXT,
        REASON_FEATURE_DISABLED,
        REASON_FLAG_CHECK_FAILED,
        REASON_TOO_MANY_CALLS,
    }
)

#: ``feature_enabled(flag, tenant=..., community=..., default=False)`` -- injectable for tests.
FlagCheck = Callable[..., Awaitable[bool]]

ProviderName = Literal["openai", "anthropic", "ollama"]


class ToolCallDenied(Exception):  # noqa: N818 - reads as the decision it is, like PermissionError
    """A model-requested tool call failed server-side authorisation (fail-closed).

    ``reason`` is a stable code from :data:`DENIAL_REASONS`; ``tool_known`` says whether the
    requested name resolved in the server-side registry. The message never contains the model's
    tool name or any argument value.
    """

    def __init__(self, reason: str, *, tool_known: bool = False, tool: str = "unknown") -> None:
        """Record the machine-checkable ``reason`` and, for known tools, the registered ``tool``."""
        self.reason = reason
        self.tool_known = tool_known
        self.tool = tool if tool_known else "unknown"
        super().__init__(f"tool call denied: {reason}")


# --------------------------------------------------------------------------------------
# Declarations (server-side only)
# --------------------------------------------------------------------------------------

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_.@-]{0,63}$")
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# resource:action -- the action half is never a wildcard (mirrors flask_core.authz).
_SCOPE_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_.-]+$")

_RESERVED_PREFIXES = ("tenant", "community", "impersonat", "onbehalf", "actor")
_RESERVED_EXACT = frozenset(
    "user userid uid sub subject scope scopes role roles team teams teamid org orgid "
    "organization organizationid asuser runas principal owner ownerid permission permissions "
    "claims jwt token".split()
)


def is_reserved_argument(name: str) -> bool:
    """True if ``name`` looks like an identity / authorisation argument (tenant, user, scope...).

    Matching ignores case and punctuation, so ``Tenant-ID``, ``tenantId`` and ``tenant_id`` are
    the same key. Reserved arguments are *bound by the server* from the invocation context; the
    model never supplies them.
    """
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return key in _RESERVED_EXACT or key.startswith(_RESERVED_PREFIXES)


ParamType = Literal["string", "integer", "number", "boolean"]


@dataclass(slots=True, frozen=True)
class ToolParam:
    """One declared, flat, typed tool argument."""

    name: str
    type: ParamType = "string"
    required: bool = True
    max_length: int = DEFAULT_MAX_STRING_CHARS
    choices: tuple[str, ...] = ()
    pattern: str | None = None

    def __post_init__(self) -> None:
        """Reject malformed declarations at registration time -- a bad tool never loads."""
        if not _PARAM_NAME_RE.match(self.name):
            raise ValueError(f"tool parameter name {self.name!r} must be lower snake_case")
        if is_reserved_argument(self.name):
            raise ValueError(
                f"tool parameter {self.name!r} names an identity/authorisation field; "
                "those are bound by the server and cannot be declared"
            )
        if self.type not in ("string", "integer", "number", "boolean"):
            raise ValueError(f"unsupported tool parameter type {self.type!r}")
        if self.pattern is not None:
            re.compile(self.pattern)


@dataclass(slots=True, frozen=True)
class ToolSpec:
    """A tool the server is willing to execute on a user's behalf.

    ``required_scopes`` must be non-empty: a tool with no scope requirement is unauthorisable by
    construction (``resource:action`` scopes, no wildcard action). ``side_effects`` defaults to
    True (conservative) -- only an explicitly read-only tool survives a tainted context -- and a
    side-effecting tool MUST name a feature ``flag``: every executable action ships behind a
    PostHog flag (resolved through the platform's flag-AND-licence-tier evaluator, default OFF).
    """

    name: str
    required_scopes: tuple[str, ...]
    parameters: tuple[ToolParam, ...] = ()
    flag: str | None = None
    side_effects: bool = True
    community_scoped: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        """Validate the declaration so a bad tool fails at load, never at call time."""
        if not _TOOL_NAME_RE.match(self.name):
            raise ValueError(f"tool name {self.name!r} must match {_TOOL_NAME_RE.pattern}")
        if not self.required_scopes:
            raise ValueError(f"tool {self.name!r} must declare at least one required scope")
        for scope in self.required_scopes:
            if not _SCOPE_RE.match(scope):
                raise ValueError(f"tool {self.name!r} scope {scope!r} must be resource:action")
        if self.side_effects and self.flag is None:
            raise ValueError(
                f"side-effecting tool {self.name!r} must declare a feature flag "
                "(every executable action ships behind a flag, default OFF)"
            )
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError(f"tool {self.name!r} declares duplicate parameters")

    @classmethod
    def from_contract(
        cls,
        contract: Any,
        *,
        parameters: tuple[ToolParam, ...] = (),
        side_effects: bool = True,
        community_scoped: bool = True,
    ) -> ToolSpec:
        """Derive a spec from a :class:`~flask_core.feature_contract.FeatureContract`.

        Scopes, flag and name come from the contract, so an AI tool can never widen what its own
        Feature already declares -- the same "one authorisation path" rule the MCP surface uses.
        The name is ``<id>@<version>``, identical to ``mcp_server.tool_name_for_contract``.
        """
        return cls(
            name=f"{contract.id}@{contract.version}",
            required_scopes=tuple(sorted(contract.requires_scopes)),
            parameters=parameters,
            flag=contract.flag,
            side_effects=side_effects,
            community_scoped=community_scoped,
            description=f"Feature {contract.id}",
        )


class ToolRegistry:
    """Immutable, server-side set of executable tools. The model cannot add to it."""

    __slots__ = ("_tools",)

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        """Index ``specs`` by name; a duplicate name is a programming error and raises."""
        tools: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in tools:
                raise ValueError(f"duplicate tool {spec.name!r} in registry")
            tools[spec.name] = spec
        self._tools: Mapping[str, ToolSpec] = MappingProxyType(tools)

    def get(self, name: object) -> ToolSpec | None:
        """The spec registered under ``name``, or ``None`` (non-strings never match)."""
        return self._tools.get(name) if isinstance(name, str) else None

    def names(self) -> tuple[str, ...]:
        """Registered tool names, sorted."""
        return tuple(sorted(self._tools))

    def __len__(self) -> int:
        """Number of registered tools."""
        return len(self._tools)


#: The default registry: no tools exposed, so every model-requested call is denied.
EMPTY_REGISTRY = ToolRegistry()


# --------------------------------------------------------------------------------------
# Invocation context + call shapes
# --------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class InvocationContext:
    """The verified identity a tool call must stay inside -- built from the request, not the model.

    ``granted_scopes`` is excluded from ``repr`` so it can never be logged by accident.
    ``tainted`` is True when untrusted retrieved content (or an observed injection attempt)
    shaped the request, which withdraws side-effecting tools.
    """

    tenant: str
    community_id: int | None = None
    user_id: int | str | None = None
    granted_scopes: frozenset[str] = field(default_factory=frozenset, repr=False)
    tainted: bool = False

    def __post_init__(self) -> None:
        """A context without a tenant is unusable -- refuse to construct it."""
        if not isinstance(self.tenant, str) or not self.tenant.strip():
            raise ValueError("InvocationContext requires a non-empty tenant")

    def with_taint(self) -> InvocationContext:
        """A copy of this context marked tainted (taint only ever ratchets up)."""
        return InvocationContext(
            tenant=self.tenant,
            community_id=self.community_id,
            user_id=self.user_id,
            granted_scopes=self.granted_scopes,
            tainted=True,
        )


@dataclass(slots=True, frozen=True)
class ToolCallRequest:
    """One tool call exactly as the model asked for it -- UNAUTHORISED, never executable.

    ``parse_error`` is set when the provider payload for this call could not be decoded; such a
    call is always denied (``malformed_call``) rather than skipped.
    """

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict, hash=False)
    call_id: str | None = None
    parse_error: str | None = None


@dataclass(slots=True, frozen=True)
class AuthorizedToolCall:
    """A tool call that passed every gate. The only shape an executor should accept.

    ``tenant`` / ``community_id`` / ``user_id`` are bound from the :class:`InvocationContext`;
    ``arguments`` holds only the validated, model-supplied business arguments (read-only).
    """

    name: str
    arguments: Mapping[str, Any] = field(hash=False)
    tenant: str
    community_id: int | None
    user_id: int | str | None
    call_id: str | None = None


# --------------------------------------------------------------------------------------
# Provider payload parsing
# --------------------------------------------------------------------------------------


def _malformed(call_id: str | None = None) -> ToolCallRequest:
    """A placeholder request that the authoriser will deny as ``malformed_call``."""
    return ToolCallRequest(name="", call_id=call_id, parse_error=REASON_MALFORMED_CALL)


def _decode_arguments(raw: Any) -> tuple[Mapping[str, Any], str | None]:
    """Decode a provider ``arguments`` value (JSON string or mapping) -> ``(args, error)``."""
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, str):
        if len(raw) > MAX_ARGUMENTS_CHARS:
            return {}, REASON_MALFORMED_CALL
        try:
            raw = json.loads(raw)
        except ValueError:
            logger.debug("ai_tool_arguments_undecodable")
            return {}, REASON_MALFORMED_CALL
    if not isinstance(raw, Mapping):
        return {}, REASON_MALFORMED_CALL
    if len(json.dumps(raw, default=str)) > MAX_ARGUMENTS_CHARS:
        return {}, REASON_MALFORMED_CALL
    return dict(raw), None


def _request_from(name: Any, raw_args: Any, call_id: Any) -> ToolCallRequest:
    """Build a :class:`ToolCallRequest` from decoded provider fields, flagging bad shapes."""
    cid = call_id if isinstance(call_id, str) and len(call_id) <= 128 else None
    if not isinstance(name, str) or not name or len(name) > 256:
        return _malformed(cid)
    args, error = _decode_arguments(raw_args)
    return ToolCallRequest(name=name, arguments=args, call_id=cid, parse_error=error)


def _openai_calls(payload: Mapping[str, Any]) -> list[ToolCallRequest]:
    """Tool calls in an OpenAI-compatible chat completion (``tool_calls`` and legacy function)."""
    choices = payload.get("choices")
    if choices is None:
        return []
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return [_malformed()]
    first = choices[0]
    message = first.get("message")
    message = message if isinstance(message, Mapping) else {}
    calls: list[ToolCallRequest] = []
    raw_calls = message.get("tool_calls")
    if raw_calls is not None:
        if not isinstance(raw_calls, list):
            return [_malformed()]
        for raw in raw_calls:
            fn = raw.get("function") if isinstance(raw, Mapping) else None
            if not isinstance(fn, Mapping):
                calls.append(_malformed())
                continue
            calls.append(_request_from(fn.get("name"), fn.get("arguments"), raw.get("id")))
    legacy = message.get("function_call")
    if legacy is not None:
        if isinstance(legacy, Mapping):
            calls.append(_request_from(legacy.get("name"), legacy.get("arguments"), None))
        else:
            calls.append(_malformed())
    if not calls and first.get("finish_reason") in ("tool_calls", "function_call"):
        calls.append(_malformed())
    return calls


def _anthropic_calls(payload: Mapping[str, Any]) -> list[ToolCallRequest]:
    """``tool_use`` content blocks in an Anthropic Messages response."""
    blocks = payload.get("content")
    calls: list[ToolCallRequest] = []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                calls.append(_request_from(block.get("name"), block.get("input"), block.get("id")))
    if not calls and payload.get("stop_reason") == "tool_use":
        calls.append(_malformed())
    return calls


def _ollama_calls(payload: Mapping[str, Any]) -> list[ToolCallRequest]:
    """``message.tool_calls`` in an Ollama ``/api/chat`` response (``/api/generate`` has none)."""
    message = payload.get("message")
    if not isinstance(message, Mapping) or message.get("tool_calls") is None:
        return []
    raw_calls = message["tool_calls"]
    if not isinstance(raw_calls, list):
        return [_malformed()]
    calls: list[ToolCallRequest] = []
    for raw in raw_calls:
        fn = raw.get("function") if isinstance(raw, Mapping) else None
        if not isinstance(fn, Mapping):
            calls.append(_malformed())
            continue
        calls.append(_request_from(fn.get("name"), fn.get("arguments"), raw.get("id")))
    return calls


def extract_tool_calls(payload: Any, *, provider: ProviderName) -> tuple[ToolCallRequest, ...]:
    """Normalise the tool calls a provider response asks for into unauthorised requests.

    A response that *claims* to be a tool call (``finish_reason == "tool_calls"``,
    ``stop_reason == "tool_use"``) but carries nothing parseable yields one malformed request,
    which the authoriser denies -- a half-parsed tool turn is never read as "no tool calls".

    Args:
        payload: The decoded JSON body of the provider response.
        provider: Which wire format to read.

    Returns:
        The requested calls, in order (empty when the model asked for none).
    """
    if not isinstance(payload, Mapping):
        return ()
    if provider == "openai":
        return tuple(_openai_calls(payload))
    if provider == "anthropic":
        return tuple(_anthropic_calls(payload))
    return tuple(_ollama_calls(payload))


# --------------------------------------------------------------------------------------
# Authorisation
# --------------------------------------------------------------------------------------


def _check_value(param: ToolParam, value: Any) -> None:
    """Validate one argument against its declaration; raise ``invalid_argument`` on failure."""
    bad = ToolCallDenied(REASON_INVALID_ARGUMENT, tool_known=True)
    if param.type == "string":
        if not isinstance(value, str) or len(value) > param.max_length:
            raise bad
        if param.choices and value not in param.choices:
            raise bad
        if param.pattern is not None and re.fullmatch(param.pattern, value) is None:
            raise bad
    elif param.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad
    elif param.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise bad
    elif not isinstance(value, bool):
        raise bad


def _validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly validate ``arguments`` against ``spec`` and return the accepted copy."""
    declared = {p.name: p for p in spec.parameters}
    clean: dict[str, Any] = {}
    for key, value in arguments.items():
        param = declared.get(key) if isinstance(key, str) else None
        if param is None:
            raise ToolCallDenied(REASON_UNKNOWN_ARGUMENT, tool_known=True, tool=spec.name)
        try:
            _check_value(param, value)
        except ToolCallDenied as exc:
            raise ToolCallDenied(exc.reason, tool_known=True, tool=spec.name) from None
        clean[key] = value
    for param in spec.parameters:
        if param.required and param.name not in clean:
            raise ToolCallDenied(REASON_MISSING_ARGUMENT, tool_known=True, tool=spec.name)
    return clean


async def _flag_enabled(spec: ToolSpec, ctx: InvocationContext, check: FlagCheck | None) -> None:
    """Fail closed unless the tool's feature flag is on for the invoking tenant/community."""
    if spec.flag is None:
        return
    check_fn = check
    if check_fn is None:
        # Local import: keeps posthog / licensing out of callers that inject their own check.
        from flask_core.feature_flags import feature_enabled

        check_fn = feature_enabled
    try:
        enabled = await check_fn(
            spec.flag, tenant=ctx.tenant, community=ctx.community_id, default=False
        )
    except Exception:  # noqa: BLE001 - ANY backend failure must deny, never allow
        logger.error("ai_tool_flag_check_failed tool=%s", spec.name)
        raise ToolCallDenied(REASON_FLAG_CHECK_FAILED, tool_known=True, tool=spec.name) from None
    if not enabled:
        raise ToolCallDenied(REASON_FEATURE_DISABLED, tool_known=True, tool=spec.name)


async def authorize_tool_call(
    ctx: InvocationContext,
    call: ToolCallRequest,
    registry: ToolRegistry,
    *,
    flag_check: FlagCheck | None = None,
) -> AuthorizedToolCall:
    """Re-authorise one model-requested call against the invoking user's tenant and scopes.

    Gate order (first failure wins; nothing past a failed gate runs): malformed -> reserved
    (identity) arguments -> registry membership -> scopes -> community context -> argument
    schema -> taint policy -> feature flag.

    Args:
        ctx: Identity captured from the verified request.
        call: The model's request (untrusted).
        registry: The server-side tools this request may execute.
        flag_check: Feature-flag callable; defaults to ``flask_core.feature_flags``.

    Returns:
        The call with tenant / community / user bound from ``ctx``.

    Raises:
        ToolCallDenied: Any gate failed.
    """
    if call.parse_error is not None:
        raise ToolCallDenied(REASON_MALFORMED_CALL)
    if any(isinstance(k, str) and is_reserved_argument(k) for k in call.arguments):
        known = registry.get(call.name) is not None
        raise ToolCallDenied(REASON_RESERVED_ARGUMENT, tool_known=known, tool=call.name)
    spec = registry.get(call.name)
    if spec is None:
        raise ToolCallDenied(REASON_NO_TOOLS_EXPOSED if len(registry) == 0 else REASON_UNKNOWN_TOOL)
    if not has_required_scopes(ctx.granted_scopes, spec.required_scopes):
        raise ToolCallDenied(REASON_SCOPE_DENIED, tool_known=True, tool=spec.name)
    if spec.community_scoped and ctx.community_id is None:
        raise ToolCallDenied(REASON_NO_COMMUNITY, tool_known=True, tool=spec.name)
    arguments = _validate_arguments(spec, call.arguments)
    if ctx.tainted and spec.side_effects:
        raise ToolCallDenied(REASON_TAINTED_CONTEXT, tool_known=True, tool=spec.name)
    await _flag_enabled(spec, ctx, flag_check)
    return AuthorizedToolCall(
        name=spec.name,
        arguments=MappingProxyType(arguments),
        tenant=ctx.tenant,
        community_id=ctx.community_id if spec.community_scoped else None,
        user_id=ctx.user_id,
        call_id=call.call_id,
    )


async def authorize_tool_calls(
    ctx: InvocationContext,
    calls: Sequence[ToolCallRequest],
    registry: ToolRegistry,
    *,
    flag_check: FlagCheck | None = None,
    max_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> tuple[AuthorizedToolCall, ...]:
    """Authorise a batch of model-requested calls, all-or-nothing, with PII-free telemetry.

    Args:
        ctx: Identity captured from the verified request.
        calls: Everything the model asked for in one response.
        registry: The server-side tools this request may execute.
        flag_check: Feature-flag callable (see :func:`authorize_tool_call`).
        max_calls: Maximum calls honoured from one response.

    Returns:
        The authorised calls (empty when ``calls`` is empty).

    Raises:
        ToolCallDenied: The batch is too large or any call failed a gate; nothing is returned.
    """
    if not calls:
        return ()
    started = time.perf_counter()
    authorised: list[AuthorizedToolCall] = []
    try:
        if len(calls) > max_calls:
            raise ToolCallDenied(REASON_TOO_MANY_CALLS)
        for call in calls:
            authorised.append(await authorize_tool_call(ctx, call, registry, flag_check=flag_check))
    except ToolCallDenied as exc:
        logger.warning(
            "ai_tool_call_denied reason=%s tool_known=%s calls=%d tainted=%s",
            exc.reason,
            exc.tool_known,
            len(calls),
            ctx.tainted,
        )
        telemetry.record_tool_decision(
            allowed=False,
            reason=exc.reason,
            tool=exc.tool,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000.0
    for item in authorised:
        telemetry.record_tool_decision(
            allowed=True, reason="authorized", tool=item.name, duration_ms=duration_ms
        )
    logger.debug("ai_tool_calls_authorized calls=%d tainted=%s", len(authorised), ctx.tainted)
    return tuple(authorised)


def reject_unsolicited_tool_calls(payload: Any, *, provider: ProviderName, surface: str) -> None:
    """Deny any tool call in a response from a surface that exposes NO tools.

    For services (chat reply, research synthesis) that never offer tools to the model: a
    ``tool_calls`` block in the answer is an anomaly -- a hijacked or misbehaving model -- and is
    refused rather than ignored, logged and metered like any other denial.

    Args:
        payload: The decoded provider response body.
        provider: Which wire format to read.
        surface: Short closed-vocabulary label for the calling surface (log / diagnostics only).

    Raises:
        ToolCallDenied: ``payload`` requests at least one tool call.
    """
    calls = extract_tool_calls(payload, provider=provider)
    if not calls:
        return
    logger.warning(
        "ai_tool_call_denied reason=%s surface=%s calls=%d",
        REASON_NO_TOOLS_EXPOSED,
        surface,
        len(calls),
    )
    telemetry.record_tool_decision(allowed=False, reason=REASON_NO_TOOLS_EXPOSED, tool="unknown")
    raise ToolCallDenied(REASON_NO_TOOLS_EXPOSED)


__all__ = [
    "DEFAULT_MAX_TOOL_CALLS",
    "DENIAL_REASONS",
    "EMPTY_REGISTRY",
    "AuthorizedToolCall",
    "FlagCheck",
    "InvocationContext",
    "ProviderName",
    "ToolCallDenied",
    "ToolCallRequest",
    "ToolParam",
    "ToolRegistry",
    "ToolSpec",
    "authorize_tool_call",
    "authorize_tool_calls",
    "extract_tool_calls",
    "is_reserved_argument",
    "reject_unsolicited_tool_calls",
    "telemetry",
]
