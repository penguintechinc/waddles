"""Backfill coverage for the `secret` bundle: every failure step, corrupt index, PII-free logs.

`test_app.py` covers the ordered happy path and the headline failure modes; this module covers
every remaining failure step of the flow -- identity-index backend error / corruption, a bad
community id, a missing `message_id`, a malformed store response body, a missing `hub_api_url`
config -- each of which must (a) fail loud with the right step, (b) send ONLY the generic public
notice, (c) never reach `chat.delete`/`dm.send` after a pre-delete failure, and (d) never log the
secret, the username, the token, the target UUID or the host error text. It also pins the input
bounds, both-flags-required gating with a fail-closed default, the credential never entering the
guest (`secret_refs`, not headers/body), the gh-631 key-charset regression for the identity-index
key, and the static `_entry_wiring` re-export. `secret` has no moderator-gated verb (any member may
send a secret; the host throttles DMs), so there is no mod-gate matrix here.

Reuses `test_app.py`'s `host` fixture and helpers.
"""

# F811: pytest fixtures re-exported from `test_app.py` are re-bound by the test parameters.
# ruff: noqa: F811
from __future__ import annotations

import json
import sys
from typing import Any

import _entry_wiring
import app
import pytest
from app import (
    _ERR_DELETE,
    _ERR_DM,
    _ERR_STORE,
    _ERR_TARGET,
    _ERR_UNSUPPORTED,
    _MAX_MESSAGE_CHARS,
    _USAGE,
    FEATURE_FLAG_KEY,
    FLAG_KEY,
    SERVICE_TOKEN_SECRET,
    SUPPORTED_PLATFORMS,
    DispatchResult,
    SecretFlowError,
    _normalize_target,
    _target_key,
    dispatch,
    transform,
)
from test_app import (
    CONFIG,
    SECRET_TEXT,
    TARGET_UUID,
    TOKEN,
    USERNAME,
    FakeHttp,
    _envelope,
    _event,
    _run,
    _secret_event,
    host,  # noqa: F401 - fixture re-exported for this module
)
from waddle_sdk.http import SecretRef
from waddle_sdk.kv import validate_key

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0


class _BodyHttp(FakeHttp):
    """`FakeHttp` that returns a caller-supplied response instead of the canned success body."""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__()
        self.response = response

    async def post(self, url: str, **kw: Any) -> dict[str, Any]:
        self.calls.append((url, kw))
        return self.response


def _flow(
    event: Any,
    *,
    http: Any = None,
    config: dict[str, Any] | None = None,
    community: str | None = "7",
) -> Any:
    return _run(
        dispatch(
            _envelope(event, community=community),
            CONFIG if config is None else config,
            http_client=FakeHttp() if http is None else http,
        )
    )


# -- identity index: backend error + corruption
def test_identity_index_backend_error_fails_loud_before_store_and_never_leaks(host: Any) -> None:
    def _boom(_key: str) -> bytes:
        raise RuntimeError(f"kv echoed {CANARY} {USERNAME}")

    sys.modules["wit_world"].imports.kv.get = _boom
    http = FakeHttp()

    with pytest.raises(SecretFlowError) as err:
        _flow(_secret_event(), http=http)

    assert err.value.step == "resolve"
    assert http.calls == []
    assert host.events == ["chat.send"] and host.relayed[0]["text"] == _ERR_TARGET
    errors = [(m, json.loads(f)) for lvl, m, f in host.logs if lvl == ERROR_LEVEL]
    assert ("secret.resolve_failed", {"community_id": "7", "exc": "RuntimeError"}) in errors
    assert CANARY not in repr(host.logs) and USERNAME not in repr(host.logs)


@pytest.mark.parametrize(
    "blob",
    [
        b"\xff\xfe",
        b"not json",
        b"[]",
        b"null",
        b'"string"',
        json.dumps({"uuid": TARGET_UUID}).encode(),  # missing platform_user_id
        json.dumps({"platform_user_id": "1"}).encode(),  # missing uuid
    ],
)
def test_corrupt_identity_index_fails_loud_before_store_and_is_never_overwritten(
    host: Any, blob: bytes
) -> None:
    key = "c.7." + _target_key(USERNAME)
    host.kv[key] = blob
    http = FakeHttp()

    with pytest.raises(SecretFlowError) as err:
        _flow(_secret_event(), http=http)

    assert err.value.step == "index_corrupt"
    assert http.calls == [] and host.events == ["chat.send"]
    assert host.relayed[0]["text"] == _ERR_TARGET
    assert host.kv[key] == blob
    errors = [m for lvl, m, _f in host.logs if lvl == ERROR_LEVEL]
    assert "secret.index_corrupt" in errors
    assert USERNAME not in repr(host.logs)


def test_target_lookup_is_case_insensitive_and_at_prefix_tolerant(host: Any) -> None:
    for text in (f"!secret @{USERNAME.lower()} hi", f"!secret {USERNAME.upper()} hi"):
        out = _run(transform(_event(text)))
        assert out is not None and out.payload["command"] == "secret"
        _flow(out)
    assert host.events.count("dm.send") == 2


# -- other pre-delete failure steps
def test_non_numeric_community_id_fails_before_any_http_call(host: Any) -> None:
    http = FakeHttp()
    with pytest.raises(SecretFlowError) as err:
        _flow(_secret_event(), http=http, community="not-an-int")
    # the identity index is community-scoped, so a non-"7" community also has no linked target
    assert err.value.step in {"target_unlinked", "bad_community"}
    assert http.calls == [] and host.events == ["chat.send"]


def test_bad_community_step_when_the_identity_index_resolves(host: Any) -> None:
    host.kv["c.not-an-int." + _target_key(USERNAME)] = json.dumps(
        {"uuid": TARGET_UUID, "platform_user_id": "424242"}
    ).encode()
    http = FakeHttp()
    with pytest.raises(SecretFlowError) as err:
        _flow(_secret_event(), http=http, community="not-an-int")
    assert err.value.step == "bad_community"
    assert http.calls == [] and host.events == ["chat.send"]
    assert host.relayed[0]["text"] == _ERR_STORE


def test_missing_message_id_fails_loud_without_touching_kv_or_http(host: Any) -> None:
    event = _secret_event()
    event.payload["message_id"] = None
    http = FakeHttp()
    with pytest.raises(SecretFlowError) as err:
        _flow(event, http=http)
    assert err.value.step == "missing_context"
    assert http.calls == [] and host.events == ["chat.send"]
    assert host.relayed[0]["text"] == _ERR_STORE


def test_missing_community_fails_loud_without_touching_http(host: Any) -> None:
    http = FakeHttp()
    with pytest.raises(SecretFlowError) as err:
        _flow(_secret_event(), http=http, community=None)
    assert err.value.step == "missing_context"
    assert http.calls == [] and host.events == ["chat.send"]


@pytest.mark.parametrize(
    "response",
    [
        {"status": 201, "body": b"not json"},
        {"status": 201, "body": json.dumps({}).encode()},
        {"status": 201, "body": json.dumps({"token": None, "x": 1}).encode()[:5]},
        {"status": 201, "body": None},
        {"status": 201},
        {"status": 200, "body": json.dumps({"token": TOKEN}).encode()},
        {"status": 403, "body": b""},
        {"status": 500, "body": b"boom"},
        {},
    ],
    ids=[
        "non-json",
        "no-token",
        "truncated",
        "none-body",
        "no-body",
        "wrong-2xx",
        "403",
        "500",
        "empty",
    ],
)
def test_malformed_or_non_201_store_responses_fail_before_delete_and_dm(
    host: Any, response: dict[str, Any]
) -> None:
    http = _BodyHttp(response)
    with pytest.raises(SecretFlowError) as err:
        _flow(_secret_event(), http=http)

    assert err.value.step in {"store_body", "store_status"}
    assert host.events == ["chat.send"] and host.relayed[0]["text"] == _ERR_STORE
    assert "chat.delete" not in host.events and "dm.send" not in host.events


@pytest.mark.parametrize("missing", ["hub_api_url", "webui_url"])
def test_missing_config_fails_loud_and_never_completes_the_flow(host: Any, missing: str) -> None:
    config = {k: v for k, v in CONFIG.items() if k != missing}
    http = FakeHttp()
    with pytest.raises(Exception) as err:  # noqa: PT011 - step differs by which key is missing
        _flow(_secret_event(), http=http, config=config)

    assert "Secret delivered by DM." not in [r.get("text") for r in host.relayed]
    if missing == "hub_api_url":
        assert isinstance(err.value, SecretFlowError) and err.value.step == "store"
        assert host.events == ["chat.send"]
    else:
        # `webui_url` is only needed to build the DM link, i.e. after the delete: a KeyError
        # (config contract violation, `required_config` in bundle.yaml) surfaces loudly.
        assert isinstance(err.value, KeyError)


def test_transport_exception_text_is_never_logged(host: Any) -> None:
    http = FakeHttp(raises=RuntimeError(f"{SECRET_TEXT} {CANARY}"))
    with pytest.raises(SecretFlowError) as err:
        _flow(_secret_event(), http=http)
    assert err.value.step == "store"
    assert SECRET_TEXT not in repr(host.logs) and CANARY not in repr(host.logs)
    errors = [(m, json.loads(f)) for lvl, m, f in host.logs if lvl == ERROR_LEVEL]
    assert ("secret.store_failed", {"community_id": "7", "exc": "RuntimeError"}) in errors


def test_store_rejection_logs_only_the_status_code(host: Any) -> None:
    with pytest.raises(SecretFlowError):
        _flow(_secret_event(), http=FakeHttp(status=403))
    errors = [(m, json.loads(f)) for lvl, m, f in host.logs if lvl == ERROR_LEVEL]
    assert ("secret.store_rejected", {"community_id": "7", "status": 403}) in errors


# -- dispatch preconditions
def test_dispatch_without_a_channel_id_raises_and_relays_nothing(host: Any) -> None:
    event = _secret_event()
    event.payload["channel_id"] = None
    with pytest.raises(ValueError, match="channel_id"):
        _flow(event)
    assert host.relayed == []


@pytest.mark.parametrize("command", [None, "bogus", "SECRET", ""])
def test_unrecognized_command_raises_and_relays_nothing(host: Any, command: Any) -> None:
    event = _secret_event()
    event.payload["command"] = command
    with pytest.raises(ValueError, match="unrecognized secret command"):
        _flow(event)
    assert host.relayed == []


def test_success_result_shape_and_exact_ok_notice(host: Any) -> None:
    result = _flow(_secret_event())
    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("discord", "relayed")
    assert result.sub_type is None and result.http_status is None
    assert host.relayed[-1] == {
        "v": 1,
        "op": "chat.send",
        "platform": "discord",
        "channel": "555",
        "text": "Secret delivered by DM.",
    }


def test_dm_link_normalizes_url_slashes_and_carries_the_token_only_in_the_fragment(
    host: Any,
) -> None:
    config = {"hub_api_url": CONFIG["hub_api_url"] + "/", "webui_url": CONFIG["webui_url"] + "//"}
    http = FakeHttp()
    _flow(_secret_event(), http=http, config=config)

    assert http.calls[0][0] == "https://hub-api.penguintech.cloud/api/v1/one-time-secrets"
    dm = next(r for r in host.relayed if r["op"] == "dm.send")
    assert dm["text"].endswith(f"https://waddles.example/secret#{TOKEN}")
    assert "?token" not in dm["text"] and SECRET_TEXT not in dm["text"]


def test_service_credential_travels_only_as_a_secret_ref_never_in_headers_or_body(
    host: Any,
) -> None:
    http = FakeHttp()
    _flow(_secret_event(), http=http)
    _url, kw = http.calls[0]
    assert kw["headers"] == {"Content-Type": "application/json"}
    assert set(kw["secret_refs"]) == {"Authorization"}
    ref = kw["secret_refs"]["Authorization"]
    assert isinstance(ref, SecretRef) and ref.name == SERVICE_TOKEN_SECRET  # a name, never a value
    assert b"Bearer" not in kw["body"] and SERVICE_TOKEN_SECRET.encode() not in kw["body"]


# -- input bounds + flags
@pytest.mark.parametrize(
    ("text", "command"),
    [
        (f"!secret {USERNAME} " + "x" * _MAX_MESSAGE_CHARS, "secret"),
        (f"!secret {USERNAME} " + "x" * (_MAX_MESSAGE_CHARS + 1), "usage"),
        ("!secret " + "a" * 32 + " hi", "secret"),
        ("!secret " + "a" * 33 + " hi", "usage"),
        (f"!secret @{USERNAME} hi there friend", "secret"),
        (f"!secret   {USERNAME}    spaced   message  ", "secret"),
        ("!SECRET bob hi", "secret"),
        (f"!secret {USERNAME}", "usage"),
        (f"!secret {USERNAME}    ", "usage"),
        ("!secret @@bob hi", "usage"),
        ("!secret -bob hi", "usage"),
    ],
    ids=[
        "msg-max",
        "msg-over",
        "name-max",
        "name-over",
        "at-prefix",
        "spacing",
        "upper",
        "no-msg",
        "blank-msg",
        "double-at",
        "leading-dash",
    ],
)
def test_input_bounds_and_shapes(host: Any, text: str, command: str) -> None:
    out = _run(transform(_event(text)))
    assert out is not None and out.payload["command"] == command


@pytest.mark.parametrize("text", [None, 42, ["!secret a b"], b"!secret a b"])
def test_non_string_text_is_ignored_before_any_flag_check(host: Any, text: Any) -> None:
    event = _event("x")
    event.payload["text"] = text
    assert _run(transform(event)) is None
    assert host.logs == []


def test_message_is_forwarded_verbatim_after_trimming_only_the_ends(host: Any) -> None:
    out = _run(transform(_event(f"!secret {USERNAME}   keep  inner   spaces  ")))
    assert out is not None and out.payload["message"] == "keep  inner   spaces"
    assert out.payload["target"] == USERNAME


def test_usage_path_neither_echoes_nor_logs_the_malformed_text(host: Any) -> None:
    out = _run(transform(_event(f"!secret bad$name {CANARY}")))
    assert out is not None and out.payload["command"] == "usage"
    assert CANARY not in json.dumps(out.payload)
    assert CANARY not in repr(host.logs)
    _flow(out)
    assert host.relayed[-1]["text"] == _USAGE


@pytest.mark.parametrize("off", [FLAG_KEY, FEATURE_FLAG_KEY])
def test_both_flags_are_required_and_each_defaults_closed(host: Any, off: str) -> None:
    asked: list[tuple[str, bool]] = []

    def _enabled(key: str, default_value: bool) -> bool:
        asked.append((key, default_value))
        return key != off

    sys.modules["wit_world"].imports.flags = type("F", (), {"enabled": staticmethod(_enabled)})()
    assert _run(transform(_event(f"!secret {USERNAME} hi"))) is None
    assert all(default is False for _key, default in asked)
    assert asked[0] == (FLAG_KEY, False)
    assert FLAG_KEY == "waddles.command-secret" and FEATURE_FLAG_KEY == "waddles.secret-messaging"


def test_flag_server_outage_echoing_the_default_keeps_the_command_off(host: Any) -> None:
    sys.modules["wit_world"].imports.flags.enabled = lambda key, default_value: default_value
    assert _run(transform(_event(f"!secret {USERNAME} hi"))) is None


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_event(f"!secret {USERNAME} hi"))) is None


def test_flag_off_logs_nothing_and_does_no_io(host: Any) -> None:
    host.flags = False
    assert _run(transform(_event(f"!secret {USERNAME} {CANARY}"))) is None
    assert host.logs == [] and host.events == []


def test_supported_platforms_are_exactly_discord_and_others_are_refused_up_front(host: Any) -> None:
    assert SUPPORTED_PLATFORMS == frozenset({"discord"})
    for platform in ("twitch", "slack", ""):
        http = FakeHttp()
        host.events.clear()
        host.relayed.clear()
        with pytest.raises(SecretFlowError) as err:
            _flow(_envelope_for(platform), http=http)
        assert err.value.step == "unsupported_platform"
        assert http.calls == [] and host.events == ["chat.send"]
        assert host.relayed[0]["text"] == _ERR_UNSUPPORTED


def _envelope_for(platform: str) -> Any:
    event = _secret_event()
    event.platform = platform
    return event


# -- identity-index key (gh-631)
def test_identity_index_key_passes_the_host_charset_and_hides_the_username() -> None:
    """regression: gh-631 -- the index key is `.`-separated hex; the host rejects `:`."""
    key = _target_key(f"@{CANARY}".lstrip("@"))
    assert key.startswith("secret.target.") and CANARY.lower() not in key.lower()
    validate_key("c.7." + key)
    assert ":" not in key
    assert _target_key("Bob") == _target_key("bOB") != _target_key("bob2")
    assert len(key.split(".")[-1]) == 64


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a", "a"),
        ("@a", "a"),
        ("A" * 32, "A" * 32),
        ("", None),
        ("@", None),
        ("@@a", None),
        ("A" * 33, None),
        ("bad name", None),
        ("-x", None),
    ],
)
def test_normalize_target_boundaries(raw: str, expected: str | None) -> None:
    assert _normalize_target(raw) == expected


# -- PII-free logs
def test_no_log_line_in_any_failure_path_contains_secret_text_username_token_or_uuid(
    host: Any,
) -> None:
    """PII-free-log regression across every flow outcome (success + each failure step)."""
    secret_event = _run(transform(_event(f"!secret @{USERNAME} {CANARY} {SECRET_TEXT}")))
    assert secret_event is not None

    _flow(secret_event)  # success
    for fail_op in ("chat.delete", "dm.send"):
        host.fail_op = fail_op
        with pytest.raises(SecretFlowError):
            _flow(secret_event)
    host.fail_op = None
    for http in (
        FakeHttp(status=500),
        FakeHttp(raises=RuntimeError(f"{CANARY} {SECRET_TEXT}")),
        _BodyHttp({"status": 201, "body": b"{"}),
    ):
        with pytest.raises(SecretFlowError):
            _flow(secret_event, http=http)
    host.kv["c.7." + _target_key(USERNAME)] = b"corrupt"
    with pytest.raises(SecretFlowError):
        _flow(secret_event)

    assert host.logs, "expected log lines (denominator must be non-zero)"
    blob = repr(host.logs).lower()
    for needle in (CANARY, SECRET_TEXT, USERNAME, TOKEN, TARGET_UUID, "424242"):
        assert needle.lower() not in blob


def test_replies_to_the_public_channel_are_generic_for_every_failure(host: Any) -> None:
    replies = set()
    event = _secret_event()
    host.fail_op = "chat.delete"
    with pytest.raises(SecretFlowError):
        _flow(event)
    replies.add(host.relayed[-1]["text"])
    host.fail_op = "dm.send"
    with pytest.raises(SecretFlowError):
        _flow(event)
    replies.add(host.relayed[-1]["text"])
    assert replies == {_ERR_DELETE, _ERR_DM}
    for reply in replies:
        for needle in (SECRET_TEXT, USERNAME, TOKEN, TARGET_UUID):
            assert needle not in reply


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
