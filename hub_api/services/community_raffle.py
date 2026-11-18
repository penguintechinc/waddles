"""Raffle customization service -- port of Node's `raffleCustomizationController.js`.

Per-event-type sound + message-template config for raffle/giveaway
events. Sound files are stored on local disk under `UPLOAD_DIR`
(`/app/uploads/raffle-sounds` by default), matching Node's `multer`
memory-storage-then-write-to-disk pattern exactly -- no object storage
migration in scope for this port.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .community_common import ensure_community_tables
from .community_public_id import lookup_public_ids_sync

VALID_EVENT_TYPES = frozenset(
    {
        "raffle_start",
        "raffle_winner",
        "raffle_end",
        "giveaway_start",
        "giveaway_winner",
        "giveaway_end",
    }
)
VALID_FORMATS = frozenset({"mp3", "ogg", "wav"})
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024
_UPLOAD_BASE_DIR = os.getenv("UPLOAD_DIR", "/app/uploads/raffle-sounds")


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


@dataclass(slots=True, frozen=True)
class RaffleCustomization:
    """One `community_raffle_sounds` row."""

    id: int
    community_id: str
    event_type: str
    sound_url: str | None
    sound_filename: str | None
    sound_size_bytes: int | None
    sound_format: str | None
    message_template: str | None
    is_active: bool
    created_at: str | None
    updated_at: str | None


def _public_id(dal: Any, community_id: int) -> str:
    """Public UUID for one community id (fails loud when the row has none)."""
    return lookup_public_ids_sync(dal, [community_id])[int(community_id)]


def _to_dto(row: Any, public_community_id: str) -> RaffleCustomization:
    """Customization DTO; `community_id` is the community's PUBLIC UUID."""
    return RaffleCustomization(
        id=row.id,
        community_id=public_community_id,
        event_type=row.event_type,
        sound_url=row.sound_url,
        sound_filename=row.sound_filename,
        sound_size_bytes=row.sound_size_bytes,
        sound_format=row.sound_format,
        message_template=row.message_template,
        is_active=bool(row.is_active),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def get_customizations(dal: Any, community_id: int) -> dict[str, RaffleCustomization]:
    """All custom sounds/messages for a community, keyed by `event_type`."""
    ensure_community_tables(dal)
    rows = dal(dal.community_raffle_sounds.community_id == community_id).select(
        orderby=dal.community_raffle_sounds.event_type
    )
    public_id = _public_id(dal, community_id)
    return {r.event_type: _to_dto(r, public_id) for r in rows}


def upsert_customization(
    dal: Any,
    community_id: int,
    event_type: str,
    message_template: str | None,
    is_active: bool | None,
) -> RaffleCustomization | None:
    """Upsert `message_template`/`is_active` for one event type. `None` = invalid event type."""
    if event_type not in VALID_EVENT_TYPES:
        return None
    ensure_community_tables(dal)
    existing = (
        dal(
            (dal.community_raffle_sounds.community_id == community_id)
            & (dal.community_raffle_sounds.event_type == event_type)
        )
        .select()
        .first()
    )

    if existing is None:
        new_id = dal.community_raffle_sounds.insert(
            community_id=community_id,
            event_type=event_type,
            message_template=message_template,
            is_active=is_active if is_active is not None else True,
            updated_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
        dal.commit()
        return _to_dto(dal.community_raffle_sounds[new_id], _public_id(dal, community_id))

    fields: dict[str, Any] = {"updated_at": datetime.utcnow()}
    if message_template is not None:
        fields["message_template"] = message_template
    if is_active is not None:
        fields["is_active"] = is_active
    dal(dal.community_raffle_sounds.id == existing.id).update(**fields)
    dal.commit()
    return _to_dto(dal.community_raffle_sounds[existing.id], _public_id(dal, community_id))


def delete_customization(dal: Any, community_id: int, event_type: str) -> bool | None:
    """Reset an event type to defaults -- removes the row + sound file. `None` = invalid type."""
    if event_type not in VALID_EVENT_TYPES:
        return None
    ensure_community_tables(dal)
    row = (
        dal(
            (dal.community_raffle_sounds.community_id == community_id)
            & (dal.community_raffle_sounds.event_type == event_type)
        )
        .select()
        .first()
    )
    if row is None:
        return True  # nothing to delete -- Node returns success either way

    dal(dal.community_raffle_sounds.id == row.id).delete()
    dal.commit()

    if row.sound_filename:
        file_path = Path(_UPLOAD_BASE_DIR) / str(community_id) / row.sound_filename
        file_path.unlink(missing_ok=True)
    return True


def store_sound(
    dal: Any, community_id: int, event_type: str, filename: str, size: int, data: bytes
) -> tuple[RaffleCustomization | None, str | None]:
    """Validate + persist an uploaded sound file, upserting the DB row.

    Returns `(dto, None)` on success, `(None, error)` on validation failure.
    """
    if event_type not in VALID_EVENT_TYPES:
        return None, f"Invalid event type. Must be one of: {', '.join(sorted(VALID_EVENT_TYPES))}"
    if size > MAX_FILE_SIZE_BYTES:
        return None, "File exceeds 2MB limit"

    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in VALID_FORMATS:
        return None, f"Invalid file format. Must be one of: {', '.join(sorted(VALID_FORMATS))}"

    ensure_community_tables(dal)
    community_dir = Path(_UPLOAD_BASE_DIR) / str(community_id)
    timestamp = int(datetime.utcnow().timestamp() * 1000)
    safe_filename = f"{event_type}_{timestamp}.{ext}"
    dest_path = community_dir / safe_filename

    existing = (
        dal(
            (dal.community_raffle_sounds.community_id == community_id)
            & (dal.community_raffle_sounds.event_type == event_type)
        )
        .select()
        .first()
    )

    try:
        community_dir.mkdir(parents=True, exist_ok=True)
        if existing and existing.sound_filename:
            (community_dir / existing.sound_filename).unlink(missing_ok=True)
        dest_path.write_bytes(data)
    except OSError as exc:
        return None, f"Failed to upload sound file: {exc}"

    sound_url = f"/uploads/raffle-sounds/{community_id}/{safe_filename}"
    fields = {
        "sound_url": sound_url,
        "sound_filename": safe_filename,
        "sound_size_bytes": size,
        "sound_format": ext,
        "is_active": True,
        "updated_at": datetime.utcnow(),
    }
    if existing is None:
        new_id = dal.community_raffle_sounds.insert(
            community_id=community_id, event_type=event_type, created_at=datetime.utcnow(), **fields
        )
        dal.commit()
        return _to_dto(dal.community_raffle_sounds[new_id], _public_id(dal, community_id)), None

    dal(dal.community_raffle_sounds.id == existing.id).update(**fields)
    dal.commit()
    return _to_dto(dal.community_raffle_sounds[existing.id], _public_id(dal, community_id)), None
