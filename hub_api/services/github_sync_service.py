"""Business logic ported from `githubSyncService.js` -- bidirectional GitHub Issues sync.

AES-256-GCM token-at-rest encryption (`cryptography.hazmat.primitives.
ciphers.aead.AESGCM`, stdlib-adjacent -- already a transitive dep via
`pyopenssl`, added directly to `requirements.in` by this port since it's
now a real runtime dependency, not just transitive), HMAC-SHA256
webhook signature verification (`hmac.compare_digest`, timing-safe,
matches Node's `crypto.timingSafeEqual`), GitHub REST API calls via
`httpx.AsyncClient`.

SECURITY FIXES (not faithful-port items, see each function's own
docstring for detail):
  1. `repo_owner`/`repo_name` are validated against GitHub's own naming
     rules at connection-create time -- Node's `createRepoConnection()`
     only checked truthiness, letting any string reach both the stored
     row and every future GitHub API path built from it
     (`_github_request()`). SSRF is NOT the applicable threat model
     here (`GITHUB_API_BASE` is a hardcoded constant, `https://
     api.github.com`, never derived from `repo_owner`/`repo_name` or
     any other caller input -- `hub_api/services/url_guard.py`'s
     DNS-rebind guard targets arbitrary-HOST fetches, e.g. the AI
     Knowledge crawler's user-supplied `source_url`; this client's host
     is fixed, so that guard doesn't fit the threat model and isn't
     imported here). The real risk closed is REST path/parameter
     injection into GitHub's own API surface -- unvalidated `repo_owner`/
     `repo_name` interpolated into `/repos/{owner}/{name}/...`.
  2. `create_repo_connection()` ignores any caller-supplied `vendor_id`/
     `community_id` in the request body, always deriving `community_id`
     from the ALREADY-`require_community_admin()`-verified URL path
     param -- Node's controller passed `req.body.vendor_id` straight
     through to the INSERT with no ownership check, so any admin of
     ANY community could plant a connection under an arbitrary
     `vendor_id` (a `hub_users.id` they don't own). No real route ever
     exercises a legitimate vendor-only (non-community) creation path
     today (`routes/githubSync.js` only registers the community-scoped
     `/:communityId/github-sync/connections` route) -- the `vendor_id`
     branch was unreachable dead code from any real endpoint, not a
     feature this fix removes.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.community_authz import ADMIN_SCOPES, get_caller_community_role
from services.errors import ApiError, bad_request, forbidden, not_found
from services.schema import bind_github_sync_tables

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"
_MAX_RETRY_COUNT = 3
_TIMEOUT_SECONDS = 10.0

#: GitHub username/org rules (owner) -- alnum + single hyphens, no
#: leading/trailing hyphen, <=39 chars.
_REPO_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
#: GitHub repo name rules -- alnum, hyphen, underscore, period, 1-100 chars.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_VALID_AUTH_TYPES = frozenset({"github_app", "pat"})
_VALID_SYNC_MODES = frozenset({"tickets_only", "tickets_and_discussions", "off"})


def _encryption_key() -> bytes:
    """Return the 32-byte AES-256 key from `GITHUB_SYNC_ENCRYPTION_KEY` (64 hex chars)."""
    raw = os.getenv("GITHUB_SYNC_ENCRYPTION_KEY")
    _key_error = (
        "GITHUB_SYNC_ENCRYPTION_KEY must be a 64-character hex string (32 bytes)",
        500,
        "INTERNAL_ERROR",
    )
    if not raw:
        raise ApiError(
            "GITHUB_SYNC_ENCRYPTION_KEY environment variable is not set", 500, "INTERNAL_ERROR"
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise ApiError(*_key_error) from exc
    if len(key) != 32:
        raise ApiError(*_key_error)
    return key


def encrypt_token(plaintext: str) -> str:
    """AES-256-GCM encrypt; base64 string of iv(12) + ciphertext+tag.

    Matches Node's `encryptToken()`.
    """
    key = _encryption_key()
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_and_tag = aesgcm.encrypt(iv, plaintext.encode(), None)
    return base64.b64encode(iv + ciphertext_and_tag).decode()


def decrypt_token(encrypted_b64: str) -> str:
    """Decrypt a token encrypted by `encrypt_token()`. Raises `ApiError` on tamper/corruption.

    BUG FIX (caught by this port's own test suite, not a Node behavior to
    preserve): the original version only caught `InvalidTag` (AEAD
    verification failure), letting a malformed/non-base64
    `encrypted_token` value (e.g. hand-edited DB row, migration mishap)
    raise a raw `binascii.Error`/`ValueError` straight out of this
    function -- every caller (`get_repo_connections()`'s per-row masking
    fallback, `sync_comment_to_github()`'s silent-return-on-decrypt-
    failure) already assumes `decrypt_token()` only ever raises
    `ApiError`, matching every other error path in this module.
    """
    key = _encryption_key()
    try:
        raw = base64.b64decode(encrypted_b64, validate=True)
        iv, ciphertext_and_tag = raw[:12], raw[12:]
        plaintext = AESGCM(key).decrypt(iv, ciphertext_and_tag, None)
    except (InvalidTag, ValueError, binascii.Error) as exc:
        raise ApiError("Failed to decrypt token", 500, "INTERNAL_ERROR") from exc
    return plaintext.decode()


def mask_token(token: str) -> str:
    """Show only the last 4 chars -- matches Node's `maskToken()`."""
    if not token or len(token) < 8:
        return "****"
    return f"****{token[-4:]}"


def generate_webhook_secret() -> str:
    """32 random bytes, hex-encoded -- matches Node's `generateWebhookSecret()`."""
    return os.urandom(32).hex()


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Timing-safe HMAC-SHA256 verification -- matches Node's `verifyWebhookSignature()`."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, "sha256").hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _validate_repo_coordinates(repo_owner: str, repo_name: str) -> None:
    """SECURITY FIX (1) -- see module docstring. Raises 400 on a malformed owner/name."""
    if not _REPO_OWNER_RE.match(repo_owner):
        raise bad_request("repo_owner is not a valid GitHub username/organization")
    if not _REPO_NAME_RE.match(repo_name):
        raise bad_request("repo_name is not a valid GitHub repository name")


async def _github_request(
    method: str, path: str, token: str, *, json_body: dict[str, Any] | None = None
) -> tuple[bool, int, Any]:
    """Authenticated GitHub REST call -- host is ALWAYS `_GITHUB_API_BASE`, never caller-derived."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "WaddleBot-GithubSync/1.0",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.request(
            method, f"{_GITHUB_API_BASE}{path}", headers=headers, json=json_body
        )
    try:
        data: Any = response.json()
    except ValueError:
        data = None
    return response.is_success, response.status_code, data


@dataclass(slots=True, frozen=True)
class RepoConnection:
    """Row-shaped view of `github_repo_connections`.

    Token is masked -- never the raw encrypted value.
    """

    id: int
    community_id: int | None
    vendor_id: int | None
    module_id: int | None
    repo_owner: str
    repo_name: str
    sync_mode: str
    default_labels: list[str]
    auto_close_on_github_close: bool
    auth_type: str
    webhook_secret: str
    installation_id: str | None
    is_active: bool
    token_masked: str


@dataclass(slots=True, frozen=True)
class SyncRecord:
    """Row-shaped view of a `ticket_github_sync` write result."""

    id: int
    ticket_id: int
    github_repo_connection_id: int
    github_issue_number: int
    github_issue_node_id: str | None
    sync_status: str
    last_error: str | None = None


async def _write_sync_log(
    async_dal: Any,
    dal: Any,
    *,
    ticket_github_sync_id: int | None,
    direction: str,
    event_type: str,
    payload: dict[str, Any],
    success: bool,
    error_message: str | None = None,
) -> None:
    """Best-effort audit row -- matches Node's `writeSyncLog()`, logs (never raises) on failure."""
    try:
        await async_dal.insert_async(
            dal.github_sync_log,
            ticket_github_sync_id=ticket_github_sync_id,
            direction=direction,
            event_type=event_type,
            payload=payload,
            success=success,
            error_message=error_message,
        )
    except Exception:  # noqa: BLE001 - logging must never break the caller's own flow
        logger.warning(
            "githubSync: failed to write sync log",
            extra={"event_type": event_type, "success": success},
        )


async def create_repo_connection(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    repo_owner: str,
    repo_name: str,
    auth_type: str,
    token: str,
    sync_mode: str = "tickets_only",
    default_labels: list[str] | None = None,
    auto_close_on_github_close: bool = True,
    installation_id: str | None = None,
) -> RepoConnection:
    """Port of `createRepoConnection()` -- SECURITY FIX (1)+(2), see module docstring."""
    bind_github_sync_tables(dal)
    if not repo_owner or not repo_name:
        raise bad_request("repo_owner and repo_name are required")
    _validate_repo_coordinates(repo_owner, repo_name)
    if auth_type not in _VALID_AUTH_TYPES:
        raise bad_request("auth_type must be 'github_app' or 'pat'")
    if not token:
        raise bad_request("token is required")
    if sync_mode not in _VALID_SYNC_MODES:
        raise bad_request(
            "sync_mode must be one of 'tickets_only', 'tickets_and_discussions', 'off'"
        )

    encrypted_token = encrypt_token(token)
    webhook_secret = generate_webhook_secret()
    labels = default_labels or ["waddles", "support"]

    new_id = await async_dal.insert_async(
        dal.github_repo_connections,
        community_id=community_id,
        vendor_id=None,  # SECURITY FIX (2) -- never trust a body-supplied vendor_id
        module_id=None,
        repo_owner=repo_owner,
        repo_name=repo_name,
        sync_mode=sync_mode,
        default_labels=labels,
        auto_close_on_github_close=auto_close_on_github_close,
        auth_type=auth_type,
        encrypted_token=encrypted_token,
        webhook_secret=webhook_secret,
        installation_id=installation_id,
        is_active=True,
    )
    rows = await async_dal.select_async(dal(dal.github_repo_connections.id == new_id))
    row = rows.first()
    return RepoConnection(
        id=row.id,
        community_id=row.community_id,
        vendor_id=row.vendor_id,
        module_id=row.module_id,
        repo_owner=row.repo_owner,
        repo_name=row.repo_name,
        sync_mode=row.sync_mode,
        default_labels=list(row.default_labels or []),
        auto_close_on_github_close=row.auto_close_on_github_close,
        auth_type=row.auth_type,
        webhook_secret=row.webhook_secret,
        installation_id=row.installation_id,
        is_active=row.is_active,
        token_masked=mask_token(token),
    )


async def get_repo_connections(
    async_dal: Any, dal: Any, *, community_id: int
) -> list[RepoConnection]:
    """Port of `getRepoConnections()`, scoped to ONE community (the URL-verified one).

    Node's original scoped by `vendor_id = userId OR community membership`
    across ALL communities the caller admins; this port narrows to the
    single `community_id` already proven by `require_community_admin()`
    on the route -- consistent with every route being mounted at
    `/<community_id>/github-sync/connections` (there is no cross-
    community list endpoint in the real contract to preserve).
    """
    bind_github_sync_tables(dal)
    rows = await async_dal.select_async(
        dal(
            (dal.github_repo_connections.community_id == community_id)
            & (dal.github_repo_connections.is_active == True)  # noqa: E712
        )
    )
    result: list[RepoConnection] = []
    for row in rows:
        try:
            plain = decrypt_token(row.encrypted_token)
            masked = mask_token(plain)
        except ApiError:
            masked = "****"
        result.append(
            RepoConnection(
                id=row.id,
                community_id=row.community_id,
                vendor_id=row.vendor_id,
                module_id=row.module_id,
                repo_owner=row.repo_owner,
                repo_name=row.repo_name,
                sync_mode=row.sync_mode,
                default_labels=list(row.default_labels or []),
                auto_close_on_github_close=row.auto_close_on_github_close,
                auth_type=row.auth_type,
                webhook_secret=row.webhook_secret,
                installation_id=row.installation_id,
                is_active=row.is_active,
                token_masked=masked,
            )
        )
    return result


async def delete_repo_connection(
    async_dal: Any, dal: Any, *, user_id: int, community_id: int, connection_id: int
) -> None:
    """Port of `deleteRepoConnection()`.

    Re-verifies ownership against the row's REAL `community_id`.
    Faithful to Node's own defense-in-depth: looks the connection up by
    id first and checks ITS `community_id`, rather than trusting the
    route's `community_id` path param alone -- catches a connection_id
    belonging to a different community than the one in the URL.
    """
    bind_github_sync_tables(dal)
    rows = await async_dal.select_async(
        dal(
            (dal.github_repo_connections.id == connection_id)
            & (dal.github_repo_connections.is_active == True)  # noqa: E712
        )
    )
    if not rows:
        raise not_found("Connection not found")
    conn = rows.first()
    if conn.community_id != community_id:
        raise forbidden("Forbidden")

    caller_role = await get_caller_community_role(
        async_dal, dal, community_id=community_id, user_id=user_id
    )
    if caller_role is None or not (caller_role.scopes & ADMIN_SCOPES):
        raise forbidden("Forbidden")

    await async_dal.update_async(dal.github_repo_connections.id == connection_id, is_active=False)


async def sync_ticket_to_github(
    async_dal: Any, dal: Any, *, ticket_id: int, repo_connection_id: int
) -> SyncRecord:
    """Port of `syncTicketToGithub()` -- pushes a support ticket to GitHub as a new issue."""
    bind_github_sync_tables(dal)
    ticket_rows = await async_dal.select_async(dal(dal.support_tickets.id == ticket_id))
    if not ticket_rows:
        raise not_found("Ticket not found")
    ticket = ticket_rows.first()

    conn_rows = await async_dal.select_async(
        dal(
            (dal.github_repo_connections.id == repo_connection_id)
            & (dal.github_repo_connections.is_active == True)  # noqa: E712
        )
    )
    if not conn_rows:
        raise not_found("Repo connection not found or inactive")
    conn = conn_rows.first()

    token = decrypt_token(conn.encrypted_token)
    issue_body = "\n".join(
        [
            ticket.description or "",
            "",
            "---",
            f"*Synced from [WaddleBot](https://waddles.app) support ticket #{ticket.id}*",
            f"Priority: {ticket.priority or 'normal'} | Status: {ticket.status}",
        ]
    )
    labels = list(conn.default_labels or ["waddles", "support"])
    ok, status, data = await _github_request(
        "POST",
        f"/repos/{conn.repo_owner}/{conn.repo_name}/issues",
        token,
        json_body={"title": ticket.subject, "body": issue_body, "labels": labels},
    )

    if not ok:
        new_id = await async_dal.insert_async(
            dal.ticket_github_sync,
            ticket_id=ticket_id,
            github_repo_connection_id=repo_connection_id,
            github_issue_number=0,
            sync_status="failed",
            last_error=f"GitHub API {status}: {data}",
        )
        await _write_sync_log(
            async_dal,
            dal,
            ticket_github_sync_id=new_id,
            direction="outbound",
            event_type="issue_created",
            payload={"ticketId": ticket_id, "repoConnectionId": repo_connection_id},
            success=False,
            error_message=f"GitHub API {status}: {data}",
        )
        raise ApiError(f"GitHub API error {status}", 502, "GITHUB_API_ERROR")

    raw_issue_number = data.get("number") if isinstance(data, dict) else None
    if not isinstance(raw_issue_number, int):
        # A 2xx GitHub response with no numeric `number` field is malformed --
        # never seen in practice, but `SyncRecord.github_issue_number: int`
        # must not silently accept `None`.
        raise ApiError("GitHub API returned an issue with no issue number", 502, "GITHUB_API_ERROR")
    issue_number: int = raw_issue_number
    issue_node_id = data.get("node_id") if isinstance(data, dict) else None
    new_id = await async_dal.insert_async(
        dal.ticket_github_sync,
        ticket_id=ticket_id,
        github_repo_connection_id=repo_connection_id,
        github_issue_number=issue_number,
        github_issue_node_id=issue_node_id,
        sync_status="synced",
    )
    await _write_sync_log(
        async_dal,
        dal,
        ticket_github_sync_id=new_id,
        direction="outbound",
        event_type="issue_created",
        payload={"ticketId": ticket_id, "issueNumber": issue_number},
        success=True,
    )
    return SyncRecord(
        id=new_id,
        ticket_id=ticket_id,
        github_repo_connection_id=repo_connection_id,
        github_issue_number=issue_number,
        github_issue_node_id=issue_node_id,
        sync_status="synced",
    )


async def sync_comment_to_github(
    async_dal: Any, dal: Any, *, ticket_id: int, comment_text: str, author_name: str
) -> None:
    """Port of `syncCommentToGithub()`.

    Best-effort -- matches Node's silent-return-on-miss behavior.
    """
    bind_github_sync_tables(dal)
    rows = await async_dal.select_async(
        dal(
            (dal.ticket_github_sync.ticket_id == ticket_id)
            & (dal.ticket_github_sync.sync_status != "failed")
        ),
        dal.ticket_github_sync.ALL,
        dal.github_repo_connections.ALL,
        left=dal.github_repo_connections.on(
            dal.ticket_github_sync.github_repo_connection_id == dal.github_repo_connections.id
        ),
    )
    match = next((r for r in rows if r.github_repo_connections.is_active), None)
    if match is None:
        return

    sync_row, conn_row = match.ticket_github_sync, match.github_repo_connections
    try:
        token = decrypt_token(conn_row.encrypted_token)
    except ApiError:
        return

    body = f"**{author_name}** (via WaddleBot):\n\n{comment_text}"
    ok, status, data = await _github_request(
        "POST",
        f"/repos/{conn_row.repo_owner}/{conn_row.repo_name}/issues/{sync_row.github_issue_number}/comments",
        token,
        json_body={"body": body},
    )
    await _write_sync_log(
        async_dal,
        dal,
        ticket_github_sync_id=sync_row.id,
        direction="outbound",
        event_type="comment_added",
        payload={"ticketId": ticket_id, "issueNumber": sync_row.github_issue_number},
        success=ok,
        error_message=None if ok else f"GitHub API {status}: {data}",
    )


async def handle_github_webhook(
    async_dal: Any,
    dal: Any,
    *,
    repo_owner: str,
    repo_name: str,
    event: str,
    payload: dict[str, Any],
    raw_body: bytes,
    signature: str | None,
) -> None:
    """Port of `handleGithubWebhook()`.

    HMAC-verified, no JWT/tenant auth (see blueprint docstring).
    """
    bind_github_sync_tables(dal)
    conn_rows = await async_dal.select_async(
        dal(
            (dal.github_repo_connections.repo_owner == repo_owner)
            & (dal.github_repo_connections.repo_name == repo_name)
            & (dal.github_repo_connections.is_active == True)  # noqa: E712
        )
    )
    if not conn_rows:
        return
    conn = conn_rows.first()

    if not verify_webhook_signature(raw_body, signature, conn.webhook_secret):
        raise ApiError("Invalid webhook signature", 401, "UNAUTHORIZED")

    issue = payload.get("issue") if isinstance(payload, dict) else None
    issue_number = issue.get("number") if isinstance(issue, dict) else None
    if issue_number is None:
        return

    sync_rows = await async_dal.select_async(
        dal(
            (dal.ticket_github_sync.github_repo_connection_id == conn.id)
            & (dal.ticket_github_sync.github_issue_number == issue_number)
        )
    )
    if not sync_rows:
        return
    sync_row = sync_rows.first()

    action = payload.get("action")
    if event == "issue_comment" and action == "created":
        comment = payload.get("comment") or {}
        comment_body = comment.get("body") or ""
        author = (comment.get("user") or {}).get("login") or "GitHub User"
        if "(via WaddleBot)" not in comment_body:
            await process_inbound_issue_comment(
                async_dal,
                dal,
                sync_id=sync_row.id,
                ticket_id=sync_row.ticket_id,
                comment_body=comment_body,
                author=author,
            )
        return

    if event == "issues" and action == "closed":
        await process_inbound_issue_close(
            async_dal, dal, sync_id=sync_row.id, ticket_id=sync_row.ticket_id
        )


async def process_inbound_issue_comment(
    async_dal: Any, dal: Any, *, sync_id: int, ticket_id: int, comment_body: str, author: str
) -> None:
    """Port of `processInboundIssueComment()`."""
    bind_github_sync_tables(dal)
    try:
        await async_dal.insert_async(
            dal.support_ticket_comments,
            ticket_id=ticket_id,
            content=comment_body,
            author_name=author,
            is_internal=False,
            source="github",
        )
        await _write_sync_log(
            async_dal,
            dal,
            ticket_github_sync_id=sync_id,
            direction="inbound",
            event_type="comment_received",
            payload={"author": author},
            success=True,
        )
    except Exception as exc:  # noqa: BLE001 - matches Node's own catch-log-continue
        await _write_sync_log(
            async_dal,
            dal,
            ticket_github_sync_id=sync_id,
            direction="inbound",
            event_type="comment_received",
            payload={"author": author},
            success=False,
            error_message=str(exc),
        )


async def process_inbound_issue_close(
    async_dal: Any, dal: Any, *, sync_id: int, ticket_id: int
) -> None:
    """Port of `processInboundIssueClose()`."""
    bind_github_sync_tables(dal)
    try:
        conn_rows = await async_dal.select_async(
            dal(dal.ticket_github_sync.id == sync_id),
            dal.ticket_github_sync.ALL,
            dal.github_repo_connections.ALL,
            left=dal.github_repo_connections.on(
                dal.ticket_github_sync.github_repo_connection_id == dal.github_repo_connections.id
            ),
        )
        conn_row = conn_rows.first().github_repo_connections if conn_rows else None
        if conn_row is None or not conn_row.auto_close_on_github_close:
            return

        still_open = ~dal.support_tickets.status.belongs(["closed", "resolved"])
        await async_dal.update_async(
            (dal.support_tickets.id == ticket_id) & still_open,
            status="closed",
        )
        await _write_sync_log(
            async_dal,
            dal,
            ticket_github_sync_id=sync_id,
            direction="inbound",
            event_type="issue_closed",
            payload={},
            success=True,
        )
    except Exception as exc:  # noqa: BLE001 - matches Node's own catch-log-continue
        await _write_sync_log(
            async_dal,
            dal,
            ticket_github_sync_id=sync_id,
            direction="inbound",
            event_type="issue_closed",
            payload={},
            success=False,
            error_message=str(exc),
        )


@dataclass(slots=True, frozen=True)
class RetryResult:
    """Return shape of `retry_failed_syncs()`."""

    retried: int
    succeeded: int
    failed: int
    errors: list[str] = field(default_factory=list)


async def retry_failed_syncs(async_dal: Any, dal: Any) -> RetryResult:
    """Port of `retryFailedSyncs()` -- exponential backoff retry for failed syncs.

    No `routes/githubSync.js` route calls this (cron-triggered in Node,
    "e.g. cron every 5 minutes" per its own docstring) -- ported as a
    callable service function for parity with `githubSyncService.js`'s
    exports, not wired to any blueprint route (none exists to match).
    """
    bind_github_sync_tables(dal)
    pending_rows = await async_dal.select_async(
        dal(
            (dal.ticket_github_sync.sync_status == "failed")
            & (dal.ticket_github_sync.retry_count < _MAX_RETRY_COUNT)
        ),
        dal.ticket_github_sync.ALL,
        dal.github_repo_connections.ALL,
        left=dal.github_repo_connections.on(
            dal.ticket_github_sync.github_repo_connection_id == dal.github_repo_connections.id
        ),
        orderby=dal.ticket_github_sync.created_at,
        limitby=(0, 50),
    )
    records = [r for r in pending_rows if r.github_repo_connections.is_active]

    succeeded, failed, errors = 0, 0, []
    for record in records:
        sync_row = record.ticket_github_sync
        backoff_minutes = 2**sync_row.retry_count
        created_at = sync_row.created_at
        if created_at is not None:
            # BUG FIX -- see `workflow_service._is_expired()`'s docstring for
            # the full rationale: pydal/sqlite round-trips a tz-aware
            # datetime back as naive-but-still-UTC-valued, so the naive
            # branch must compare against naive UTC, never naive LOCAL time.
            now = (
                datetime.now(UTC)
                if created_at.tzinfo is not None
                else datetime.now(UTC).replace(tzinfo=None)
            )
            elapsed_minutes = (now - created_at).total_seconds() / 60
            if elapsed_minutes < backoff_minutes:
                continue
        try:
            await sync_ticket_to_github(
                async_dal,
                dal,
                ticket_id=sync_row.ticket_id,
                repo_connection_id=sync_row.github_repo_connection_id,
            )
            succeeded += 1
        except ApiError as exc:
            failed += 1
            errors.append(exc.message)
            new_retry_count = sync_row.retry_count + 1
            await async_dal.update_async(
                dal.ticket_github_sync.id == sync_row.id,
                retry_count=new_retry_count,
                last_error=exc.message,
                sync_status="failed" if new_retry_count >= _MAX_RETRY_COUNT else "pending",
            )

    return RetryResult(retried=len(records), succeeded=succeeded, failed=failed, errors=errors)
