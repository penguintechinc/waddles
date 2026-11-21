"""Raffle customization service -- port of Node's `raffleCustomizationController.js`.

Per-event-type sound + message-template config for raffle/giveaway
events. Sound files live in the S3-compatible assets bucket (SeaweedFS)
under `raffle-sounds/<community-public-uuid>/` (SSE at rest via
`storage_service`), never on local disk (stateless containers). They are
served only through the membership-guarded hub-api route
`GET /api/v1/admin/<community>/raffle-customization/<event>/sound`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from flask_core.safe_logging import describe_exc

from . import storage_service
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
logger = logging.getLogger(__name__)

_FORMAT_CONTENT_TYPES = {"mp3": "audio/mpeg", "ogg": "audio/ogg", "wav": "audio/wav"}


def sniff_audio_format(data: bytes) -> str | None:
    """Detect mp3/ogg/wav from magic bytes; `None` when the content is none of them."""
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":
        return "mp3"
    # Bare MPEG audio frame: 11-bit sync word, version != reserved, layer != reserved.
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        if (data[1] >> 3) & 0x03 != 0x01 and (data[1] >> 1) & 0x03 != 0x00:
            return "mp3"
    return None


def object_key(public_community_id: str, filename: str) -> str:
    """Bucket key for one community's sound: `raffle-sounds/<community-uuid>/<filename>`."""
    return f"raffle-sounds/{public_community_id}/{filename}"


def sound_route_url(public_community_id: str, event_type: str) -> str:
    """Hub-api route that streams the sound for one event type (membership-guarded)."""
    return f"/api/v1/admin/{public_community_id}/raffle-customization/{event_type}/sound"


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


async def delete_customization(dal: Any, community_id: int, event_type: str) -> bool | None:
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
        await storage_service.delete_private_object(
            object_key(_public_id(dal, community_id), row.sound_filename)
        )
    return True


async def store_sound(
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

    sniffed = sniff_audio_format(data)
    if sniffed is None:
        return None, "File content is not a supported audio type (mp3, ogg, wav)"
    if sniffed != ext:
        return None, f"File content ({sniffed}) does not match its extension (.{ext})"

    ensure_community_tables(dal)
    public_community_id = _public_id(dal, community_id)
    timestamp = int(datetime.utcnow().timestamp() * 1000)
    safe_filename = f"{event_type}_{timestamp}.{ext}"

    existing = (
        dal(
            (dal.community_raffle_sounds.community_id == community_id)
            & (dal.community_raffle_sounds.event_type == event_type)
        )
        .select()
        .first()
    )

    try:
        await storage_service.put_private_object(
            object_key(public_community_id, safe_filename), data, _FORMAT_CONTENT_TYPES[ext]
        )
        if existing and existing.sound_filename:
            await storage_service.delete_private_object(
                object_key(public_community_id, existing.sound_filename)
            )
    except Exception as exc:  # noqa: BLE001 -- boto/KMS errors surface as a 400 upload failure
        logger.error("raffle sound upload failed: %s", describe_exc(exc))
        return None, "Failed to upload sound file"

    sound_url = sound_route_url(public_community_id, event_type)
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


async def read_sound(dal: Any, community_id: int, event_type: str) -> tuple[bytes, str] | None:
    """`(bytes, content_type)` of the stored sound, or `None` when none is set/stored."""
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
    if row is None or not row.sound_filename:
        return None
    data = await storage_service.get_private_object(
        object_key(_public_id(dal, community_id), row.sound_filename)
    )
    if data is None:
        return None
    return data, _FORMAT_CONTENT_TYPES.get(row.sound_format or "", "application/octet-stream")
