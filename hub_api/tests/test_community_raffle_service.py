"""`services/community_raffle.py` -- direct unit tests for the sound-upload/delete paths.

Sound bytes live in the S3-compatible assets bucket (never local disk); the three
`storage_service` object calls are replaced by an in-memory dict so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

import services.community_raffle as raffle_svc
from services.community_raffle import (
    delete_customization,
    object_key,
    read_sound,
    sniff_audio_format,
    store_sound,
    upsert_customization,
)
from tests.community_ids import public_id

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16
OGG = b"OggS\x00\x02" + b"\x00" * 20
MP3_ID3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 20
MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 20


@pytest.fixture(autouse=True)
def _seed_public_id(community_db: Any) -> None:
    """Responses carry the community UUID: give the fixture community one."""
    dal, community_id = community_db
    public_id(dal, community_id)


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[bytes, str]]:
    """In-memory stand-in for the assets bucket: `key -> (bytes, content_type)`."""
    store: dict[str, tuple[bytes, str]] = {}

    async def put(key: str, data: bytes, content_type: str) -> None:
        store[key] = (data, content_type)

    async def get(key: str) -> bytes | None:
        return store[key][0] if key in store else None

    async def delete(key: str) -> None:
        store.pop(key, None)

    monkeypatch.setattr(raffle_svc.storage_service, "put_private_object", put)
    monkeypatch.setattr(raffle_svc.storage_service, "get_private_object", get)
    monkeypatch.setattr(raffle_svc.storage_service, "delete_private_object", delete)
    return store


class TestUpsertUpdateExistingRow:
    def test_upsert_updates_only_provided_fields(self, community_db: Any) -> None:
        dal, community_id = community_db
        upsert_customization(dal, community_id, "raffle_start", "original template", True)

        updated = upsert_customization(dal, community_id, "raffle_start", "new template", None)
        assert updated is not None
        assert updated.message_template == "new template"
        assert updated.is_active is True  # unchanged -- is_active was None in this call

        toggled = upsert_customization(dal, community_id, "raffle_start", None, False)
        assert toggled is not None
        assert toggled.message_template == "new template"  # unchanged
        assert toggled.is_active is False


class TestSniffAudioFormat:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [(WAV, "wav"), (OGG, "ogg"), (MP3_ID3, "mp3"), (MP3_FRAME, "mp3")],
    )
    def test_recognises_supported_audio(self, data: bytes, expected: str) -> None:
        assert sniff_audio_format(data) == expected

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"MZ\x90\x00" + b"\x00" * 20,
            b"<html>x</html>",
            b"#!/bin/sh\n",
            b"RIFF\x00\x00\x00\x00AVI ",
        ],
    )
    def test_rejects_everything_else(self, data: bytes) -> None:
        assert sniff_audio_format(data) is None


class TestDeleteCustomization:
    async def test_delete_with_no_existing_row_returns_true(self, community_db: Any) -> None:
        dal, community_id = community_db
        assert await delete_customization(dal, community_id, "raffle_end") is True

    async def test_delete_removes_row_and_sound_object(
        self, community_db: Any, bucket: dict[str, tuple[bytes, str]]
    ) -> None:
        dal, community_id = community_db

        dto, err = await store_sound(dal, community_id, "raffle_winner", "clip.mp3", 100, MP3_ID3)
        assert err is None
        assert dto is not None
        assert len(bucket) == 1

        assert await delete_customization(dal, community_id, "raffle_winner") is True
        assert bucket == {}
        assert dal(dal.community_raffle_sounds.community_id == community_id).count() == 0

    async def test_invalid_event_type_returns_none(self, community_db: Any) -> None:
        dal, community_id = community_db
        assert await delete_customization(dal, community_id, "not-a-real-event") is None


class TestStoreSound:
    async def test_invalid_event_type_returns_error(self, community_db: Any) -> None:
        dal, community_id = community_db
        dto, err = await store_sound(
            dal, community_id, "not-a-real-event", "clip.mp3", 100, MP3_ID3
        )
        assert dto is None
        assert err is not None and "Invalid event type" in err

    async def test_file_too_large_returns_error(self, community_db: Any) -> None:
        dal, community_id = community_db
        dto, err = await store_sound(
            dal, community_id, "raffle_start", "clip.mp3", 3 * 1024 * 1024, MP3_ID3
        )
        assert dto is None
        assert err == "File exceeds 2MB limit"

    async def test_invalid_format_returns_error(self, community_db: Any) -> None:
        dal, community_id = community_db
        dto, err = await store_sound(dal, community_id, "raffle_start", "clip.exe", 100, WAV)
        assert dto is None
        assert err is not None and "Invalid file format" in err

    async def test_renamed_non_audio_file_is_rejected(
        self, community_db: Any, bucket: dict[str, tuple[bytes, str]]
    ) -> None:
        """regression: extension-only validation let an executable through as `.mp3`."""
        dal, community_id = community_db
        dto, err = await store_sound(
            dal, community_id, "raffle_start", "evil.mp3", 40, b"MZ\x90\x00" + b"\x00" * 36
        )
        assert dto is None
        assert err is not None and "not a supported audio type" in err
        assert bucket == {}
        assert dal(dal.community_raffle_sounds.community_id == community_id).count() == 0

    async def test_audio_with_mismatched_extension_is_rejected(
        self, community_db: Any, bucket: dict[str, tuple[bytes, str]]
    ) -> None:
        dal, community_id = community_db
        dto, err = await store_sound(dal, community_id, "raffle_start", "clip.mp3", 40, WAV)
        assert dto is None
        assert err is not None and "does not match" in err
        assert bucket == {}

    async def test_new_upload_writes_object_under_community_prefix(
        self, community_db: Any, bucket: dict[str, tuple[bytes, str]]
    ) -> None:
        dal, community_id = community_db
        cuuid = public_id(dal, community_id)

        dto, err = await store_sound(dal, community_id, "giveaway_start", "clip.wav", 200, WAV)
        assert err is None
        assert dto is not None
        assert dto.sound_format == "wav"
        assert dto.sound_size_bytes == 200
        assert dto.is_active is True
        key = object_key(cuuid, dto.sound_filename)
        assert key.startswith(f"raffle-sounds/{cuuid}/")
        assert bucket[key] == (WAV, "audio/wav")
        # URL points at the membership-guarded route, never a public/local path.
        assert dto.sound_url == (f"/api/v1/admin/{cuuid}/raffle-customization/giveaway_start/sound")
        assert "/uploads/" not in dto.sound_url

    async def test_reupload_replaces_existing_object(
        self, community_db: Any, bucket: dict[str, tuple[bytes, str]]
    ) -> None:
        dal, community_id = community_db

        first_dto, _ = await store_sound(dal, community_id, "giveaway_end", "one.mp3", 10, MP3_ID3)
        assert first_dto is not None
        first_key = object_key(public_id(dal, community_id), first_dto.sound_filename)
        assert first_key in bucket

        second_dto, err = await store_sound(dal, community_id, "giveaway_end", "two.ogg", 20, OGG)
        assert err is None
        assert second_dto is not None
        assert second_dto.sound_format == "ogg"
        assert first_key not in bucket  # old object cleaned up
        assert len(bucket) == 1
        assert dal(dal.community_raffle_sounds.community_id == community_id).count() == 1

    async def test_storage_failure_is_reported_not_swallowed(
        self, community_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(key: str, data: bytes, content_type: str) -> None:
            raise RuntimeError("s3 down")

        monkeypatch.setattr(raffle_svc.storage_service, "put_private_object", boom)
        dal, community_id = community_db
        dto, err = await store_sound(dal, community_id, "raffle_end", "c.mp3", 10, MP3_ID3)
        assert dto is None
        assert err == "Failed to upload sound file"
        assert dal(dal.community_raffle_sounds.community_id == community_id).count() == 0


class TestReadSound:
    async def test_round_trip_returns_bytes_and_content_type(
        self, community_db: Any, bucket: dict[str, tuple[bytes, str]]
    ) -> None:
        dal, community_id = community_db
        await store_sound(dal, community_id, "raffle_start", "c.ogg", 10, OGG)
        assert await read_sound(dal, community_id, "raffle_start") == (OGG, "audio/ogg")

    async def test_missing_row_or_invalid_type_is_none(
        self, community_db: Any, bucket: dict[str, tuple[bytes, str]]
    ) -> None:
        dal, community_id = community_db
        assert await read_sound(dal, community_id, "raffle_start") is None
        assert await read_sound(dal, community_id, "nope") is None
