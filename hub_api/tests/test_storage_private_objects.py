"""`storage_service` private-object helpers -- `boto3`'s `_client()` is mocked, no real S3."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import storage_service


async def test_put_private_object_writes_under_sse_to_assets_bucket() -> None:
    client = MagicMock()
    sse = MagicMock()
    sse.put_params = AsyncMock(return_value={"ServerSideEncryption": "AES256"})
    with (
        patch("services.storage_service._client", return_value=client),
        patch("services.storage_service.get_object_storage_sse", return_value=sse),
    ):
        await storage_service.put_private_object("raffle-sounds/c/a.mp3", b"data", "audio/mpeg")

    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Key"] == "raffle-sounds/c/a.mp3"
    assert kwargs["Bucket"] == storage_service._bucket()
    assert kwargs["Body"] == b"data"
    assert kwargs["ServerSideEncryption"] == "AES256"
    assert "ACL" not in kwargs  # never public-read


async def test_get_private_object_returns_bytes() -> None:
    client = MagicMock()
    client.get_object.return_value = {"Body": BytesIO(b"abc")}
    with patch("services.storage_service._client", return_value=client):
        assert await storage_service.get_private_object("k") == b"abc"


async def test_get_private_object_missing_key_is_none() -> None:
    client = MagicMock()
    client.get_object.side_effect = RuntimeError("An error occurred (NoSuchKey)")
    with patch("services.storage_service._client", return_value=client):
        assert await storage_service.get_private_object("k") is None


async def test_get_private_object_other_error_propagates() -> None:
    client = MagicMock()
    client.get_object.side_effect = RuntimeError("AccessDenied")
    with patch("services.storage_service._client", return_value=client):
        with pytest.raises(RuntimeError):
            await storage_service.get_private_object("k")


async def test_delete_private_object_deletes_key() -> None:
    client = MagicMock()
    with patch("services.storage_service._client", return_value=client):
        await storage_service.delete_private_object("k")
    client.delete_object.assert_called_once_with(Bucket=storage_service._bucket(), Key="k")
