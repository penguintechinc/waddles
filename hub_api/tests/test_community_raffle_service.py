"""`services/community_raffle.py` -- direct unit tests for the sound-upload/delete.

paths not exercised through `test_community_raffle.py`'s blueprint-level tests
(which deliberately avoid real filesystem writes -- see that file's docstring).

`_UPLOAD_BASE_DIR` is a module-level constant; monkeypatched to `tmp_path`
per test so `store_sound`/`delete_customization`'s real file I/O runs
against a throwaway directory, never `/app/uploads/...`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import services.community_raffle as raffle_svc
from services.community_raffle import delete_customization, store_sound, upsert_customization
from tests.community_ids import public_id


@pytest.fixture(autouse=True)
def _seed_public_id(community_db: Any) -> None:
    """Responses carry the community UUID: give the fixture community one."""
    dal, community_id = community_db
    public_id(dal, community_id)


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


class TestDeleteCustomization:
    def test_delete_with_no_existing_row_returns_true(self, community_db: Any) -> None:
        dal, community_id = community_db
        assert delete_customization(dal, community_id, "raffle_end") is True

    def test_delete_removes_row_and_sound_file(
        self, community_db: Any, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(raffle_svc, "_UPLOAD_BASE_DIR", str(tmp_path))
        dal, community_id = community_db

        dto, err = store_sound(
            dal, community_id, "raffle_winner", "clip.mp3", 100, b"fake-audio-bytes"
        )
        assert err is None
        assert dto is not None
        sound_path = tmp_path / str(community_id) / dto.sound_filename
        assert sound_path.exists()

        result = delete_customization(dal, community_id, "raffle_winner")
        assert result is True
        assert not sound_path.exists()
        assert dal(dal.community_raffle_sounds.community_id == community_id).count() == 0

    def test_invalid_event_type_returns_none(self, community_db: Any) -> None:
        dal, community_id = community_db
        assert delete_customization(dal, community_id, "not-a-real-event") is None


class TestStoreSound:
    def test_invalid_event_type_returns_error(self, community_db: Any) -> None:
        dal, community_id = community_db
        dto, err = store_sound(dal, community_id, "not-a-real-event", "clip.mp3", 100, b"data")
        assert dto is None
        assert err is not None and "Invalid event type" in err

    def test_file_too_large_returns_error(self, community_db: Any) -> None:
        dal, community_id = community_db
        dto, err = store_sound(
            dal, community_id, "raffle_start", "clip.mp3", 3 * 1024 * 1024, b"data"
        )
        assert dto is None
        assert err == "File exceeds 2MB limit"

    def test_invalid_format_returns_error(self, community_db: Any) -> None:
        dal, community_id = community_db
        dto, err = store_sound(dal, community_id, "raffle_start", "clip.exe", 100, b"data")
        assert dto is None
        assert err is not None and "Invalid file format" in err

    def test_new_upload_creates_row_and_file(
        self, community_db: Any, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(raffle_svc, "_UPLOAD_BASE_DIR", str(tmp_path))
        dal, community_id = community_db

        dto, err = store_sound(dal, community_id, "giveaway_start", "clip.wav", 200, b"wav-bytes")
        assert err is None
        assert dto is not None
        assert dto.sound_format == "wav"
        assert dto.sound_size_bytes == 200
        assert dto.is_active is True
        assert (tmp_path / str(community_id) / dto.sound_filename).read_bytes() == b"wav-bytes"

    def test_reupload_replaces_existing_file(
        self, community_db: Any, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(raffle_svc, "_UPLOAD_BASE_DIR", str(tmp_path))
        dal, community_id = community_db

        first_dto, _ = store_sound(dal, community_id, "giveaway_end", "one.mp3", 10, b"first")
        assert first_dto is not None
        first_path = tmp_path / str(community_id) / first_dto.sound_filename
        assert first_path.exists()

        second_dto, err = store_sound(dal, community_id, "giveaway_end", "two.ogg", 20, b"second")
        assert err is None
        assert second_dto is not None
        assert second_dto.sound_format == "ogg"
        assert not first_path.exists()  # old file cleaned up
        assert dal(dal.community_raffle_sounds.community_id == community_id).count() == 1
