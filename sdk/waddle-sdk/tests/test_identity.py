"""Tests for `waddle_sdk.identity` (actor / mention -> community user_uuid)."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest

import waddle_sdk.identity as identity
from waddle_sdk.testing import (
    FakeIdentityHost,
    _Error_Backend,
    _Error_Denied,
    _Error_Invalid,
    _Error_Unavailable,
    _FakeWitError,
    install_fake_identity_host,
)

ALICE = str(uuid4())
BOB = str(uuid4())
TOKEN_BOB = str(uuid4())  # an opaque pseudonym token as shown in `{user:<token>}`


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("identity coroutine unexpectedly suspended")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeIdentityHost:
    h = install_fake_identity_host(monkeypatch, {ALICE, BOB}, actor=ALICE)
    h.add_mention(TOKEN_BOB, BOB)
    return h


# --- happy paths -------------------------------------------------------------


def test_resolve_actor_returns_the_community_user_uuid(host: FakeIdentityHost) -> None:
    assert _run(identity.resolve_actor()) == ALICE
    assert host.calls == ["actor"]


def test_resolve_mention_accepts_the_bare_token_or_the_full_placeholder(
    host: FakeIdentityHost,
) -> None:
    assert _run(identity.resolve_mention(TOKEN_BOB)) == BOB
    assert _run(identity.resolve_mention(f"{{user:{TOKEN_BOB}}}")) == BOB
    assert _run(identity.resolve_mention(f"  {TOKEN_BOB.upper()}  ")) == BOB


def test_a_game_flow_resolves_actor_and_mention_and_composes_with_economy(
    host: FakeIdentityHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`!steal @bob`: the uuids that reach economy are exactly the resolved ones."""
    text = f"!steal {{user:{TOKEN_BOB}}} 25"
    (token,) = identity.mention_tokens(text)
    actor = _run(identity.resolve_actor())
    target = _run(identity.resolve_mention(token))
    assert (actor, target) == (ALICE, BOB)


# --- mention_tokens ------------------------------------------------------------


def test_mention_tokens_extracts_every_placeholder_in_text_order() -> None:
    assert identity.mention_tokens("a {user:t1} b {user:t2} c {user:t1}") == ["t1", "t2", "t1"]
    assert identity.mention_tokens("no mentions here") == []
    assert identity.mention_tokens("") == []


def test_mention_tokens_skips_a_forged_placeholder_the_host_escaped() -> None:
    # The host escapes a chatter-typed `{user:x}` as `\{user:x\}`; a real
    # placeholder after an escaped backslash is still a placeholder.
    assert identity.mention_tokens(r"\{user:forged\} {user:real}") == ["real"]
    assert identity.mention_tokens(r"\\{user:real}") == ["real"]


def test_mention_tokens_ignores_unterminated_or_empty_placeholders() -> None:
    assert identity.mention_tokens("{user:") == []
    assert identity.mention_tokens("{user:}") == []
    assert identity.mention_tokens("{user:abc") == []


# --- fail-loud on every host refusal (never a swallowed default) --------------


def test_an_unlinked_actor_raises_not_linked_never_a_default(host: FakeIdentityHost) -> None:
    host.actor = None
    with pytest.raises(identity.NotLinkedError):
        _run(identity.resolve_actor())


def test_a_non_member_actor_raises_not_a_member(host: FakeIdentityHost) -> None:
    host.members.discard(ALICE)
    with pytest.raises(identity.NotAMemberError):
        _run(identity.resolve_actor())


def test_an_unknown_token_or_raw_handle_is_not_found_not_a_lookup(host: FakeIdentityHost) -> None:
    for probe in (str(uuid4()), "@bob", "bob", "<@123>"):
        with pytest.raises(identity.NotFoundError):
            _run(identity.resolve_mention(probe))


def test_unlinked_and_ambiguous_mentions_raise_their_own_errors(host: FakeIdentityHost) -> None:
    host.add_unlinked_mention("t-unlinked")
    host.add_ambiguous_mention("t-ambiguous")
    with pytest.raises(identity.NotLinkedError):
        _run(identity.resolve_mention("t-unlinked"))
    with pytest.raises(identity.AmbiguousError):
        _run(identity.resolve_mention("t-ambiguous"))


def test_a_mention_outside_the_community_is_not_a_member(host: FakeIdentityHost) -> None:
    host.add_mention("t-stranger", str(uuid4()))
    with pytest.raises(identity.NotAMemberError):
        _run(identity.resolve_mention("t-stranger"))


def test_gate_denial_raises_denied_error_with_the_gate_code(host: FakeIdentityHost) -> None:
    host.granted = False
    for coro in (identity.resolve_actor(), identity.resolve_mention(TOKEN_BOB)):
        with pytest.raises(identity.DeniedError) as ei:
            _run(coro)
        assert ei.value.code == "not_granted"


def _raising_host(monkeypatch: pytest.MonkeyPatch, variant: object) -> None:
    import sys
    import types

    def boom(*_args: object) -> str:
        raise _FakeWitError(variant)

    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        identity=types.SimpleNamespace(resolve_actor=boom, resolve_mention=boom)
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake)


@pytest.mark.parametrize(
    ("variant", "exc"),
    [
        (_Error_Unavailable("flag off"), identity.UnavailableError),
        (_Error_Backend("boom"), identity.BackendError),
        (_Error_Invalid("bad"), identity.InvalidIdentityArgError),
        (_Error_Denied("rate_limited"), identity.DeniedError),
    ],
)
def test_every_other_wit_error_variant_maps_to_its_exception(
    monkeypatch: pytest.MonkeyPatch, variant: object, exc: type[Exception]
) -> None:
    _raising_host(monkeypatch, variant)
    with pytest.raises(exc):
        _run(identity.resolve_actor())
    with pytest.raises(exc):
        _run(identity.resolve_mention(TOKEN_BOB))


def test_an_unrecognized_error_shape_is_a_loud_backend_error_not_a_swallow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raising_host(monkeypatch, object())
    with pytest.raises(identity.BackendError):
        _run(identity.resolve_actor())


# --- the success position is only ever a canonical uuid ------------------------


@pytest.mark.parametrize(
    "leaked",
    ["1001", "secret_handle", "<@123>", f"{{user:{ALICE}}}", ALICE.upper(), ALICE.replace("-", "")],
)
def test_a_non_canonical_or_non_uuid_host_value_is_never_returned(
    monkeypatch: pytest.MonkeyPatch, leaked: str
) -> None:
    import sys
    import types

    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        identity=types.SimpleNamespace(
            resolve_actor=lambda: leaked, resolve_mention=lambda token: leaked
        )
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake)
    with pytest.raises(identity.BackendError) as ei:
        _run(identity.resolve_actor())
    assert leaked not in str(ei.value), "the offending value must not be echoed"
    with pytest.raises(identity.BackendError):
        _run(identity.resolve_mention(TOKEN_BOB))


# --- argument validation (before any host call) --------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "x" * 257, "a\nb", "a\x00b"])
def test_a_malformed_token_is_rejected_before_any_host_call(
    host: FakeIdentityHost, bad: str
) -> None:
    with pytest.raises(identity.InvalidIdentityArgError):
        _run(identity.resolve_mention(bad))
    assert host.calls == []


def test_a_non_string_token_is_rejected(host: FakeIdentityHost) -> None:
    with pytest.raises(identity.InvalidIdentityArgError):
        _run(identity.resolve_mention(5))  # type: ignore[arg-type]
    assert host.calls == []


def test_errors_are_distinct_subclasses_of_identity_error() -> None:
    for cls in (
        identity.NotLinkedError,
        identity.NotAMemberError,
        identity.NotFoundError,
        identity.AmbiguousError,
        identity.UnavailableError,
        identity.BackendError,
        identity.DeniedError,
        identity.InvalidIdentityArgError,
    ):
        assert issubclass(cls, identity.IdentityError)
    assert issubclass(identity.InvalidIdentityArgError, ValueError)


# --- drift guard against the Rust host -----------------------------------------


def test_limits_match_the_executor_and_stage_rust_sources() -> None:
    root = Path(__file__).resolve().parents[3]
    executor = (
        root / "core" / "bundle_executor" / "src" / "host" / "stage_next_identity.rs"
    ).read_text()
    stage = (root / "core" / "svc_process" / "src" / "identity.rs").read_text()
    for source in (executor, stage):
        match = re.search(r"MAX_MENTION_TOKEN_LEN: usize = (\d+);", source)
        assert match is not None
        assert int(match.group(1)) == identity.MAX_MENTION_TOKEN_LEN


def test_the_fake_host_wire_vocabulary_matches_the_wit_error_variant() -> None:
    wit = (Path(__file__).resolve().parents[3] / "wit" / "waddle-bundle" / "stage.wit").read_text()
    block = wit[wit.index("interface identity {") :]
    block = block[: block.index("resolve-actor")]
    for case in (
        "denied(string)",
        "not-linked",
        "not-a-member",
        "not-found",
        "ambiguous",
        "invalid(string)",
        "unavailable(string)",
        "backend(string)",
    ):
        assert re.search(rf"^\s+{re.escape(case)},", block, re.M), case
