"""Host-native tests for the `shoutout` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- both exports are plain async Python, testable
directly against a fake `wit_world` module standing in for the WIT `db`/
`kv`/`flags`/`relay`/`http` host imports (same pattern
`bundles/python/pyping/tests/test_app.py` uses for `relay`). Coverage
mirrors `core/svc_process/tests/test_bundles_social_shoutout_process.py`'s
proven behavioral matrix (gh #316) -- prefix parsing, usage hints, login
validation, self-shoutout, permission tiers, feature flag -- plus this
bundle's own cooldown (`kv`) and Twitch-enrichment (`http`) behavior.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from app import (
    _AI_FEATURE_FLAG,
    _AI_REQUIRED_TIER,
    _AISO_USAGE,
    _FEATURE_FLAG,
    _INVALID_LOGIN_REPLY,
    _PERMISSION_DENIED_REPLY,
    _SELF_SHOUTOUT_REPLY,
    _SO_USAGE,
    dispatch,
    transform,
)
from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core.bundle_runtime import (
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

TENANT = "tenant-1"
COMMUNITY = "42"
COMMUNITY_ID = 42

MOD_ACTOR = "mod_user"
NON_MOD_ACTOR = "rando"
ADMIN_ACTOR = "owner_user"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _event(text: str, *, actor: str | None = MOD_ACTOR, **payload_overrides: object) -> PlatformEvent:
    payload: dict[str, object] = {
        "text": text,
        "channel_id": "12345",
        "author_id": "platform-user-1",
        **payload_overrides,
    }
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-09-28T00:00:00.000Z",
    )


# --------------------------------------------------------------------------
# Fake `wit_world` -- db (shoutout_config / community_members), kv, flags, relay.
# --------------------------------------------------------------------------


@dataclass
class _WitValue:
    value: Any = None


def _make_value_classes() -> types.SimpleNamespace:
    """Build the `Value_*` case classes `waddle_sdk.db` expects on `wit_world.imports.db`."""

    def _cls(name: str) -> type:
        return type(name, (_WitValue,), {})

    ns = types.SimpleNamespace()
    ns.Value_NullValue = _cls("Value_NullValue")
    ns.Value_BoolValue = _cls("Value_BoolValue")
    ns.Value_IntValue = _cls("Value_IntValue")
    ns.Value_FloatValue = _cls("Value_FloatValue")
    ns.Value_TextValue = _cls("Value_TextValue")
    ns.Value_BytesValue = _cls("Value_BytesValue")
    return ns


class _FakeRows:
    def __init__(self, columns: list[str], rows: list[list[Any]], rows_affected: int = 0) -> None:
        self.columns = columns
        self.rows = rows
        self.rows_affected = rows_affected


class _FakeDbHarness:
    """Answers `db.execute()` for exactly the two SQL shapes `app.py` issues."""

    def __init__(self) -> None:
        self.shoutout_config: dict[int, dict[str, Any]] = {}
        self.roles_by_platform: dict[tuple[int, str, str], str] = {}
        self.roles_by_display_name: dict[tuple[int, str], str] = {}
        self.value_mod = _make_value_classes()

    def _wrap(self, value: Any) -> _WitValue:
        """Wrap a plain Python scalar the same way a real WIT `db.execute()` result would."""
        if value is None:
            return self.value_mod.Value_NullValue()
        if isinstance(value, bool):
            return self.value_mod.Value_BoolValue(value)
        if isinstance(value, int):
            return self.value_mod.Value_IntValue(value)
        return self.value_mod.Value_TextValue(str(value))

    def execute(self, statement: str, params: list[Any]) -> _FakeRows:
        unwrapped = [p.value if isinstance(p, _WitValue) else p for p in params]
        if "FROM shoutout_config" in statement:
            (community_id,) = unwrapped
            row = self.shoutout_config.get(community_id)
            if row is None:
                return _FakeRows(["so_permission", "cooldown_minutes"], [])
            return _FakeRows(
                ["so_permission", "cooldown_minutes"],
                [[self._wrap(row["so_permission"]), self._wrap(row["cooldown_minutes"])]],
            )
        if "platform_user_id" in statement:
            community_id, platform, platform_user_id = unwrapped
            role = self.roles_by_platform.get((community_id, platform, platform_user_id))
            return _FakeRows(["role"], [[self._wrap(role)]] if role else [])
        if "display_name" in statement:
            community_id, display_name = unwrapped
            role = self.roles_by_display_name.get((community_id, display_name))
            return _FakeRows(["role"], [[self._wrap(role)]] if role else [])
        raise AssertionError(f"unexpected SQL in test fake: {statement!r}")


class _FakeKv:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, bytes, int]] = []

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self.store[key] = value
        self.set_calls.append((key, value, ttl_seconds))


class _FakeRelay:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def push(self, provider: str, message_json: str) -> None:
        self.calls.append((provider, message_json))


class _FakeLevel:
    """Stands in for the generated `wit_world.imports.log.Level` enum -- subscriptable by
    member name only, matching `waddle_sdk.log._write`'s `log_mod.Level[level_name]` lookup."""

    _MEMBERS: ClassVar[dict[str, int]] = {"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3}

    def __getitem__(self, name: str) -> int:
        return self._MEMBERS[name]


class _FakeLog:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.Level = _FakeLevel()  # matches generated binding's own PascalCase attribute name

    def write(self, level: int, message: str, fields_json: str) -> None:
        self.calls.append((level, message, fields_json))


@dataclass
class _Harness:
    db: _FakeDbHarness
    kv: _FakeKv
    relay: _FakeRelay
    log: _FakeLog
    flags_enabled: bool
    ai_flag_enabled: bool = True
    tier: str = _AI_REQUIRED_TIER


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Install a fake `wit_world` covering every host import `app.py` touches."""
    db = _FakeDbHarness()
    kv = _FakeKv()
    relay = _FakeRelay()
    log_fake = _FakeLog()
    state = _Harness(db=db, kv=kv, relay=relay, log=log_fake, flags_enabled=True)

    # Lambdas indirect through the harness instance at CALL time (not bound-method references
    # captured now), so a test's `harness.db.execute = _boom`-style instance override -- set
    # after this fixture already ran -- is still honored.
    db_mod = types.SimpleNamespace(
        execute=lambda statement, params: db.execute(statement, params), **vars(db.value_mod)
    )
    kv_mod = types.SimpleNamespace(
        get=lambda key: kv.get(key), set=lambda key, value, ttl: kv.set(key, value, ttl)
    )
    relay_mod = types.SimpleNamespace(push=lambda provider, message: relay.push(provider, message))
    flags_mod = types.SimpleNamespace(
        enabled=lambda key, default: (
            state.ai_flag_enabled if key == _AI_FEATURE_FLAG else state.flags_enabled
        ),
        tier=lambda: state.tier,
    )
    clock_mod = types.SimpleNamespace(monotonic_nanos=lambda: 0)

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        db=db_mod, kv=kv_mod, relay=relay_mod, flags=flags_mod, log=log_fake, clock=clock_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    set_bundle_dal(AsyncDB())
    yield state
    reset_bundle_dal_for_tests()


@pytest.fixture(autouse=True)
def _ctx() -> Any:
    with bundle_context(tenant=TENANT, community=COMMUNITY, app_id="waddles.core.shoutout.default"):
        yield


class TestTransformCommandParsing:
    @pytest.mark.parametrize("cmd", ["!so", "!shoutout"])
    def test_valid_command_forwards_to_dispatch(self, harness: _Harness, cmd: str) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event(f"{cmd} clubpenguinfan")))
        assert result is not None
        assert result.payload["target"] == "clubpenguinfan"
        assert result.payload["channel_id"] == "12345"

    def test_strips_leading_at_and_lowercases(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event("!so @ClubPenguinFan")))
        assert result is not None
        assert result.payload["target"] == "clubpenguinfan"

    def test_command_prefix_is_case_insensitive(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event("!SO clubpenguinfan")))
        assert result is not None

    @pytest.mark.parametrize("text", ["!sox something", "!vso someone", "hello", "", "   "])
    def test_non_matching_text_returns_none(self, harness: _Harness, text: str) -> None:
        assert _run(transform(_event(text))) is None

    def test_missing_text_field_raises(self, harness: _Harness) -> None:
        event = PlatformEvent(
            platform="twitch", event_type="chat.message", actor=MOD_ACTOR, payload={},
            occurred_at="2026-09-28T00:00:00.000Z",
        )
        with pytest.raises(ValueError, match="text"):
            _run(transform(event))

    def test_non_string_text_raises(self, harness: _Harness) -> None:
        event = PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor=MOD_ACTOR,
            payload={"text": 123},
            occurred_at="2026-09-28T00:00:00.000Z",
        )
        with pytest.raises(ValueError, match="text"):
            _run(transform(event))


class TestTransformFeatureFlag:
    def test_flag_off_returns_none(self, harness: _Harness) -> None:
        harness.flags_enabled = False
        assert _run(transform(_event("!so clubpenguinfan"))) is None

    def test_flag_off_never_touches_db(self, harness: _Harness) -> None:
        harness.flags_enabled = False

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("db must not be touched when the flag is off")

        harness.db.execute = _boom  # type: ignore[method-assign]
        assert _run(transform(_event("!so clubpenguinfan"))) is None

    def test_flag_checked_with_correct_key(self, harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _capture(key: str, default: bool) -> bool:
            captured["key"] = key
            captured["default"] = default
            return True

        import wit_world

        wit_world.imports.flags.enabled = _capture
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        _run(transform(_event("!so clubpenguinfan")))
        assert captured["key"] == _FEATURE_FLAG
        assert captured["default"] is False


class TestTransformUsageAndValidation:
    def test_bare_so_returns_usage(self, harness: _Harness) -> None:
        result = _run(transform(_event("!so")))
        assert result is not None
        assert result.payload["text"] == _SO_USAGE

    def test_whitespace_only_target_returns_usage(self, harness: _Harness) -> None:
        result = _run(transform(_event("!so      ")))
        assert result is not None
        assert result.payload["text"] == _SO_USAGE

    @pytest.mark.parametrize("target", ["ab", "a" * 26, "club-penguin-fan"])
    def test_invalid_login_is_rejected(self, harness: _Harness, target: str) -> None:
        result = _run(transform(_event(f"!so {target}")))
        assert result is not None
        assert result.payload["text"] == _INVALID_LOGIN_REPLY

    @pytest.mark.parametrize("target", ["abc", "a" * 25])
    def test_valid_length_boundaries_pass_validation(self, harness: _Harness, target: str) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event(f"!so {target}")))
        assert result is not None
        assert result.payload["target"] == target


class TestTransformSelfShoutout:
    def test_self_shoutout_denied(self, harness: _Harness) -> None:
        result = _run(transform(_event(f"!so {MOD_ACTOR}", actor=MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] == _SELF_SHOUTOUT_REPLY

    def test_self_shoutout_denied_case_and_at_insensitive(self, harness: _Harness) -> None:
        result = _run(transform(_event("!so @Mod_User", actor="mod_user")))
        assert result is not None
        assert result.payload["text"] == _SELF_SHOUTOUT_REPLY

    def test_different_target_is_not_self_shoutout(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event("!so someone_else", actor=MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] != _SELF_SHOUTOUT_REPLY


class TestTransformPermission:
    def test_mod_permission_denies_non_mod(self, harness: _Harness) -> None:
        harness.db.shoutout_config[COMMUNITY_ID] = {"so_permission": "mod", "cooldown_minutes": 60}
        result = _run(transform(_event("!so target_user", actor=NON_MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY

    def test_mod_permission_allows_moderator(self, harness: _Harness) -> None:
        harness.db.shoutout_config[COMMUNITY_ID] = {"so_permission": "mod", "cooldown_minutes": 60}
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event("!so target_user", actor=MOD_ACTOR)))
        assert result is not None
        assert "target" in result.payload

    def test_everyone_permission_allows_non_mod(self, harness: _Harness) -> None:
        harness.db.shoutout_config[COMMUNITY_ID] = {
            "so_permission": "everyone",
            "cooldown_minutes": 60,
        }
        result = _run(transform(_event("!so target_user", actor=NON_MOD_ACTOR)))
        assert result is not None
        assert "target" in result.payload

    def test_admin_only_denies_moderator(self, harness: _Harness) -> None:
        harness.db.shoutout_config[COMMUNITY_ID] = {
            "so_permission": "admin_only",
            "cooldown_minutes": 60,
        }
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event("!so target_user", actor=MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY

    def test_admin_only_allows_admin(self, harness: _Harness) -> None:
        harness.db.shoutout_config[COMMUNITY_ID] = {
            "so_permission": "admin_only",
            "cooldown_minutes": 60,
        }
        harness.db.roles_by_display_name[(COMMUNITY_ID, ADMIN_ACTOR)] = "admin"
        result = _run(transform(_event("!so target_user", actor=ADMIN_ACTOR)))
        assert result is not None
        assert "target" in result.payload

    def test_missing_config_row_defaults_to_mod(self, harness: _Harness) -> None:
        denied = _run(transform(_event("!so target_user", actor=NON_MOD_ACTOR)))
        assert denied is not None
        assert denied.payload["text"] == _PERMISSION_DENIED_REPLY

        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        allowed = _run(transform(_event("!so target_user", actor=MOD_ACTOR)))
        assert allowed is not None
        assert "target" in allowed.payload

    def test_config_lookup_error_defaults_to_mod_and_denies_non_mod(self, harness: _Harness) -> None:
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("simulated shoutout_config outage")

        harness.db.execute = _boom  # type: ignore[method-assign]
        result = _run(transform(_event("!so target_user", actor=NON_MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY

    def test_allowed_by_platform_user_id_match(self, harness: _Harness) -> None:
        harness.db.roles_by_platform[(COMMUNITY_ID, "twitch", "platform-user-1")] = "admin"
        result = _run(transform(_event("!so target_user", actor=NON_MOD_ACTOR)))
        assert result is not None
        assert "target" in result.payload

    def test_no_community_defaults_to_mod_and_denies_by_default(self, harness: _Harness) -> None:
        with bundle_context(tenant=TENANT, community=None, app_id="waddles.core.shoutout.default"):
            result = _run(transform(_event("!so target_user", actor=NON_MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY

    def test_non_numeric_community_defaults_to_mod_and_denies_by_default(
        self, harness: _Harness
    ) -> None:
        """A tenant-wide/non-integer community slug can't join `shoutout_config` -- fails closed."""
        with bundle_context(
            tenant=TENANT, community="not-a-number", app_id="waddles.core.shoutout.default"
        ):
            result = _run(transform(_event("!so target_user", actor=NON_MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY


class TestTransformCooldown:
    def test_on_cooldown_returns_reply_without_dispatch_payload(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        harness.kv.store[f"shoutout:cd:{COMMUNITY}:target_user"] = b"1"
        result = _run(transform(_event("!so target_user", actor=MOD_ACTOR)))
        assert result is not None
        assert "target" not in result.payload
        assert "recently" in result.payload["text"]

    def test_not_on_cooldown_forwards_normally(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event("!so target_user", actor=MOD_ACTOR)))
        assert result is not None
        assert result.payload["target"] == "target_user"

    def test_kv_denied_degrades_to_not_on_cooldown(self, harness: _Harness) -> None:
        """`kv` is currently hardcoded `denied` host-side (capabilities.rs) -- must degrade,
        never block the shoutout. See `app._is_on_cooldown`'s own docstring for the gap."""
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"

        def _denied(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("kv capability denied")

        harness.kv.get = _denied  # type: ignore[method-assign]
        result = _run(transform(_event("!so target_user", actor=MOD_ACTOR)))
        assert result is not None
        assert result.payload["target"] == "target_user"

    def test_permission_denied_checked_before_cooldown(self, harness: _Harness) -> None:
        """A cooldown key must never mask a permission denial (order matters)."""
        harness.db.shoutout_config[COMMUNITY_ID] = {"so_permission": "mod", "cooldown_minutes": 60}
        harness.kv.store[f"shoutout:cd:{COMMUNITY}:target_user"] = b"1"
        result = _run(transform(_event("!so target_user", actor=NON_MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] == _PERMISSION_DENIED_REPLY


def _sample_envelope(
    *,
    cooldown_minutes: int = 60,
    channel_id: str | None = "12345",
    target: str | None = "clubpenguinfan",
    command: str = "so",
) -> StageEnvelope:
    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "cooldown_minutes": cooldown_minutes,
        "command": command,
    }
    if target is not None:
        payload["target"] = target
    return StageEnvelope(
        tenant=TENANT,
        community=COMMUNITY,
        app_id="waddles.core.shoutout.default",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor=MOD_ACTOR,
            payload=payload,
            occurred_at="2026-09-28T00:00:00.000Z",
        ),
        ts="2026-09-28T00:00:00.000Z",
    )


class TestDispatch:
    def test_relays_to_the_events_own_origin_platform(self, harness: _Harness) -> None:
        envelope = _sample_envelope()
        result = _run(dispatch(envelope, {}, http_client=None))
        assert result.transport == "twitch"
        provider, message_json = harness.relay.calls[0]
        assert provider == "twitch"
        assert "clubpenguinfan" in json.loads(message_json)["text"]

    def test_sets_cooldown_after_successful_relay(self, harness: _Harness) -> None:
        envelope = _sample_envelope(cooldown_minutes=5)
        _run(dispatch(envelope, {}, http_client=None))
        key, value, ttl = harness.kv.set_calls[0]
        assert key == f"shoutout:cd:{COMMUNITY}:clubpenguinfan"
        assert ttl == 5 * 60

    def test_zero_cooldown_minutes_skips_kv_set(self, harness: _Harness) -> None:
        envelope = _sample_envelope(cooldown_minutes=0)
        _run(dispatch(envelope, {}, http_client=None))
        assert harness.kv.set_calls == []

    def test_kv_denied_degrades_without_failing_the_shoutout(self, harness: _Harness) -> None:
        """`kv.set()` denial (capability gap, see `app._set_cooldown`) must not fail dispatch."""

        def _denied(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("kv capability denied")

        harness.kv.set = _denied  # type: ignore[method-assign]
        envelope = _sample_envelope()
        result = _run(dispatch(envelope, {}, http_client=None))
        assert result.transport == "twitch"
        assert harness.relay.calls  # the shoutout itself still went out

    def test_missing_channel_id_raises(self, harness: _Harness) -> None:
        envelope = _sample_envelope(channel_id=None)
        with pytest.raises(ValueError, match="channel_id"):
            _run(dispatch(envelope, {}, http_client=None))
        assert harness.relay.calls == []

    def test_missing_target_raises(self, harness: _Harness) -> None:
        envelope = _sample_envelope(target=None)
        with pytest.raises(ValueError, match="target"):
            _run(dispatch(envelope, {}, http_client=None))
        assert harness.relay.calls == []

    def test_no_http_client_degrades_to_minimal_template(self, harness: _Harness) -> None:
        envelope = _sample_envelope()
        _run(dispatch(envelope, {}, http_client=None))
        _, message_json = harness.relay.calls[0]
        assert json.loads(message_json)["text"] == "Shoutout to clubpenguinfan! Check them out at twitch.tv/clubpenguinfan"

    def test_http_client_error_degrades_to_minimal_template(self, harness: _Harness) -> None:
        class _BoomHttpClient:
            async def get(self, *_a: Any, **_k: Any) -> Any:
                raise RuntimeError("simulated egress denial")

        envelope = _sample_envelope()
        result = _run(dispatch(envelope, {}, http_client=_BoomHttpClient()))
        assert result.transport == "twitch"
        _, message_json = harness.relay.calls[0]
        assert "clubpenguinfan" in json.loads(message_json)["text"]

    def test_http_client_success_enriches_with_display_name(self, harness: _Harness) -> None:
        class _OkHttpClient:
            async def get(self, *_a: Any, **_k: Any) -> dict[str, Any]:
                return {
                    "status": 200,
                    "body": json.dumps({"data": [{"display_name": "ClubPenguinFan"}]}).encode(),
                }

        envelope = _sample_envelope()
        _run(dispatch(envelope, {}, http_client=_OkHttpClient()))
        _, message_json = harness.relay.calls[0]
        assert "ClubPenguinFan" in json.loads(message_json)["text"]

    def test_http_client_malformed_body_degrades_to_minimal_template(self, harness: _Harness) -> None:
        class _MalformedBodyHttpClient:
            async def get(self, *_a: Any, **_k: Any) -> dict[str, Any]:
                return {"status": 200, "body": b"not-json"}

        envelope = _sample_envelope()
        _run(dispatch(envelope, {}, http_client=_MalformedBodyHttpClient()))
        _, message_json = harness.relay.calls[0]
        assert "clubpenguinfan" in json.loads(message_json)["text"]

    def test_http_client_non_200_degrades_to_minimal_template(self, harness: _Harness) -> None:
        class _NotFoundHttpClient:
            async def get(self, *_a: Any, **_k: Any) -> dict[str, Any]:
                return {"status": 404, "body": b"{}"}

        envelope = _sample_envelope()
        _run(dispatch(envelope, {}, http_client=_NotFoundHttpClient()))
        _, message_json = harness.relay.calls[0]
        assert "clubpenguinfan" in json.loads(message_json)["text"]


# --------------------------------------------------------------------------
# `!aiso` -- AI-generated shoutout (module docstring gap 5).
# --------------------------------------------------------------------------


class TestTransformAiso:
    def test_bare_aiso_returns_usage(self, harness: _Harness) -> None:
        result = _run(transform(_event("!aiso")))
        assert result is not None
        assert result.payload["text"] == _AISO_USAGE

    def test_eligible_aiso_forwards_with_aiso_command(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event("!aiso clubpenguinfan")))
        assert result is not None
        assert result.payload["command"] == "aiso"
        assert result.payload["target"] == "clubpenguinfan"

    def test_downgrades_to_so_when_tier_not_enterprise(self, harness: _Harness) -> None:
        harness.tier = "professional"
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event("!aiso clubpenguinfan")))
        assert result is not None
        assert result.payload["command"] == "so"

    def test_downgrades_to_so_when_ai_flag_off(self, harness: _Harness) -> None:
        harness.ai_flag_enabled = False
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event("!aiso clubpenguinfan")))
        assert result is not None
        assert result.payload["command"] == "so"

    def test_downgrades_to_so_on_channel_cooldown(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        harness.kv.store["shoutout:aicd:chan:12345"] = b"1"
        result = _run(transform(_event("!aiso clubpenguinfan")))
        assert result is not None
        assert result.payload["command"] == "so"

    def test_downgrades_to_so_on_target_cooldown(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        harness.kv.store[f"shoutout:aicd:target:{COMMUNITY}:clubpenguinfan"] = b"1"
        result = _run(transform(_event("!aiso clubpenguinfan")))
        assert result is not None
        assert result.payload["command"] == "so"

    def test_kv_error_fails_closed_and_downgrades_to_so(self, harness: _Harness) -> None:
        """Unlike `!so`'s fail-OPEN cooldown, an unmetered AI call is a real cost surface --
        a `kv` outage must deny the AI path, never allow it through."""
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("kv outage")

        harness.kv.get = _boom  # type: ignore[method-assign]
        result = _run(transform(_event("!aiso clubpenguinfan")))
        assert result is not None
        assert result.payload["command"] == "so"

    def test_still_applies_shared_permission_and_self_shoutout_checks(self, harness: _Harness) -> None:
        result = _run(transform(_event(f"!aiso {MOD_ACTOR}", actor=MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] == _SELF_SHOUTOUT_REPLY

    def test_aiso_invalid_login_rejected(self, harness: _Harness) -> None:
        result = _run(transform(_event("!aiso ab")))
        assert result is not None
        assert result.payload["text"] == _INVALID_LOGIN_REPLY


class _FakeAiHttpClient:
    """Answers both Twitch Helix `GET`s and the WaddleAI completions `POST` `!aiso` needs."""

    def __init__(
        self,
        *,
        ai_text: str | None = "Big love for {user}, always a great watch!",
        ai_status: int = 200,
        stream_info: dict[str, Any] | None = None,
        user_info: dict[str, Any] | None = None,
        raise_on_post: bool = False,
    ) -> None:
        self.ai_calls: list[dict[str, Any]] = []
        self.ai_text = ai_text
        self.ai_status = ai_status
        self.stream_info = stream_info
        self.user_info = user_info
        self.raise_on_post = raise_on_post

    async def get(self, url: str, **_kwargs: Any) -> dict[str, Any]:
        if "helix/streams" in url:
            data = [self.stream_info] if self.stream_info else []
            return {"status": 200, "body": json.dumps({"data": data}).encode()}
        if "helix/users" in url:
            data = [self.user_info] if self.user_info else []
            return {"status": 200, "body": json.dumps({"data": data}).encode()}
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.ai_calls.append({"url": url, **kwargs})
        if self.raise_on_post:
            raise RuntimeError("simulated waddleai transport error")
        if self.ai_text is None:
            return {"status": self.ai_status, "body": b"{}"}
        return {
            "status": self.ai_status,
            "body": json.dumps({"text": self.ai_text}).encode(),
        }


class TestDispatchAiso:
    def test_success_substitutes_placeholder_with_display_name(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient(user_info={"display_name": "ClubPenguinFan"})
        envelope = _sample_envelope(command="aiso")
        result = _run(dispatch(envelope, {}, http_client=client))
        assert result.detail == "relayed_ai"
        _, message_json = harness.relay.calls[0]
        text = json.loads(message_json)["text"]
        assert "ClubPenguinFan" in text
        assert "{user}" not in text

    def test_prompt_never_contains_target_login_or_name(self, harness: _Harness) -> None:
        """Hard PII boundary: the outbound WaddleAI prompt must never contain the target's
        login/display name -- only the `{user}` placeholder and public stream metadata."""
        client = _FakeAiHttpClient(
            user_info={"display_name": "ClubPenguinFan"},
            stream_info={"game_name": "Just Chatting", "title": "hello!"},
        )
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        _run(dispatch(envelope, {}, http_client=client))
        assert len(client.ai_calls) == 1
        body = json.loads(client.ai_calls[0]["body"])
        prompt = body["prompt"]
        assert "clubpenguinfan" not in prompt.lower()
        assert "ClubPenguinFan" not in prompt
        assert "{user}" in prompt
        assert "Just Chatting" in prompt

    def test_ai_auth_uses_secret_ref_not_embedded_credential(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient()
        envelope = _sample_envelope(command="aiso")
        _run(dispatch(envelope, {}, http_client=client))
        secret_refs = client.ai_calls[0]["secret_refs"]
        assert secret_refs["Authorization"].name == "WADDLEAI_SERVICE_TOKEN"

    def test_falls_back_on_missing_placeholder(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient(ai_text="Great streamer, no placeholder here!")
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        result = _run(dispatch(envelope, {}, http_client=client))
        assert result.detail == "relayed"
        _, message_json = harness.relay.calls[0]
        assert "clubpenguinfan" in json.loads(message_json)["text"]

    def test_falls_back_on_moderation_hit(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient(ai_text="Screw off {user}, this is shit!")
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        result = _run(dispatch(envelope, {}, http_client=client))
        assert result.detail == "relayed"
        _, message_json = harness.relay.calls[0]
        assert "clubpenguinfan" in json.loads(message_json)["text"]

    def test_falls_back_on_url_in_ai_text(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient(ai_text="Check out {user} at https://evil.example!")
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        result = _run(dispatch(envelope, {}, http_client=client))
        assert result.detail == "relayed"

    def test_falls_back_on_non_200(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient(ai_status=503)
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        result = _run(dispatch(envelope, {}, http_client=client))
        assert result.detail == "relayed"
        _, message_json = harness.relay.calls[0]
        assert "clubpenguinfan" in json.loads(message_json)["text"]

    def test_falls_back_on_transport_error(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient(raise_on_post=True)
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        result = _run(dispatch(envelope, {}, http_client=client))
        assert result.detail == "relayed"

    def test_falls_back_on_missing_text_field(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient(ai_text=None)
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        result = _run(dispatch(envelope, {}, http_client=client))
        assert result.detail == "relayed"

    def test_falls_back_when_no_http_client(self, harness: _Harness) -> None:
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        result = _run(dispatch(envelope, {}, http_client=None))
        assert result.detail == "relayed"
        _, message_json = harness.relay.calls[0]
        assert "clubpenguinfan" in json.loads(message_json)["text"]

    def test_output_capped_to_max_chars(self, harness: _Harness) -> None:
        from app import _AI_OUTPUT_MAX_CHARS

        client = _FakeAiHttpClient(ai_text="{user} " + ("x" * 1000))
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        _run(dispatch(envelope, {}, http_client=client))
        _, message_json = harness.relay.calls[0]
        assert len(json.loads(message_json)["text"]) <= _AI_OUTPUT_MAX_CHARS

    def test_sets_both_cooldown_buckets_before_generation_even_on_failure(
        self, harness: _Harness
    ) -> None:
        """Cooldown is set BEFORE the WaddleAI call (fail-closed metering) -- a failing
        generation must still count against the window, never allow a retry-storm."""
        client = _FakeAiHttpClient(raise_on_post=True)
        envelope = _sample_envelope(command="aiso", channel_id="12345", target="clubpenguinfan")
        _run(dispatch(envelope, {}, http_client=client))
        assert harness.kv.store.get("shoutout:aicd:chan:12345") == b"1"
        assert harness.kv.store.get(f"shoutout:aicd:target:{COMMUNITY}:clubpenguinfan") == b"1"

    def test_uses_config_waddleai_base_url_override(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient()
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        _run(dispatch(envelope, {"waddleai_base_url": "https://ai.internal.example"}, http_client=client))
        assert client.ai_calls[0]["url"].startswith("https://ai.internal.example/")

    def test_emits_latency_and_fallback_metrics_via_log(self, harness: _Harness) -> None:
        client = _FakeAiHttpClient(raise_on_post=True)
        envelope = _sample_envelope(command="aiso", target="clubpenguinfan")
        _run(dispatch(envelope, {}, http_client=client))
        messages = [call[1] for call in harness.log.calls]
        assert "shoutout_ai_latency" in messages
        assert "shoutout_ai_fallback" in messages

    def test_so_command_never_calls_waddleai(self, harness: _Harness) -> None:
        """A plain `!so` (or a downgraded `!aiso`) must never touch the AI HTTP path."""
        client = _FakeAiHttpClient()
        envelope = _sample_envelope(command="so", target="clubpenguinfan")
        _run(dispatch(envelope, {}, http_client=client))
        assert client.ai_calls == []


class TestEntryWiring:
    """`_entry_wiring.py` is the hand-authored `bundle_compiler` stand-in -- see its docstring."""

    def test_wires_bundle_transform_and_dispatch_to_app_module(self) -> None:
        import _entry_wiring

        assert _entry_wiring.bundle_transform is transform
        assert _entry_wiring.bundle_dispatch is dispatch
