"""Tests for `storage_service.bundle_component_key()`/`upload_bundle_component()`.

`boto3`'s `_client()` is mocked -- no real S3/MinIO connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.storage_service import (
    bundle_component_key,
    bundle_sidecar_key,
    upload_bundle_component,
)


def test_bundle_component_key_matches_the_documented_layout() -> None:
    key = bundle_component_key("waddles.integrations.vendor-42.mybundle", "1.0.0", "abc123")
    assert key == "bundles/waddles.integrations.vendor-42.mybundle/1.0.0/abc123.wasm"


def test_bundle_sidecar_key_matches_the_documented_layout() -> None:
    """The `app_versions.sidecar_key` value (migration 0024) -- same stem, `.json` extension."""
    key = bundle_sidecar_key("waddles.integrations.vendor-42.mybundle", "1.0.0", "abc123")
    assert key == "bundles/waddles.integrations.vendor-42.mybundle/1.0.0/abc123.json"


async def test_upload_bundle_component_puts_the_component_and_a_json_sidecar() -> None:
    mock_client = MagicMock()
    with patch("services.storage_service._client", return_value=mock_client):
        key = await upload_bundle_component(
            "waddles.integrations.vendor-42.mybundle", "1.0.0", "abc123", b"wasm-bytes"
        )

    assert key == "bundles/waddles.integrations.vendor-42.mybundle/1.0.0/abc123.wasm"
    assert mock_client.put_object.call_count == 2

    component_call, sidecar_call = mock_client.put_object.call_args_list
    assert component_call.kwargs["Key"] == key
    assert component_call.kwargs["Body"] == b"wasm-bytes"
    assert component_call.kwargs["ContentType"] == "application/wasm"
    assert component_call.kwargs["ServerSideEncryption"] == "AES256"

    assert sidecar_call.kwargs["Key"] == (
        "bundles/waddles.integrations.vendor-42.mybundle/1.0.0/abc123.json"
    )
    assert sidecar_call.kwargs["Body"] == b"{}"
    assert sidecar_call.kwargs["ServerSideEncryption"] == "AES256"
