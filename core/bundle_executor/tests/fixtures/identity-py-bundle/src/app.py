"""Test-only Python `stage-next` bundle that CALLS the `identity` host import.

Not a shipped bundle (no `bundles/core-bundles.yaml` entry, never seeded): it
exists so `core/bundle_executor/tests/stage_next_python_identity_e2e.rs` can
prove the whole Python path against a REAL componentize-py build -- the SDK's
eager wizening of `wit_world.imports.identity`, the `waddle_sdk.identity`
facade, and its fail-loud error mapping -- exactly the seam
`componentize-py-eager-wizen` documents as invisible to mocked tests.

It is also the REFERENCE SHAPE of a points-game bundle (`!steal @user`): it
reads the mention token out of the message text it was delivered, resolves the
actor and the mention to community uuids through the host, then moves currency
between exactly those two uuids -- and stops, surfacing a clear error, on any
refusal (an unlinked actor/target never reaches `economy.transfer`).

`event.event_type` selects the flow (`id-actor` / `id-mention` / `id-steal`);
the outcome is echoed back as `payload["result"]` (`ok:<uuid>` / `steal:ok` /
a stable error tag) so the test can assert on it.
"""

from __future__ import annotations

from dataclasses import replace

from waddle_sdk import economy, identity
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _tag(exc: Exception) -> str:
    """Render an `identity` / `economy` failure as a stable short tag."""
    if isinstance(exc, identity.DeniedError | economy.DeniedError):
        return f"denied:{exc.code}"
    if isinstance(exc, identity.NotLinkedError):
        return "not-linked"
    if isinstance(exc, identity.NotAMemberError | economy.NotAMemberError):
        return "not-a-member"
    if isinstance(exc, identity.NotFoundError):
        return "not-found"
    if isinstance(exc, identity.AmbiguousError):
        return "ambiguous"
    if isinstance(exc, identity.UnavailableError | economy.UnavailableError):
        return "unavailable"
    if isinstance(exc, identity.InvalidIdentityArgError | economy.InvalidEconomyArgError):
        return "invalid"
    return "backend"


async def _steal(text: str, amount: int) -> str:
    """The reference `!steal @target <amount>` flow; every hop fails loud and stops."""
    tokens = identity.mention_tokens(text)
    if not tokens:
        return "no-mention"
    try:
        actor = await identity.resolve_actor()
    except identity.IdentityError as exc:
        return f"actor:{_tag(exc)}"
    try:
        target = await identity.resolve_mention(tokens[0])
    except identity.IdentityError as exc:
        return f"target:{_tag(exc)}"
    try:
        await economy.transfer(actor, target, amount)
    except economy.EconomyError as exc:
        return f"transfer:{_tag(exc)}"
    return "steal:ok"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Run the identity flow named by `event_type` and echo the outcome."""
    payload = event.payload
    try:
        if event.event_type == "id-actor":
            result = f"ok:{await identity.resolve_actor()}"
        elif event.event_type == "id-mention":
            result = f"ok:{await identity.resolve_mention(str(payload['token']))}"
        elif event.event_type == "id-steal":
            result = await _steal(str(payload["text"]), int(payload["amount"]))
        else:
            return None
    except identity.IdentityError as exc:
        result = _tag(exc)
    return replace(event, payload={"result": result})


async def dispatch(envelope: StageEnvelope, config: dict[str, object], **_: object) -> None:
    """Unsupported: this bundle has no action stage."""
    raise NotImplementedError("identity test bundle has no action stage")
