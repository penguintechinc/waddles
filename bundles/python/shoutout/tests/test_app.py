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
from typing import Any

import pytest
from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core.bundle_runtime import (
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    _FEATURE_FLAG,
    _INVALID_LOGIN_REPLY,
    _PERMISSION_DENIED_REPLY,
    _SELF_SHOUTOUT_REPLY,
    _SO_USAGE,
    dispatch,
    transform,
)

TENANT = "tenant-1"
COMMUNITY = "42"
COMMUNITY_ID = 42

MOD_ACTOR = "mod_user"
NON_MOD_ACTOR = "rando"
ADMIN_ACTOR = "owner_user"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _event(
    text: str, *, actor: str | None = MOD_ACTOR, **payload_overrides: object
) -> PlatformEvent:
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


@dataclass
class _Harness:
    db: _FakeDbHarness
    kv: _FakeKv
    relay: _FakeRelay
    flags_enabled: bool


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Install a fake `wit_world` covering every host import `app.py` touches."""
    db = _FakeDbHarness()
    kv = _FakeKv()
    relay = _FakeRelay()
    state = _Harness(db=db, kv=kv, relay=relay, flags_enabled=True)

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
    flags_mod = types.SimpleNamespace(enabled=lambda key, default: state.flags_enabled)

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        db=db_mod, kv=kv_mod, relay=relay_mod, flags=flags_mod
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

    @pytest.mark.parametrize("text", ["!sox something", "!vsoo someone", "hello", "", "   "])
    def test_non_matching_text_returns_none(self, harness: _Harness, text: str) -> None:
        assert _run(transform(_event(text))) is None

    def test_missing_text_field_raises(self, harness: _Harness) -> None:
        event = PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor=MOD_ACTOR,
            payload={},
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

    def test_flag_checked_with_correct_key(
        self, harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_config_lookup_error_defaults_to_mod_and_denies_non_mod(
        self, harness: _Harness
    ) -> None:
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
        """`kv` is hardcoded `denied` host-side (capabilities.rs) -- must degrade.

        Never block the shoutout. See `app._is_on_cooldown`'s own docstring for the gap.
        """
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
) -> StageEnvelope:
    payload: dict[str, Any] = {"channel_id": channel_id, "cooldown_minutes": cooldown_minutes}
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
        assert (
            json.loads(message_json)["text"]
            == "Shoutout to clubpenguinfan! Check them out at twitch.tv/clubpenguinfan"
        )

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

    def test_http_client_malformed_body_degrades_to_minimal_template(
        self, harness: _Harness
    ) -> None:
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


class TestEntryWiring:
    """`_entry_wiring.py` is the hand-authored `bundle_compiler` stand-in -- see its docstring."""

    def test_wires_bundle_transform_and_dispatch_to_app_module(self) -> None:
        import _entry_wiring

        assert _entry_wiring.bundle_transform is transform
        assert _entry_wiring.bundle_dispatch is dispatch
