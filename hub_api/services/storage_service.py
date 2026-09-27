"""Object storage for user-uploaded assets (avatars) -- S3/MinIO only.

`app.py`'s own docstring states hub-api's rootless contract explicitly:
"no filesystem writes outside LOG_DIR" -- so unlike Node's
`storageService.js` (which defaults to local-disk storage in dev), this
port has no local-filesystem backend at all; every environment writes to
an S3-compatible bucket (MinIO in dev/beta, per this repo's existing
infra conventions, real S3 in prod). Scoped to exactly what
`profileController.js`'s avatar endpoints need (`uploadFile`/`deleteFile`/
`isAllowedImageType`/`MAX_FILE_SIZES.avatar`) -- Node's full
`storageService.js` also backs community logo/banner uploads
(`adminController.js`, M3 Platform-admin group); this module is a
reusable starting point for that group to extend, not a full port of
every caller.

Blocking boto3 calls run via `asyncio.to_thread` (`penguin-python-dev`
Concurrency Selection: "Blocking I/O -> asyncio.to_thread()") -- never
sync S3 I/O directly on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

import boto3
from botocore.client import Config as BotoConfig

logger = logging.getLogger(__name__)

ALLOWED_AVATAR_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
MAX_AVATAR_SIZE_BYTES = 5 * 1024 * 1024  # 5MB, matches profileController.js


def is_allowed_image_type(content_type: str) -> bool:
    """Is allowed image type."""
    return content_type in ALLOWED_AVATAR_CONTENT_TYPES


def _client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT_URL", "http://minio:9000"),
        aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY", ""),
        region_name=os.getenv("S3_REGION", "us-east-1"),
        config=BotoConfig(signature_version="s3v4"),
    )


def _bucket() -> str:
    return os.getenv("S3_BUCKET_NAME", "waddlebot-assets")


def _public_base_url() -> str:
    return os.getenv("S3_PUBLIC_BASE_URL", "http://localhost:9000/waddlebot-assets")


async def upload_avatar(data: bytes, original_filename: str, content_type: str) -> str:
    """Upload avatar bytes to `avatars/<uuid><ext>`. Returns the public URL."""
    ext = os.path.splitext(original_filename)[1] or ""
    key = f"avatars/{uuid.uuid4()}{ext}"

    def _put() -> None:
        _client().put_object(
            Bucket=_bucket(),
            Key=key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",  # security.md: default server-side encryption
        )

    await asyncio.to_thread(_put)
    return f"{_public_base_url()}/{key}"


MAX_LOGO_SIZE_BYTES = 5 * 1024 * 1024  # 5MB, matches communityProfileController.js's logo limit
MAX_BANNER_SIZE_BYTES = 10 * 1024 * 1024  # 10MB, matches its banner limit


async def upload_community_asset(
    data: bytes, original_filename: str, content_type: str, *, folder: str
) -> str:
    """Upload community logo/banner bytes to `<folder>/<uuid><ext>`. Returns the public URL.

    `folder` is `community-logos`/`community-banners`, matching Node's
    `uploadFile(buffer, 'community-logos', ...)` bucket-folder convention
    (`communityProfileController.js`) -- extends this module per its own
    docstring ("a reusable starting point ... not a full port of every
    caller"), reusing `upload_avatar`'s exact put_object/SSE pattern.
    """
    ext = os.path.splitext(original_filename)[1] or ""
    key = f"{folder}/{uuid.uuid4()}{ext}"

    def _put() -> None:
        _client().put_object(
            Bucket=_bucket(),
            Key=key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",  # security.md: default server-side encryption
        )

    await asyncio.to_thread(_put)
    return f"{_public_base_url()}/{key}"


def bundle_component_key(app_id: str, version: str, sha256_hex: str) -> str:
    """`bundles/{app_id}/{version}/{sha256}.wasm` -- the bucket layout every consumer expects.

    Not a public URL (unlike avatars/community assets): a staged bundle
    artifact is never web-public -- the executor's bucket poller (spec
    Sec7.6) reads it directly from the bucket via this same key, never
    through `_public_base_url()`. This is also the exact value persisted
    to `app_versions.component_key` (migration 0024) -- the data-plane
    loader resolves a published version's staged bytes straight from that
    column, with no digest-to-key re-derivation on that side.
    """
    return f"bundles/{app_id}/{version}/{sha256_hex}.wasm"


def bundle_sidecar_key(app_id: str, version: str, sha256_hex: str) -> str:
    """`bundles/{app_id}/{version}/{sha256}.json` -- the `.json` sidecar next to the component.

    Same path stem as `bundle_component_key()`; the value persisted to
    `app_versions.sidecar_key` (migration 0024).
    """
    return f"bundles/{app_id}/{version}/{sha256_hex}.json"


async def upload_bundle_component(app_id: str, version: str, sha256_hex: str, data: bytes) -> str:
    """Stage a validated component's bytes + a `{}` JSON sidecar to the bucket.

    Only called after `bundle_component_validator.validate_component()`
    has already approved `data` (spec Sec9.1's INSPECTING -> ADDRESSING
    edge, "sha256 over the component bytes") -- this function does no
    validation of its own. The sidecar is a `{}` stub for now (a future
    milestone's scan-result/signature metadata slot); returns the
    component's own bucket key, recorded on `app_version_uploads.
    staging_component_key` and (once published) `app_versions.component_key`.
    """
    key = bundle_component_key(app_id, version, sha256_hex)
    sidecar_key = bundle_sidecar_key(app_id, version, sha256_hex)

    def _put() -> None:
        client = _client()
        client.put_object(
            Bucket=_bucket(),
            Key=key,
            Body=data,
            ContentType="application/wasm",
            ServerSideEncryption="AES256",  # security.md: default server-side encryption
        )
        client.put_object(
            Bucket=_bucket(),
            Key=sidecar_key,
            Body=b"{}",
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )

    await asyncio.to_thread(_put)
    return key


async def delete_object(url: str) -> None:
    """Delete a previously-uploaded object given its public URL. Never raises on not-found."""
    base = _public_base_url().rstrip("/") + "/"
    if not url.startswith(base):
        return
    key = url[len(base) :]

    def _delete() -> None:
        try:
            _client().delete_object(Bucket=_bucket(), Key=key)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup, matches Node's deleteFile()
            logger.warning("Failed to delete storage object", extra={"key": key, "error": str(exc)})

    await asyncio.to_thread(_delete)
