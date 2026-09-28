"""`!quote` community-quotes chat bundle, migrated from `quote_interaction_module`.

Legacy `action/interactive/quote_interaction_module` is retained; retiring it
is a separate step. Implements both WIT exports against
`wit/waddle-bundle/stage.wit` via `waddle_sdk`, following the same
process/action split as `bundles/python/pyping/src/app.py`: `transform()`
(process-stage) is pure command parsing --
no host I/O, no side effects -- recognizing `!quote add|get|random|delete`
and packaging the parsed intent into the outgoing event's payload;
`dispatch()` (action-stage) does the actual work (permission check, quote
storage) and relays the final reply text back to the event's own origin
platform over the WIT `relay` host import, exactly like `pyping`'s own
`dispatch()`.

Narrower than the legacy module by design (this migration's scope): `add`,
`get`, `random`, `delete` only -- no update/search/list/stats/author-lookup
surface.

**Storage: `kv`, not `db` (deliberate, current direction).** The pre-existing
`quotes` Postgres table (`config/postgres/migrations/015_add_quote_tables.sql`)
would have been the natural fit -- and IS the mechanism a bundle uses to own
relational data here (manifest `data.tables` + RLS, see `wit/waddle-bundle/
stage.wit`'s own `db` interface doc comment; `sdk/waddle-sdk/tests/
test_oracle_alias_bundle.py` already proves the pattern against
`command_aliases`). But as of this PR, `db` is still only being designed,
while `kv` has active implementation work in flight
(`feature/bundle-kv-capability`) -- so this bundle is built against `kv`
instead, per that in-flight work's direction, and documented here rather than
against a capability with no near-term host wiring. See "Known limitation"
below.

**No index -- no read-modify-write race.** An earlier revision of this
bundle kept a `quotes:{community_id}:index` JSON array of live ids, updated
via read-modify-write on every add/delete; that's a lost-update race under
concurrent adds (`kv` has no compare-and-swap/transaction primitive), so it
is gone. Key layout instead:

  - `quotes:seq` -- an atomic counter (`kv.increment`, delta 1) handing out
    the next quote id; `kv.increment(key, 0, 0)` (delta 0) doubles as an
    atomic *peek* at the current high-water mark, used by `!quote random`
    below.
  - `quotes:q:{id}` -- one quote's JSON blob (`{"id", "text", "created_at"}`).
    No user-identifying field is stored at all (see PII note below).
    `!quote delete` hard-deletes this key -- no Postgres-style `deleted_at`
    tombstone; `kv` has no query language to filter tombstones by.

No `community_id` segment in either key: the host's own `kv` capability
already namespaces every key by `(tenant, community, app_id)` server-side
(spec SS6.5/SS7.4, "bundle-scoped key/value, stored under the bundle's own
`...:state` key") -- an earlier revision added a redundant guest-side
`community_id` segment defensively; dropped per review, since duplicating
scoping the host already guarantees just adds a second, unverified
assumption about the id's shape rather than removing one.

**`!quote random` without an index.** Peek the current max id via
`kv.increment(seq_key, 0, 0)`, pick a uniform random id in `[1, max_id]`, and
retry up to `_MAX_RANDOM_ATTEMPTS` times if that id was deleted (`kv.get`
returns nothing) -- cheap for a chat-quote board (ids are dense; only
deleted ones miss) and needs no separate live-id index to stay correct
under concurrent adds/deletes. Reports "no quotes yet" if every attempt
misses, same as a genuinely empty board.

**Follow-up once `db` lands.** This bundle is expected to move off `kv`
entirely onto the bundle-owns-tables `db` mechanism once it's implemented
(manifest `data.tables` + RLS, `wit/waddle-bundle/stage.wit`'s own `db`
interface doc comment) -- a single `core-<app-id>`-scoped table, not the
legacy `quotes` Postgres table, replacing every key above with real rows.
Tracked as a follow-up, not part of this PR.

**PII note.** No user identity is persisted anywhere by this bundle -- not
even the non-UUID platform display name the legacy `quotes.quoted_username`/
`added_by_user_id` columns carried. `!quote add <text>` has no author
sub-field (the legacy `author` parameter was already optional), so there is
nothing to attribute a quote to; the only user-identifying values this
module ever reads are the inbound event's own `actor`/payload role flags
(below), used for a permission decision, never written anywhere.

**Permission check, `!quote delete` only.** `add`/`get`/`random` are open to
any community member (matches the legacy module's own generic read/write
scopes, no admin-only gate); `delete` requires a community moderator/
broadcaster -- the legacy module never actually enforced this (its
`DELETE /quotes/<id>` route carried no `@require_scope`/`@tenant_middleware`
decorator at all), so this migration closes that gap rather than reproducing
it. With no `db` capability to read `community_members.role` (the mechanism
`core/svc_process/bundles/social_alias_process.py`'s own
`_caller_is_moderator_or_admin` uses), this checks the moderator/broadcaster
flags svc-ingest's Twitch normalizer already stamps onto every `chat.message`
payload (`is_mod`/`is_broadcaster`, `core/svc_ingest/src/normalize.rs`).
Discord's own normalizer does not populate an equivalent flag yet -- a
platform-parity gap outside this bundle's scope, not something invented
here -- so a Discord `!quote delete` fails closed (denied) until it does.
Deliberately NOT a bundle-owned KV admin list: that would duplicate, and risk
drifting from, the platform's own `community_members` role table, which only
`db` can read from inside the sandbox.

**Known limitation (document, don't work around):** the `kv` host capability
itself is still hardcoded `access-denied` in both `core/svc_process/src/
capabilities.rs` and `core/svc_action/src/capabilities.rs` as of this PR --
implementation is in flight on `feature/bundle-kv-capability`. This bundle's
storage/permission logic is written and unit-tested against a mocked `kv`
host (`tests/test_app.py`, `tests/wit_fakes.py`), the same oracle-proof
pattern `sdk/waddle-sdk/tests/test_oracle_alias_bundle.py` uses for `db`, so
it is ready to run the moment that host wiring merges -- it cannot execute
end to end against a live `kv` backend yet.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from typing import Any

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.bundle_runtime import get_bundle_context
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: The chat command this bundle reacts to (`!quote ...`).
_COMMAND = "quote"

#: PostHog flag key (per this migration's own requirement) -- default OFF
#: until validated, unlike `social_alias_process`'s always-on
#: `waddles.bot.command_aliases` (this is a new parallel implementation of an
#: existing feature, not yet proven in production).
_FEATURE_FLAG = "waddles.quotes-bundle"

#: Normalized `chat.message` payload flags treated as moderator/admin
#: authority for `!quote delete` -- see module docstring's permission-check
#: section for why this replaces a `community_members.role` lookup.
_MODERATOR_PAYLOAD_FLAGS = ("is_mod", "is_moderator", "is_broadcaster", "is_admin", "is_owner")

_USAGE = "Usage: !quote add <text> | !quote get <id> | !quote random | !quote delete <id>"
_COMMUNITY_REQUIRED_MSG = (
    "Quote commands require a community context and cannot be used tenant-wide."
)
_PERMISSION_DENIED_MSG = "only moderators/admins can delete quotes"
_NO_QUOTES_MSG = "no quotes yet -- add one with !quote add <text>"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: parse `!quote ...` into a dispatch intent.

    Pure command recognition -- no host I/O, no side effects (mirrors
    `bundles/python/pyping`'s own `transform()`). Returns `None` for any
    non-`!quote` text, or when `waddles.quotes-bundle` is off (fail-closed
    default, per this bundle's own feature-flag requirement).
    """
    raw_text = event.payload.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None

    text = raw_text.strip()
    if not text.startswith("!"):
        return None

    parts = text[1:].split(maxsplit=1)
    if not parts or parts[0].lower() != _COMMAND:
        return None
    rest = parts[1].strip() if len(parts) > 1 else ""

    ctx = get_bundle_context()
    enabled = await feature_enabled(
        _FEATURE_FLAG, tenant=ctx.tenant, community=_community_id(ctx.community), default=False
    )
    if not enabled:
        log.debug("quotes.flag_disabled", tenant=ctx.tenant)
        return None

    action, quote_id, quote_text, reply_hint = _parse_subcommand(rest)
    if ctx.community is None:
        action = "community_required"

    payload = {
        "quote_action": action,
        "quote_id": quote_id,
        "quote_text": quote_text,
        "reply_hint": reply_hint,
        "channel_id": event.payload.get("channel_id"),
        **{flag: event.payload.get(flag) for flag in _MODERATOR_PAYLOAD_FLAGS},
    }
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


def _parse_subcommand(rest: str) -> tuple[str, int | None, str | None, str | None]:
    """Parse the text after `!quote `.

    Bare/`random` -> random; `add`/`get`/`delete` -> their own id/text.
    """
    if not rest:
        return "random", None, None, None

    sub_parts = rest.split(maxsplit=1)
    sub = sub_parts[0].lower()
    sub_rest = sub_parts[1].strip() if len(sub_parts) > 1 else ""

    if sub == "random":
        return "random", None, None, None
    if sub == "add":
        if not sub_rest:
            return "usage", None, None, _USAGE
        return "add", None, sub_rest, None
    if sub == "get":
        quote_id = _parse_int(sub_rest)
        if quote_id is None:
            return "usage", None, None, _USAGE
        return "get", quote_id, None, None
    if sub in ("delete", "del", "remove"):
        quote_id = _parse_int(sub_rest)
        if quote_id is None:
            return "usage", None, None, _USAGE
        return "delete", quote_id, None, None
    return "usage", None, None, _USAGE


def _parse_int(value: str) -> int | None:
    """Best-effort `int(value)`; `None` on anything unparseable."""
    try:
        return int(value)
    except ValueError:
        return None


def _community_id(community: str | None) -> int | None:
    """Best-effort `int(community)` for the flag check; `None`/unparseable -> `None`."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- identical shape to `pyping`'s own.

    See `bundles/python/pyping/src/app.py`'s `DispatchResult` docstring for
    why `http_status` stays `None` (this bundle relays over a queue, not
    HTTP; a raised exception is `_component_entry`'s only failure signal).
    """

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: run the parsed `!quote` command and relay the reply.

    `config`/`http_client` are accepted but unused -- same signature
    constraint as `pyping`'s own `dispatch()` (`_component_entry.WitWorld.
    dispatch` always calls `bundle_dispatch(envelope, config,
    http_client=...)`).

    Raises:
        ValueError: The envelope's payload has no `channel_id` (the inbound
            `chat.message` never carried one) -- same convention as `pyping`.
    """
    payload = envelope.event.payload
    action = payload.get("quote_action")
    channel_id = payload.get("channel_id")
    provider = envelope.event.platform

    if action == "community_required":
        text = _COMMUNITY_REQUIRED_MSG
    elif action == "usage" or action is None:
        text = payload.get("reply_hint") or _USAGE
    elif envelope.community is None:
        text = _COMMUNITY_REQUIRED_MSG
    else:
        text = await _run_action(action, envelope.event, payload)

    if not channel_id:
        raise ValueError("quote reply requires a channel_id from the inbound chat.message")

    await relay.push(provider, {"channel": channel_id, "text": text})
    return DispatchResult(transport=provider, detail="relayed")


async def _run_action(action: str, event: PlatformEvent, payload: dict[str, Any]) -> str:
    """Execute one already-validated quote action and return the reply text.

    Every kv call is wrapped so a backend failure replies with an error
    message rather than crashing the pipeline (matches `social_alias_process`'s
    own `except Exception ... return f"Failed to ...: {exc}"` convention).
    """
    try:
        if action == "add":
            quote_text = payload.get("quote_text") or ""
            new_quote_id = await _add_quote(quote_text)
            log.info("quotes.added", quote_id=new_quote_id)
            return f"quote #{new_quote_id} added"

        if action == "get":
            raw_quote_id: Any = payload.get("quote_id")
            if not isinstance(raw_quote_id, int):
                return _USAGE
            quote_id = raw_quote_id
            quote = await _get_quote(quote_id)
            return _format_quote(quote) if quote is not None else f"no quote #{quote_id}"

        if action == "random":
            quote = await _random_quote()
            return _format_quote(quote) if quote is not None else _NO_QUOTES_MSG

        if action == "delete":
            raw_quote_id = payload.get("quote_id")
            if not isinstance(raw_quote_id, int):
                return _USAGE
            quote_id = raw_quote_id
            if not _caller_is_moderator_or_admin(payload):
                log.debug("quotes.delete_denied", actor=event.actor or "")
                return _PERMISSION_DENIED_MSG
            removed = await _delete_quote(quote_id)
            return f"quote #{quote_id} deleted" if removed else f"no quote #{quote_id}"
    except Exception as exc:  # noqa: BLE001 -- a quote command must reply, never crash the bot
        log.error("quotes.action_failed", action=action, error=str(exc))
        return f"Failed to {action} quote: {exc}"

    return _USAGE


def _caller_is_moderator_or_admin(payload: dict[str, Any]) -> bool:
    """Best-effort moderator/admin check from the inbound event's own normalized payload.

    See module docstring's permission-check section -- no `community_members`
    role lookup is available without `db`; this reads the moderator/
    broadcaster flags already present on the transformed payload (copied
    through from the inbound event by `transform()`).
    """
    return any(bool(payload.get(flag)) for flag in _MODERATOR_PAYLOAD_FLAGS)


#: `!quote random` retry budget when a picked id was previously deleted --
#: see module docstring's "`!quote random` without an index" section.
_MAX_RANDOM_ATTEMPTS = 5

#: The `kv` key holding the bundle's next-quote-id counter. No `community_id`
#: segment -- the host's own `kv` scoping already namespaces this per
#: `(tenant, community, app_id)`, see module docstring.
_SEQ_KEY = "quotes:seq"


def _quote_key(quote_id: int) -> str:
    """The `kv` key holding one quote's JSON blob."""
    return f"quotes:q:{quote_id}"


async def _add_quote(text: str) -> int:
    """Insert a new quote (unattributed -- see module docstring's PII note).

    `kv.increment` is the sole source of new ids -- atomic, so two concurrent
    adds always get distinct ids, no read-modify-write involved.
    """
    quote_id = await kv.increment(_SEQ_KEY, 1, 0)
    blob = {"id": quote_id, "text": text, "created_at": datetime.now(UTC).isoformat()}
    await kv.set(_quote_key(quote_id), json.dumps(blob).encode("utf-8"), 0)
    return quote_id


async def _get_quote(quote_id: int) -> dict[str, Any] | None:
    """Fetch one quote's JSON blob by id, or `None` if it doesn't exist (or was deleted)."""
    raw = await kv.get(_quote_key(quote_id))
    if not raw:
        return None
    try:
        blob: dict[str, Any] = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return blob


async def _random_quote() -> dict[str, Any] | None:
    """Return one random live quote, or `None` if there are none.

    No index to pick from -- peek the current high-water mark (an atomic
    `kv.increment(..., delta=0, ...)`, never mutating it) and retry a few
    uniform-random ids in `[1, max_id]` until one hits a quote that hasn't
    been deleted (see module docstring).
    """
    max_id = await kv.increment(_SEQ_KEY, 0, 0)
    if max_id < 1:
        return None
    for _ in range(_MAX_RANDOM_ATTEMPTS):
        # A quote pick, not a security/crypto decision -- stdlib random is correct here.
        candidate_id = random.randint(1, max_id)  # noqa: S311
        quote = await _get_quote(candidate_id)
        if quote is not None:
            return quote
    return None


async def _delete_quote(quote_id: int) -> bool:
    """Hard-delete one quote by id (see module docstring: `kv` has no tombstone query surface).

    `False` if no such quote exists.
    """
    existing = await _get_quote(quote_id)
    if existing is None:
        return False
    await kv.delete(_quote_key(quote_id))
    return True


def _format_quote(quote: dict[str, Any]) -> str:
    """Render one quote blob as reply text."""
    return f'quote #{quote.get("id")}: "{quote.get("text", "")}"'
