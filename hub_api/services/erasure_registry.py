"""Declarative Art. 17 erasure registry: every user-keyed table, and what erasure does to it.

`services/data_privacy_service.py::_sync_erase()` used to hand-list ~16 tables, so every table a
later migration added with a `user_id` / `hub_user_id` / `*_user_id` column silently survived a
"completed" erasure. This module is the single, reviewable answer to "what happens to table T
when a user is erased": each user-keyed table has exactly the rules below, the executor
(`services/erasure_executor.py`) runs them inside the one erasure transaction, and
`tests/test_erasure_registry_coverage.py` scans the migration chain and FAILS when a table or a
user-keyed column exists that no rule accounts for -- a new table cannot regress erasure.

**The model (suppression-only, unchanged).** A benign user is erased completely. A user with an
active ban keeps exactly one thing -- the salted one-way `suppression_records` code written by
`services/ban_suppression.py` BEFORE any delete -- and is otherwise erased identically. Nothing
here branches on ban state: the registry deletes the same rows either way.

Actions (`Action`):

* `DELETE`  -- the row IS the subject's data (their activity, content, tokens, balances): deleted.
* `SCRUB`   -- the row belongs to someone/something else (a booking's host, a quote's adder) but
  carries the subject's raw PII (name, email, handle, IP): those columns are nulled/blanked and the
  row stays. The id column stays too -- see `REF`.
* `REF`     -- a pure pseudonymous reference (`created_by`, `approved_by`, `updated_by`, ...) on a
  row the tenant/community owns. After erasure the `hub_users` row it points at is anonymized in
  place and every identity link is deleted, so the integer identifies nobody; nulling it would
  also destroy organisational records (approvals, audit attribution) and re-fire the active-set
  change triggers on bundle tables. Retained by design, and listed here so it is a decision.
* `CORE`    -- already erased by the hand-written steps in `_sync_erase()` (profiles, sessions,
  memberships, reputation, identities, the `hub_users` anonymization). A test pins that each CORE
  table really is touched there.
* `RETAIN`  -- deliberately kept: Art. 5(2) accountability / Art. 17(3)(e) legal-claims evidence
  (consent, audit trails, the deletion ledger, contract acceptance).
* `EXEMPT`  -- the column only LOOKS user-keyed (an enum, an org display name, a keyed hash).
* `REVIEW`  -- not erased, and why. Each is a known gap surfaced for a human decision rather than
  guessed at; `tests/test_erasure_registry_coverage.py` pins the exact set so it cannot grow
  silently, and `docs/PRIVACY.md` lists them.

Statutory-right rule (critical-rules.md): erasure is available in EVERY tier. Nothing in this
module or the executor consults a tier, a license or a feature flag.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

#: Written into NOT NULL text columns that must stop identifying the subject but cannot be NULL.
ERASED_PLACEHOLDER = "[erased]"


class Key(StrEnum):
    """How a column identifies the erased subject."""

    #: integer column equal to the subject's `hub_users.id`.
    HUB_USER = "hub_user"
    #: column that is one of the subject's PII-free uuid tokens (`hub_users.uuid`, member /
    #: pseudonym uuids) -- the key bundle economy/reputation/KV state uses.
    USER_UUID = "user_uuid"
    #: `(platform column, id column)` equal to one of the subject's linked platform identities.
    #: Matched as a PAIR, never the id alone -- a bare id can collide across platforms.
    PLATFORM_ID = "platform_id"
    #: integer column equal to the id of one of the subject's `community_members` rows.
    MEMBER = "member"


class Action(StrEnum):
    """What erasure does to a table (see the module docstring)."""

    DELETE = "delete"
    SCRUB = "scrub"
    REF = "ref"
    CORE = "core"
    RETAIN = "retain"
    EXEMPT = "exempt"
    REVIEW = "review"


@dataclass(slots=True, frozen=True)
class Match:
    """One way a row can belong to the subject: `column` holds the subject's `key`."""

    column: str
    key: Key
    platform_column: str | None = None


@dataclass(slots=True, frozen=True)
class Detach:
    """NULL `column` on `table` where it points at a row about to be deleted (FK `NO ACTION`)."""

    table: str
    column: str


@dataclass(slots=True, frozen=True)
class Rule:
    """The erasure treatment of one table (a table may carry several rules)."""

    table: str
    action: Action
    matches: tuple[Match, ...] = ()
    set_null: tuple[str, ...] = ()
    set_placeholder: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    detach: tuple[Detach, ...] = ()
    reason: str = ""


@dataclass(slots=True, frozen=True)
class ObjectSource:
    """A column holding the URL of an uploaded object (avatar/banner) owned by the subject."""

    table: str
    url_column: str
    match: Match


def hub(column: str) -> Match:
    """`column` is the subject's integer hub user id."""
    return Match(column, Key.HUB_USER)


def uuid(column: str) -> Match:
    """`column` is one of the subject's uuid tokens."""
    return Match(column, Key.USER_UUID)


def platform(platform_column: str, id_column: str) -> Match:
    """`(platform_column, id_column)` is one of the subject's linked platform identities."""
    return Match(id_column, Key.PLATFORM_ID, platform_column)


def member(column: str) -> Match:
    """`column` is the id of one of the subject's `community_members` rows."""
    return Match(column, Key.MEMBER)


def delete(table: str, *matches: Match, detach: tuple[Detach, ...] = ()) -> Rule:
    """DELETE every row of `table` that matches any of `matches`."""
    return Rule(table, Action.DELETE, matches=matches, detach=detach)


def scrub(
    table: str,
    match: Match,
    *,
    set_null: tuple[str, ...] = (),
    set_placeholder: tuple[str, ...] = (),
) -> Rule:
    """Keep rows matching `match`, but blank the subject's PII columns on them."""
    return Rule(
        table, Action.SCRUB, matches=(match,), set_null=set_null, set_placeholder=set_placeholder
    )


def ref(table: str, *columns: str, reason: str = "") -> Rule:
    """`columns` are pseudonymous references to the (anonymized) subject -- retained."""
    return Rule(table, Action.REF, columns=columns, reason=reason)


def core(table: str) -> Rule:
    """The whole table is erased by the hand-written core steps in `_sync_erase()`."""
    return Rule(table, Action.CORE)


def retain(table: str, reason: str) -> Rule:
    """The whole table is deliberately retained."""
    return Rule(table, Action.RETAIN, reason=reason)


def exempt(table: str, *columns: str, reason: str) -> Rule:
    """`columns` look user-keyed but are not."""
    return Rule(table, Action.EXEMPT, columns=columns, reason=reason)


def review(table: str, *columns: str, reason: str) -> Rule:
    """`columns` hold (or may hold) the subject's data but are NOT erased -- a known gap."""
    return Rule(table, Action.REVIEW, columns=columns, reason=reason)


_ACTOR = "pseudonymous actor reference; the hub_users row is anonymized in place"
_ORG_NAME = "organisation/product display name, not a person"
_AUDIT = (
    "append-only accountability trail (GDPR Art. 5(2)); pseudonymous once hub_users is anonymized"
)

_PLATFORM_SEARCH_TABLES = (
    "build_advisor_searches",
    "clip_researcher_searches",
    "event_lookup_searches",
    "patch_notes_searches",
    "price_tracker_searches",
    "tech_troubleshooter_searches",
    "game_lookup_searches",
)
_PLATFORM_KEYED_TABLES = (
    "community_welcomed_users",
    "loyalty_balances",
    "loyalty_simple_game_cooldowns",
    "user_platform_context",
    "calendar_rsvps",
)

#: The registry. Order matters only for readability -- every statement runs in one transaction and
#: the one FK-ordering hazard (`calendar_tickets` self reference) is handled by `detach`.
RULES: tuple[Rule, ...] = (
    # --- handled by the hand-written core steps in `_sync_erase()` -------------------------------
    core("hub_users"),
    core("hub_user_profiles"),
    core("hub_user_identities"),
    core("hub_sessions"),
    core("hub_temp_passwords"),
    core("user_passkeys"),
    core("hub_chat_messages"),
    core("activity_message_events"),
    core("activity_watch_sessions"),
    core("community_members"),
    core("reputation_events"),
    core("reputation_tenant"),
    core("bundle_reputation_scores"),
    core("bundle_reputation_adjustments"),
    core("ephemeral_pseudonyms"),
    # --- retained: Art. 5(2) / 17(3)(e) evidence -------------------------------------------------
    retain("cookie_consent", "consent record (Art. 5(2)); ERASURE_RETAINED_TABLES"),
    retain("cookie_audit_log", "consent change log (Art. 5(2)); ERASURE_RETAINED_TABLES"),
    retain("audit_log", "platform audit trail (Art. 5(2)); ERASURE_RETAINED_TABLES"),
    retain("data_deletion_requests", "proof of erasure (Art. 5(2)); ERASURE_RETAINED_TABLES"),
    retain("audit_events", "hash-chained append-only audit log; a trigger rejects UPDATE/DELETE"),
    retain("workflow_audit_log", _AUDIT),
    retain("module_scope_audit", _AUDIT),
    retain("instance_permission_policy_audit", _AUDIT),
    retain(
        "marketplace_tc_acceptance",
        "terms-of-service acceptance evidence (Art. 17(3)(e) legal claims), like cookie_consent",
    ),
    # --- subject-owned rows: DELETE ---------------------------------------------------------------
    delete("activity_logs", hub("user_id")),
    delete("activity_stats_daily", hub("hub_user_id")),
    # The live feed row holds the raw handle (`actor`) + message text; the platform-native id
    # (`actor_platform_user_id`, migration 0062) is the erasure key. Rows written before 0062 have
    # a NULL id and age out via the retention window (docs/compliance/data-retention.md).
    Rule(
        "live_activity_events",
        Action.DELETE,
        matches=(platform("platform", "actor_platform_user_id"),),
        columns=("actor",),
    ),
    delete("analytics_suspected_bots", platform("platform", "platform_user_id")),
    delete("auto_shoutout_creators", platform("platform", "platform_user_id")),
    delete("booking_page_members", hub("hub_user_id")),
    delete("booking_pages", hub("hub_user_id")),
    delete("bookings", hub("host_user_id")),
    delete("bot_detection_results", hub("user_id")),
    *(delete(table, platform("platform", "user_id")) for table in _PLATFORM_SEARCH_TABLES),
    *(delete(table, platform("platform", "platform_user_id")) for table in _PLATFORM_KEYED_TABLES),
    delete("calendar_event_admins", hub("hub_user_id"), platform("platform", "platform_user_id")),
    delete("calendar_free_busy", hub("hub_user_id")),
    delete("calendar_ticket_check_ins", hub("holder_hub_user_id")),
    delete(
        "calendar_tickets",
        hub("hub_user_id"),
        platform("platform", "platform_user_id"),
        detach=(Detach("calendar_tickets", "transferred_from_ticket_id"),),
    ),
    delete("calendar_tournament_participants", platform("platform", "user_id")),
    delete("call_annotations", hub("created_by")),
    delete("call_raised_hands", hub("user_id")),
    delete("caption_events", uuid("user_ref")),
    delete("community_calendar_subscriptions", hub("hub_user_id")),
    delete("community_call_participants", hub("user_id")),
    delete("community_call_rooms", hub("created_by")),
    delete("community_join_requests", hub("user_id")),
    delete("connected_calendars", hub("hub_user_id")),
    delete("economy_balances", uuid("user_uuid")),
    delete("economy_ledger", uuid("user_uuid")),
    delete("form_submissions", hub("user_id")),
    delete("golden_ticket_holders", hub("user_id")),
    delete("hub_forum_posts", hub("author_hub_user_id")),
    delete("hub_forum_replies", hub("author_hub_user_id")),
    delete("hub_module_reviews", hub("user_id")),
    delete("hub_oauth_states", hub("user_id")),
    delete("identity_resolution_events", member("member_id")),
    delete("inventory_checkouts", hub("user_id")),
    delete("inventory_log", hub("user_id")),
    delete("lfg_joins", platform("platform", "user_id")),
    delete("lfg_posts", platform("platform", "user_id")),
    delete("loyalty_giveaway_winners", platform("platform", "user_id")),
    delete("loyalty_redemptions", platform("platform", "platform_user_id")),
    delete(
        "loyalty_simple_game_results",
        hub("hub_user_id"),
        platform("platform", "platform_user_id"),
    ),
    delete("loyalty_transactions", platform("platform", "platform_user_id")),
    delete("marketplace_reviews", hub("user_id")),
    delete("memories_bookmarks", hub("created_by_user_id")),
    delete("memories_quote_votes", hub("user_id")),
    delete("memories_quotes", hub("author_user_id")),
    delete("memories_reminders", hub("user_id")),
    delete("music_playlists", hub("created_by_user_id")),
    delete("music_song_request_votes", hub("user_id")),
    delete("one_time_secrets", uuid("target_user_uuid")),
    delete("platform_admins", hub("user_id")),
    delete("player_game_inventory", hub("user_id")),
    delete("player_game_loadouts", hub("user_id")),
    delete("poll_votes", hub("user_id")),
    delete("presence_events_log", hub("hub_user_id")),
    delete("pvp_match_history", hub("player1_id"), hub("player2_id")),
    delete("quotes", hub("quoted_user_id")),
    delete("shoutout_history", platform("platform", "target_user_id")),
    delete("sso_identities", hub("hub_user_id")),
    delete("support_tickets", hub("reporter_user_id")),
    delete("support_ticket_comments", hub("author_user_id")),
    delete("tenant_admins", hub("user_id")),
    delete("user_access_tokens", hub("user_id")),
    delete("user_calendar_settings", hub("hub_user_id")),
    delete("user_labels", hub("user_id")),
    delete("user_presence_settings", hub("hub_user_id")),
    delete("vendor_role_requests", hub("user_id")),
    delete("video_shoutout_history", platform("target_platform", "target_user_id")),
    # --- rows kept for others, subject's PII blanked: SCRUB ---------------------------------------
    scrub(
        "bookings",
        hub("guest_user_id"),
        set_null=("guest_email", "guest_notes"),
        set_placeholder=("guest_name",),
    ),
    scrub("announcements", hub("created_by"), set_null=("created_by_name",)),
    scrub("calendar_event_admins", hub("assigned_by"), set_null=("assigned_by_username",)),
    scrub(
        "calendar_ticket_check_ins",
        hub("operator_user_id"),
        set_null=("operator_username", "ip_address"),
    ),
    scrub("economy_ledger", uuid("counterparty_uuid"), set_null=("counterparty_uuid",)),
    scrub(
        "loyalty_transactions",
        platform("platform", "actor_platform_user_id"),
        set_null=("actor_platform_user_id",),
    ),
    scrub(
        "marketplace_sellers",
        hub("user_id"),
        set_null=(
            "display_name",
            "description",
            "website_url",
            "payout_method",
            "payout_account_id",
        ),
    ),
    scrub("memories_quotes", hub("created_by_user_id"), set_placeholder=("created_by_username",)),
    scrub("music_playback_history", hub("played_by_user_id"), set_null=("played_by_username",)),
    scrub(
        "music_song_requests",
        hub("requested_by_user_id"),
        set_placeholder=("requested_by_username",),
    ),
    # --- pseudonymous references on organisation-owned rows: REF ----------------------------------
    ref("ai_byok_keys", "created_by_user_id", reason=_ACTOR),
    ref("ai_knowledge_sources", "vendor_id", reason=_ACTOR),
    ref("ai_model_config", "updated_by_user_id", reason=_ACTOR),
    ref("ai_token_transactions", "actor_user_id", reason=_ACTOR),
    ref("analytics_suspected_bots", "reviewed_by", reason=_ACTOR),
    ref("announcements", "updated_by", reason=_ACTOR),
    ref("app_activations", "activated_by", reason=_ACTOR),
    ref(
        "app_active_versions",
        "activated_by",
        reason=_ACTOR + "; any write logs an active-set change",
    ),
    ref("app_global_installs", "installed_by", "revoked_by", reason=_ACTOR),
    ref(
        "app_install_approvals",
        "approved_by",
        reason=_ACTOR + "; any write logs an active-set change",
    ),
    ref("app_permission_requests", "approved_by", reason=_ACTOR),
    ref("app_stream_grants", "granted_by", reason=_ACTOR),
    ref("app_tenant_permission_restrictions", "restricted_by", reason=_ACTOR),
    ref("app_version_uploads", "requested_by", reason=_ACTOR),
    ref("auto_shoutout_creators", "added_by", reason=_ACTOR),
    ref("bot_detection_results", "reviewed_by", reason=_ACTOR),
    ref("bundle_tenant_availability", "updated_by", reason=_ACTOR),
    ref("calendar_event_admins", "revoked_by", reason=_ACTOR),
    ref("calendar_tickets", "cancelled_by", "checked_in_by", "transferred_by", reason=_ACTOR),
    ref("call_raised_hands", "acknowledged_by", reason=_ACTOR),
    ref("command_aliases", "created_by", reason=_ACTOR),
    ref("community_access_tokens", "created_by_user_id", reason=_ACTOR),
    ref(
        "community_connection_access", "approved_by_user_id", "requested_by_user_id", reason=_ACTOR
    ),
    ref("community_forms", "created_by", reason=_ACTOR + "; NOT NULL"),
    ref("community_join_requests", "reviewed_by", reason=_ACTOR),
    ref("community_permission_grants", "granted_by", "revoked_by", reason=_ACTOR),
    ref("community_polls", "created_by", reason=_ACTOR),
    ref("community_radio_stations", "created_by", reason=_ACTOR),
    ref("community_servers", "added_by", "approved_by", reason=_ACTOR),
    ref("community_vendor_installations", "installed_by", reason=_ACTOR),
    ref("cookie_policy_versions", "created_by", reason=_ACTOR),
    ref("github_repo_connections", "vendor_id", reason=_ACTOR),
    ref("guild_tenant_pairings", "created_by_user_id", reason=_ACTOR),
    ref("hub_channels", "created_by", reason=_ACTOR),
    ref("hub_module_installations", "installed_by", reason=_ACTOR),
    ref("hub_settings", "updated_by", reason=_ACTOR),
    ref("instance_permission_policies", "set_by", reason=_ACTOR),
    ref("loyalty_redemptions", "fulfilled_by", reason=_ACTOR),
    ref("marketplace_modules", "approved_by", "developer_user_id", reason=_ACTOR),
    ref(
        "marketplace_premium_offerings",
        "premium_user_id",
        reason=_ACTOR + "; offerings have subscriptions (FK NO ACTION), so the row must stay",
    ),
    ref("marketplace_sellers", "user_id", reason=_ACTOR + "; seller owns modules (FK NO ACTION)"),
    ref("marketplace_settings", "updated_by", reason=_ACTOR),
    ref("marketplace_submissions", "reviewed_by", "submitted_by", reason=_ACTOR),
    ref("mirror_groups", "created_by", reason=_ACTOR),
    ref("module_db_accounts", "created_by_user_id", reason=_ACTOR),
    ref("module_installations", "disabled_by", "installed_by", reason=_ACTOR),
    ref("music_moderation_log", "actor_user_id", reason=_ACTOR),
    ref("music_policy", "updated_by", reason=_ACTOR),
    ref("music_queue", "requested_by_user_id", reason=_ACTOR),
    ref("music_station_queue", "requested_by", reason=_ACTOR),
    ref("platform_configs", "updated_by", reason=_ACTOR),
    ref("platform_connections", "installed_by_user_id", reason=_ACTOR),
    ref("platform_integrations", "created_by_user_id", "updated_by_user_id", reason=_ACTOR),
    ref("platform_settings", "updated_by", reason=_ACTOR),
    ref("pvp_match_history", "winner_id", reason="always one of player1_id/player2_id"),
    ref("quotes", "added_by_user_id", reason=_ACTOR),
    ref("rcon_command_log", "user_id", reason=_ACTOR + "; command log, no PII column"),
    ref("scope_grants", "granted_by_user_id", reason=_ACTOR),
    ref("server_link_requests", "requested_by", "reviewed_by", reason=_ACTOR),
    ref("server_status_configs", "added_by", reason=_ACTOR),
    ref("shoutout_creators", "added_by", reason=_ACTOR),
    ref("sso_connections", "created_by_user_id", reason=_ACTOR),
    ref("support_tickets", "assignee_user_id", reason=_ACTOR),
    ref("tenant_platform_apps", "installed_by_user_id", reason=_ACTOR),
    ref("user_labels", "created_by", reason=_ACTOR),
    ref("vendor_discount_codes", "vendor_id", reason=_ACTOR + "; NOT NULL, redeemed codes"),
    ref("vendor_module_reviews", "reviewer_id", reason=_ACTOR),
    ref("vendor_role_requests", "reviewed_by", reason=_ACTOR),
    ref("vendor_submission_reviews", "reviewer_id", reason=_ACTOR + "; NOT NULL"),
    ref("vendor_submission_status_log", "changed_by", reason=_ACTOR),
    ref("vendor_submissions", "reviewed_by", reason=_ACTOR),
    ref("workflow_executions", "user_id", reason=_ACTOR),
    ref("workflow_permissions", "granted_by", reason=_ACTOR),
    ref("workflow_templates", "created_by", reason=_ACTOR),
    ref("workflows", "created_by", "updated_by", reason=_ACTOR),
    # --- look user-keyed, are not -----------------------------------------------------------------
    exempt("app_global_installs", "superseded_by", reason="id of the superseding install row"),
    exempt("app_install_approvals", "superseded_by", reason="id of the superseding approval row"),
    exempt(
        "calendar_tournament_matches", "winner_id", reason="calendar_tournament_participants.id"
    ),
    exempt("community_polls", "created_by_hash", reason="keyed one-way hash, not an identifier"),
    exempt("community_roles", "display_name", reason=_ORG_NAME),
    exempt("communities", "display_name", reason=_ORG_NAME),
    exempt("hub_modules", "display_name", reason=_ORG_NAME),
    exempt("module_db_accounts", "db_username", reason="database role name"),
    exempt("modules", "display_name", reason=_ORG_NAME),
    exempt(
        "overlay_access_log",
        "ip_address",
        reason="anonymous overlay viewer log (overlay key + IP), no user id",
    ),
    exempt("permission_scopes", "display_name", reason=_ORG_NAME),
    exempt("server_link_requests", "initiated_by", reason="enum: community | platform"),
    exempt("server_status_configs", "display_name", reason=_ORG_NAME),
    exempt("sso_connections", "display_name", reason=_ORG_NAME),
    exempt("support_tickets", "subject", reason="ticket subject line, not an IdP subject"),
    exempt("tenants", "display_name", reason=_ORG_NAME),
    # --- known gaps, surfaced for a human decision ------------------------------------------------
    review(
        "platform_integrations",
        "user_id",
        reason="user-level integration tokens; hub_api's DB role is DENIED this table "
        "(config/postgres/service-roles.yaml deny_tables) -- needs a SECURITY DEFINER erase "
        "function or a credential-service call",
    ),
    review(
        "activity_stats_daily",
        "platform_user_id",
        "platform_username",
        reason="rows keyed only by platform id (no platform column): hub-linked rows are "
        "deleted, unlinked ones cannot be matched without risking a cross-platform collision",
    ),
    review(
        "hub_admins",
        "email",
        "username",
        reason="legacy admin table with its own credentials, not keyed to hub_users",
    ),
    review(
        "loyalty_giveaway_keys",
        "claimed_by_user_id",
        reason="varchar id with no platform column; the winner row (platform + id) is deleted",
    ),
    review(
        "communities",
        "created_by",
        "deleted_by",
        "owner_id",
        "owner_name",
        reason="free-form varchar set by superadmin, not a hub_users reference",
    ),
    review(
        "command_aliases",
        "deleted_by",
        reason="varchar of unknown shape (hub id, username or system)",
    ),
    review(
        "clip_bookmarks",
        "bookmarked_by",
        reason="varchar of unknown shape (platform handle or id), no platform column",
    ),
    review(
        "clip_highlight_reels",
        "created_by",
        reason="varchar of unknown shape (platform handle or id), no platform column",
    ),
    review(
        "module_scopes",
        "granted_by_user_id",
        "revoked_by_user_id",
        reason="varchar of unknown shape; audited by trigger into module_scope_audit",
    ),
    review(
        "revoked_tokens",
        "revoked_by_user_id",
        reason="varchar of unknown shape; security record of a revoked module token",
    ),
    review(
        "server_link_requests",
        "initiator_platform_user_id",
        "initiator_platform_username",
        reason="platform id/handle of the requester with no platform column on the row",
    ),
    review(
        "shoutout_creators",
        "platform_username",
        reason="creator handle with no id column to match on",
    ),
    review(
        "video_shoutout_history",
        "triggered_by_user_id",
        "triggered_by_username",
        reason="platform id/handle of the trigger user with no platform column for the actor",
    ),
    review(
        "vendor_submissions",
        "vendor_email",
        "support_email",
        reason="company contact emails entered on a submission, not linked to the submitting user",
    ),
)

#: Integer columns matched as `hub_users.id` although the schema declares no FK to it (legacy
#: module tables whose code only ever writes the hub id). If one of them ever held something else,
#: a row of a different user with the same integer would be erased -- so each is listed here, for
#: the doc and for review, instead of being an unremarked guess.
ASSUMED_HUB_ID_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("bot_detection_results", "user_id"),
        ("calendar_ticket_check_ins", "holder_hub_user_id"),
        ("hub_module_reviews", "user_id"),
        ("memories_bookmarks", "created_by_user_id"),
        ("memories_quote_votes", "user_id"),
        ("memories_quotes", "author_user_id"),
        ("memories_quotes", "created_by_user_id"),
        ("memories_reminders", "user_id"),
        ("music_playback_history", "played_by_user_id"),
        ("music_playlists", "created_by_user_id"),
        ("music_song_request_votes", "user_id"),
        ("music_song_requests", "requested_by_user_id"),
        ("platform_admins", "user_id"),
        ("support_ticket_comments", "author_user_id"),
        ("support_tickets", "reporter_user_id"),
    }
)

#: Uploaded-object (SeaweedFS/S3) URLs to delete, read BEFORE the rows that hold them are erased.
#: Only URLs under this deployment's own public base are ever deleted (platform CDN avatars are
#: not ours to delete) -- the filter lives in `services/storage_service.py`.
OBJECT_SOURCES: tuple[ObjectSource, ...] = (
    ObjectSource("hub_users", "avatar_url", Match("id", Key.HUB_USER)),
    ObjectSource("hub_user_profiles", "custom_avatar_url", hub("hub_user_id")),
    ObjectSource("hub_user_profiles", "banner_url", hub("hub_user_id")),
    ObjectSource("hub_user_identities", "avatar_url", hub("hub_user_id")),
    ObjectSource("hub_sessions", "avatar_url", hub("user_id")),
    ObjectSource("community_members", "avatar_url", Match("id", Key.MEMBER)),
    ObjectSource("hub_chat_messages", "sender_avatar_url", hub("sender_hub_user_id")),
    ObjectSource("hub_forum_posts", "author_avatar_url", hub("author_hub_user_id")),
    ObjectSource("hub_forum_replies", "author_avatar_url", hub("author_hub_user_id")),
)

#: Valkey/Redis namespace of the bundle `kv` capability (`core/bundle_host_kv/src/scope.rs`):
#: `bundlekv:{tenant}:{community|_tenant}:{app_id}:data:{guest_key}`. A bundle that keeps per-user
#: state puts the subject's uuid token in `guest_key`.
KV_DATA_GLOB_PREFIX = "bundlekv:*:data:"
KV_DATA_SEGMENT = ":data:"
KV_COUNT_SUFFIX = ":count"

#: Columns that name a person/actor (`KEY`) or carry raw PII text about one (`DESCRIPTOR`). The
#: coverage test flags every migrated column matching these and demands a rule for it.
KEY_COLUMN = re.compile(
    r"^(user_id|hub_user_id|user_uuid|member_id|user_ref|platform_user_id|owner_id|vendor_id"
    r"|reviewer_id|winner_id|actor|actor_uuid|bookmarked_by)$"
    r"|_user_id$|_user_uuid$|_hub_user_id$|_platform_user_id$|_by$|^player[0-9]_id$"
)
DESCRIPTOR_COLUMN = re.compile(
    r"(^|_)username$|(^|_)email$|(^|_)display_name$|(^|_)avatar_url$|^ip_address$|^subject$"
    r"|^(author|owner|reporter|holder|guest|assigned_by|created_by)_name$|^user_display_name$"
)


def is_key_column(column: str) -> bool:
    """Does `column` (lowercase) look like a reference to a person?"""
    return bool(KEY_COLUMN.search(column))


def is_descriptor_column(column: str) -> bool:
    """Does `column` (lowercase) look like raw PII text about a person?"""
    return bool(DESCRIPTOR_COLUMN.search(column))


def rules_for(table: str) -> tuple[Rule, ...]:
    """Every rule that applies to `table`."""
    return tuple(rule for rule in RULES if rule.table == table)


def executable_rules() -> Iterator[Rule]:
    """The rules the executor runs: DELETE and SCRUB, in declaration order."""
    for rule in RULES:
        if rule.action in (Action.DELETE, Action.SCRUB):
            yield rule


def registry_tables() -> frozenset[str]:
    """Every table the registry classifies."""
    return frozenset(rule.table for rule in RULES)


def executed_tables() -> frozenset[str]:
    """Every table the executor issues a DELETE/UPDATE against."""
    return frozenset(rule.table for rule in executable_rules())


def columns_in_rule(rule: Rule) -> frozenset[str]:
    """Every column a rule reads or writes (for the executor's column-existence check)."""
    cols: set[str] = set(rule.columns) | set(rule.set_null) | set(rule.set_placeholder)
    for match in rule.matches:
        cols.add(match.column)
        if match.platform_column:
            cols.add(match.platform_column)
    for det in rule.detach:
        cols.add(det.column)
    return frozenset(cols)


def covered_columns(table: str) -> frozenset[str] | None:
    """The flagged columns of `table` that the registry accounts for; `None` means ALL of them.

    A `CORE`/`RETAIN` rule accounts for the whole table. A `DELETE` rule also accounts for the
    table's descriptor columns (they describe the very row being deleted) -- but NOT for an
    unmatched key column, which may reference the subject on someone else's row and needs its own
    `REF`/`SCRUB`/`REVIEW`/`EXEMPT` entry.
    """
    covered: set[str] = set()
    for rule in rules_for(table):
        if rule.action in (Action.CORE, Action.RETAIN):
            return None
        covered |= columns_in_rule(rule)
    return frozenset(covered)


def has_delete_rule(table: str) -> bool:
    """Does `table` have a DELETE rule (so its descriptor columns die with the row)?"""
    return any(rule.action is Action.DELETE for rule in rules_for(table))
