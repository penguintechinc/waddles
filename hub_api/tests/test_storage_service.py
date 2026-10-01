"""Tests for `storage_service.bundle_component_key()`/`upload_bundle_component()`.

`boto3`'s `_client()` is mocked -- no real S3 connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.storage_service import (
    _bucket,
    _bundle_bucket,
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


# regression: bundle publish/fetch bucket split after SeaweedFS migration (#508/#509) --
# upload_bundle_component() must target BUNDLE_BUCKET_NAME (core/bundle_executor's own
# bucket), NEVER S3_BUCKET_NAME ("waddlebot-assets", avatars/community assets only) -- a
# publish into the wrong bucket "succeeds" but 404s for every bundle-executor fetch.


def test_bundle_bucket_defaults_match_the_bundle_executor_chart_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BUNDLE_BUCKET_NAME", raising=False)
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    assert _bundle_bucket() == "waddles-bundles"
    assert _bucket() == "waddlebot-assets"
    assert _bundle_bucket() != _bucket()


def test_bundle_bucket_honors_the_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUNDLE_BUCKET_NAME", "custom-bundles-bucket")
    monkeypatch.setenv("S3_BUCKET_NAME", "custom-assets-bucket")
    assert _bundle_bucket() == "custom-bundles-bucket"
    assert _bucket() == "custom-assets-bucket"


async def test_upload_bundle_component_targets_the_bundle_bucket_not_the_assets_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUNDLE_BUCKET_NAME", "waddles-bundles")
    monkeypatch.setenv("S3_BUCKET_NAME", "waddlebot-assets")
    mock_client = MagicMock()
    with patch("services.storage_service._client", return_value=mock_client):
        await upload_bundle_component("waddles.core.demo.echo", "1.0.0", "abc123", b"wasm-bytes")

    component_call, sidecar_call = mock_client.put_object.call_args_list
    assert component_call.kwargs["Bucket"] == "waddles-bundles"
    assert sidecar_call.kwargs["Bucket"] == "waddles-bundles"
