"""CalendarSyncEngine — orchestrates two-way sync between WaddleBot and external calendars.

The engine is provider-agnostic; it operates solely through the CalendarProviderBase
interface so the same logic applies to Google, Microsoft, and Apple.

Conflict resolution strategy: **remote wins**.  If the same event has been
modified both locally (in the WaddleBot DB) and remotely (in the provider)
since the last sync, the remote version is accepted and the local record is
overwritten.  The prior local version is logged for audit purposes.

Sync map
--------
A "sync map" entry records the relationship between a WaddleBot-internal event
UUID and a provider event ID together with the sync token at the time of last
sync.  The sync map is persisted in the database via the DAL and must implement
the following query interface:

    await dal.execute(query, params)  → list[dict]

The engine never commits or rolls back transactions itself; callers are
responsible for transaction management if required.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from libs.calendar_sync.base import CalendarProviderBase
from libs.calendar_sync.schema import normalize_event, denormalize_event

logger = logging.getLogger(__name__)


class CalendarSyncEngine:
    """Two-way calendar synchronisation engine.

    Args:
        provider: An authenticated CalendarProviderBase instance.
        dal: Database abstraction layer with an async execute(query, params) method.
    """

    def __init__(self, provider: CalendarProviderBase, dal: Any):
        self.provider = provider
        self.dal = dal
        self._provider_name = provider.PROVIDER

    # ──────────────────────────────────────────────────────────────────────
    # Public: full sync
    # ──────────────────────────────────────────────────────────────────────

    async def full_sync(
        self,
        user_id: str,
        calendar_id: str,
        community_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Perform a full sync from the provider calendar to WaddleBot.

        All events in the provider calendar are fetched, normalised, and
        upserted into the local database.  A new sync token is stored so
        subsequent calls can use incremental_sync.

        Args:
            user_id: WaddleBot user ID (for audit logging and sync map scoping).
            calendar_id: Provider-specific calendar identifier.
            community_id: Optional community to associate events with.

        Returns:
            Summary dict: {"pulled": int, "errors": int, "sync_token": str | None}
        """
        logger.info(
            f"[SYNC] Full sync started: provider={self._provider_name}, "
            f"calendar={calendar_id}, user={user_id}"
        )

        pulled = 0
        errors = 0
        page_token: Optional[str] = None

        while True:
            try:
                result = await self.provider.get_events(
                    calendar_id=calendar_id,
                    page_token=page_token,
                    max_results=250,
                )
            except Exception as exc:
                logger.error(f"[SYNC] get_events failed: {exc}")
                errors += 1
                break

            for raw_event in result.get("events", []):
                try:
                    canonical = normalize_event(raw_event, self._provider_name)
                    await self.pull_event_from_external(
                        canonical=canonical,
                        user_id=user_id,
                        calendar_id=calendar_id,
                        community_id=community_id,
                    )
                    pulled += 1
                except Exception as exc:
                    logger.error(f"[SYNC] pull_event_from_external failed: {exc}")
                    errors += 1

            page_token = result.get("next_page_token")
            if not page_token:
                # Store the sync token from the final page.
                sync_token = result.get("next_sync_token")
                await self._update_sync_map(
                    user_id=user_id,
                    calendar_id=calendar_id,
                    waddlebot_event_id=None,
                    provider_event_id=None,
                    sync_token=sync_token,
                    is_calendar_level=True,
                )
                logger.info(
                    f"[SYNC] Full sync complete: pulled={pulled}, errors={errors}, "
                    f"token={'yes' if sync_token else 'none'}"
                )
                return {"pulled": pulled, "errors": errors, "sync_token": sync_token}

    # ──────────────────────────────────────────────────────────────────────
    # Public: incremental sync
    # ──────────────────────────────────────────────────────────────────────

    async def incremental_sync(
        self,
        user_id: str,
        calendar_id: str,
        community_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Fetch only events changed since the last sync token and apply them.

        If no sync token is stored for this user+calendar combination the
        engine falls back to full_sync automatically.

        Args:
            user_id: WaddleBot user ID.
            calendar_id: Provider-specific calendar identifier.
            community_id: Optional community to associate events with.

        Returns:
            Summary dict: {"changed": int, "errors": int, "sync_token": str | None}
        """
        stored_token = await self._get_stored_sync_token(user_id, calendar_id)

        if not stored_token:
            logger.info(
                f"[SYNC] No stored token for calendar={calendar_id}; "
                f"falling back to full_sync."
            )
            result = await self.full_sync(user_id, calendar_id, community_id)
            return {
                "changed": result["pulled"],
                "errors": result["errors"],
                "sync_token": result["sync_token"],
            }

        logger.info(
            f"[SYNC] Incremental sync started: provider={self._provider_name}, "
            f"calendar={calendar_id}, user={user_id}"
        )

        try:
            changed_events, new_token = await self.provider.sync_changes(
                calendar_id=calendar_id,
                sync_token=stored_token,
            )
        except Exception as exc:
            logger.error(f"[SYNC] sync_changes failed: {exc}")
            # Stale token: fall back to full sync.
            logger.warning("[SYNC] Stale sync token detected; performing full sync.")
            result = await self.full_sync(user_id, calendar_id, community_id)
            return {
                "changed": result["pulled"],
                "errors": result["errors"],
                "sync_token": result["sync_token"],
            }

        changed = 0
        errors = 0

        for raw_event in changed_events:
            try:
                canonical = normalize_event(raw_event, self._provider_name)
                if canonical.get("status") == "cancelled":
                    # Remote deletion: mark local copy cancelled.
                    await self._apply_remote_deletion(
                        provider_event_id=canonical["provider_id"],
                        user_id=user_id,
                        calendar_id=calendar_id,
                    )
                else:
                    await self.pull_event_from_external(
                        canonical=canonical,
                        user_id=user_id,
                        calendar_id=calendar_id,
                        community_id=community_id,
                    )
                changed += 1
            except Exception as exc:
                logger.error(f"[SYNC] Incremental apply failed: {exc}")
                errors += 1

        # Persist the new sync token.
        if new_token:
            await self._update_sync_map(
                user_id=user_id,
                calendar_id=calendar_id,
                waddlebot_event_id=None,
                provider_event_id=None,
                sync_token=new_token,
                is_calendar_level=True,
            )

        logger.info(
            f"[SYNC] Incremental sync complete: changed={changed}, errors={errors}"
        )
        return {"changed": changed, "errors": errors, "sync_token": new_token}

    # ──────────────────────────────────────────────────────────────────────
    # Public: push / pull individual events
    # ──────────────────────────────────────────────────────────────────────

    async def push_event_to_external(
        self,
        waddlebot_event: Dict[str, Any],
        calendar_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Push a WaddleBot event to the external provider calendar.

        If the event already has a provider mapping, updates it; otherwise
        creates a new event and stores the mapping.

        Args:
            waddlebot_event: Canonical event dict from the WaddleBot DB.
            calendar_id: Provider calendar identifier to push to.
            user_id: WaddleBot user ID (for sync map lookup).

        Returns:
            Updated canonical event dict with provider_id set, or None on failure.
        """
        wb_event_id = str(waddlebot_event.get("id", ""))

        try:
            # Check if a provider mapping already exists.
            existing_provider_id = await self._get_provider_event_id(
                user_id=user_id,
                calendar_id=calendar_id,
                waddlebot_event_id=wb_event_id,
            )

            provider_payload = denormalize_event(waddlebot_event, self._provider_name)

            if existing_provider_id:
                result = await self.provider.update_event(
                    calendar_id=calendar_id,
                    event_id=existing_provider_id,
                    event=provider_payload,
                )
                logger.info(
                    f"[SYNC] Pushed UPDATE to {self._provider_name}: "
                    f"wb_event={wb_event_id}, provider_event={existing_provider_id}"
                )
            else:
                result = await self.provider.create_event(
                    calendar_id=calendar_id,
                    event=provider_payload,
                )
                provider_event_id = result.get("provider_id", "")
                await self._update_sync_map(
                    user_id=user_id,
                    calendar_id=calendar_id,
                    waddlebot_event_id=wb_event_id,
                    provider_event_id=provider_event_id,
                    sync_token=None,
                    is_calendar_level=False,
                )
                logger.info(
                    f"[SYNC] Pushed CREATE to {self._provider_name}: "
                    f"wb_event={wb_event_id}, provider_event={provider_event_id}"
                )

            return result

        except Exception as exc:
            logger.error(
                f"[SYNC] push_event_to_external failed for wb_event={wb_event_id}: {exc}"
            )
            return None

    async def pull_event_from_external(
        self,
        canonical: Dict[str, Any],
        user_id: str,
        calendar_id: str,
        community_id: Optional[int] = None,
    ) -> Optional[str]:
        """Upsert a normalised provider event into the WaddleBot database.

        If a local record already exists for this provider_id, _resolve_conflict
        is called to determine which version wins (remote wins by default).

        Args:
            canonical: Normalised event dict from normalize_event().
            user_id: WaddleBot user ID.
            calendar_id: Provider calendar identifier.
            community_id: Optional community ID to associate the event with.

        Returns:
            WaddleBot-internal event UUID (str) on success, or None on failure.
        """
        provider_event_id = canonical.get("provider_id", "")

        try:
            local_event = await self._find_local_event_by_provider_id(
                provider_event_id=provider_event_id,
                user_id=user_id,
                calendar_id=calendar_id,
            )

            if local_event:
                resolved = self._resolve_conflict(local=local_event, remote=canonical)
                wb_id = local_event.get("id") or str(uuid.uuid4())
                await self._upsert_local_event(
                    event=resolved,
                    wb_event_id=str(wb_id),
                    user_id=user_id,
                    community_id=community_id,
                    calendar_id=calendar_id,
                )
                logger.debug(
                    f"[SYNC] Updated local event: wb_id={wb_id}, "
                    f"provider_id={provider_event_id}"
                )
                return str(wb_id)
            else:
                wb_id = str(uuid.uuid4())
                canonical["id"] = wb_id
                await self._upsert_local_event(
                    event=canonical,
                    wb_event_id=wb_id,
                    user_id=user_id,
                    community_id=community_id,
                    calendar_id=calendar_id,
                )
                await self._update_sync_map(
                    user_id=user_id,
                    calendar_id=calendar_id,
                    waddlebot_event_id=wb_id,
                    provider_event_id=provider_event_id,
                    sync_token=None,
                    is_calendar_level=False,
                )
                logger.debug(
                    f"[SYNC] Created local event: wb_id={wb_id}, "
                    f"provider_id={provider_event_id}"
                )
                return wb_id

        except Exception as exc:
            logger.error(
                f"[SYNC] pull_event_from_external failed for "
                f"provider_id={provider_event_id}: {exc}"
            )
            return None

    # ──────────────────────────────────────────────────────────────────────
    # Conflict resolution
    # ──────────────────────────────────────────────────────────────────────

    def _resolve_conflict(
        self,
        local: Dict[str, Any],
        remote: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Resolve a conflict between a local and remote event version.

        Strategy: **remote wins**.  The remote (provider) version is returned
        unchanged.  The local version is preserved only in log output for audit
        purposes.

        Args:
            local: Current local event dict.
            remote: Incoming remote event dict.

        Returns:
            The winning (remote) event dict with the local WaddleBot "id"
            preserved so the upsert targets the correct DB row.
        """
        local_updated = local.get("updated_at") or ""
        remote_updated = remote.get("updated_at") or ""

        if local_updated != remote_updated:
            logger.info(
                f"[SYNC] Conflict resolved (remote wins): "
                f"provider_id={remote.get('provider_id')}, "
                f"local_updated={local_updated!r}, remote_updated={remote_updated!r}"
            )
        else:
            logger.debug(
                f"[SYNC] No timestamp conflict for provider_id={remote.get('provider_id')}; "
                f"remote version applied."
            )

        # Preserve the local WaddleBot ID so the DB upsert targets the right row.
        resolved = dict(remote)
        resolved["id"] = local.get("id")
        return resolved

    # ──────────────────────────────────────────────────────────────────────
    # Sync map helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _update_sync_map(
        self,
        user_id: str,
        calendar_id: str,
        waddlebot_event_id: Optional[str],
        provider_event_id: Optional[str],
        sync_token: Optional[str],
        is_calendar_level: bool = False,
    ) -> None:
        """Upsert a sync map entry in the database.

        Calendar-level entries (is_calendar_level=True) store the sync token
        for the calendar as a whole; event-level entries store the mapping
        between a WaddleBot event UUID and a provider event ID.

        Args:
            user_id: WaddleBot user ID.
            calendar_id: Provider calendar identifier.
            waddlebot_event_id: WaddleBot event UUID (None for calendar-level entries).
            provider_event_id: Provider event ID (None for calendar-level entries).
            sync_token: Sync token to store (may be None for event-level entries).
            is_calendar_level: True if this is a calendar-level token entry.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            query = """
                INSERT INTO calendar_sync_map (
                    user_id, provider, calendar_id,
                    waddlebot_event_id, provider_event_id,
                    sync_token, is_calendar_level, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (user_id, provider, calendar_id, waddlebot_event_id)
                DO UPDATE SET
                    provider_event_id = EXCLUDED.provider_event_id,
                    sync_token = EXCLUDED.sync_token,
                    updated_at = EXCLUDED.updated_at
            """
            await self.dal.execute(query, [
                user_id,
                self._provider_name,
                calendar_id,
                waddlebot_event_id or "",
                provider_event_id or "",
                sync_token,
                is_calendar_level,
                now,
            ])
        except Exception as exc:
            logger.error(f"[SYNC] _update_sync_map failed: {exc}")

    async def _get_stored_sync_token(
        self,
        user_id: str,
        calendar_id: str,
    ) -> Optional[str]:
        """Retrieve the stored sync token for a user+calendar pair."""
        try:
            query = """
                SELECT sync_token
                FROM calendar_sync_map
                WHERE user_id = $1
                  AND provider = $2
                  AND calendar_id = $3
                  AND is_calendar_level = TRUE
                LIMIT 1
            """
            rows = await self.dal.execute(query, [user_id, self._provider_name, calendar_id])
            if rows:
                return rows[0].get("sync_token")
            return None
        except Exception as exc:
            logger.error(f"[SYNC] _get_stored_sync_token failed: {exc}")
            return None

    async def _get_provider_event_id(
        self,
        user_id: str,
        calendar_id: str,
        waddlebot_event_id: str,
    ) -> Optional[str]:
        """Look up the provider event ID for a given WaddleBot event UUID."""
        try:
            query = """
                SELECT provider_event_id
                FROM calendar_sync_map
                WHERE user_id = $1
                  AND provider = $2
                  AND calendar_id = $3
                  AND waddlebot_event_id = $4
                  AND is_calendar_level = FALSE
                LIMIT 1
            """
            rows = await self.dal.execute(query, [
                user_id, self._provider_name, calendar_id, waddlebot_event_id
            ])
            if rows:
                return rows[0].get("provider_event_id") or None
            return None
        except Exception as exc:
            logger.error(f"[SYNC] _get_provider_event_id failed: {exc}")
            return None

    async def _find_local_event_by_provider_id(
        self,
        provider_event_id: str,
        user_id: str,
        calendar_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Find the local canonical event dict for a given provider event ID."""
        try:
            map_query = """
                SELECT waddlebot_event_id
                FROM calendar_sync_map
                WHERE provider = $1
                  AND calendar_id = $2
                  AND provider_event_id = $3
                  AND user_id = $4
                  AND is_calendar_level = FALSE
                LIMIT 1
            """
            rows = await self.dal.execute(map_query, [
                self._provider_name, calendar_id, provider_event_id, user_id
            ])
            if not rows:
                return None

            wb_event_id = rows[0].get("waddlebot_event_id")
            if not wb_event_id:
                return None

            event_query = """
                SELECT * FROM calendar_synced_events
                WHERE id = $1
                LIMIT 1
            """
            event_rows = await self.dal.execute(event_query, [wb_event_id])
            return dict(event_rows[0]) if event_rows else None
        except Exception as exc:
            logger.error(f"[SYNC] _find_local_event_by_provider_id failed: {exc}")
            return None

    async def _upsert_local_event(
        self,
        event: Dict[str, Any],
        wb_event_id: str,
        user_id: str,
        community_id: Optional[int],
        calendar_id: str,
    ) -> None:
        """Insert or update a synced event record in the local database."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            query = """
                INSERT INTO calendar_synced_events (
                    id, provider, calendar_id, provider_event_id,
                    user_id, community_id,
                    title, description, location,
                    start_time, end_time, all_day, time_zone,
                    status, recurrence, organizer, attendees,
                    html_link, provider_created_at, provider_updated_at,
                    etag, raw_payload, synced_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, $18, $19, $20,
                    $21, $22, $23
                )
                ON CONFLICT (id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    location = EXCLUDED.location,
                    start_time = EXCLUDED.start_time,
                    end_time = EXCLUDED.end_time,
                    all_day = EXCLUDED.all_day,
                    time_zone = EXCLUDED.time_zone,
                    status = EXCLUDED.status,
                    recurrence = EXCLUDED.recurrence,
                    organizer = EXCLUDED.organizer,
                    attendees = EXCLUDED.attendees,
                    html_link = EXCLUDED.html_link,
                    provider_updated_at = EXCLUDED.provider_updated_at,
                    etag = EXCLUDED.etag,
                    raw_payload = EXCLUDED.raw_payload,
                    synced_at = EXCLUDED.synced_at
            """
            import json
            await self.dal.execute(query, [
                wb_event_id,
                self._provider_name,
                calendar_id,
                event.get("provider_id", ""),
                user_id,
                community_id,
                event.get("title", ""),
                event.get("description"),
                event.get("location"),
                event.get("start", ""),
                event.get("end", ""),
                event.get("all_day", False),
                event.get("time_zone", "UTC"),
                event.get("status", "confirmed"),
                json.dumps(event.get("recurrence") or []),
                json.dumps(event.get("organizer") or {}),
                json.dumps(event.get("attendees") or []),
                event.get("html_link"),
                event.get("created_at"),
                event.get("updated_at"),
                event.get("etag"),
                json.dumps(event.get("raw") or {}),
                now,
            ])
        except Exception as exc:
            logger.error(f"[SYNC] _upsert_local_event failed: {exc}")
            raise

    async def _apply_remote_deletion(
        self,
        provider_event_id: str,
        user_id: str,
        calendar_id: str,
    ) -> None:
        """Mark a locally synced event as cancelled following a remote deletion."""
        try:
            map_query = """
                SELECT waddlebot_event_id FROM calendar_sync_map
                WHERE provider = $1
                  AND calendar_id = $2
                  AND provider_event_id = $3
                  AND user_id = $4
                  AND is_calendar_level = FALSE
                LIMIT 1
            """
            rows = await self.dal.execute(map_query, [
                self._provider_name, calendar_id, provider_event_id, user_id
            ])
            if not rows:
                return

            wb_event_id = rows[0].get("waddlebot_event_id")
            if not wb_event_id:
                return

            update_query = """
                UPDATE calendar_synced_events
                SET status = 'cancelled', synced_at = $1
                WHERE id = $2
            """
            now = datetime.now(timezone.utc).isoformat()
            await self.dal.execute(update_query, [now, wb_event_id])
            logger.info(
                f"[SYNC] Remote deletion applied: wb_id={wb_event_id}, "
                f"provider_id={provider_event_id}"
            )
        except Exception as exc:
            logger.error(f"[SYNC] _apply_remote_deletion failed: {exc}")
