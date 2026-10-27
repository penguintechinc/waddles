"""Tests for `app_manifest.py`'s attribution/marketplace metadata block (migration 0026).

Mirrors `hub_api/tests/test_bundle_manifest_v2.py`'s own attribution shape
tests -- both manifest schemas share the same `flask_core.bundle_attribution`
rules (SPDX allowlist, https-only URLs), so a regression in either module's
wiring shows up the same way here. Unlike `bundle_manifest_v2`, `author`/
`license` are always OPTIONAL in this schema -- see `parse_manifest`'s own
comment for why the mandatory-for-vendors gate lives in `bundle_manifest_v2`
instead (this module's `provider: thirdparty` predates and means something
different -- `test_app_framework.py`'s "Acme Shoutout Pro" fixture is a
`thirdparty` App with no attribution at all, and must keep parsing).
"""

from __future__ import annotations

from typing import Any

import pytest

from flask_core.app_manifest import (
    REASON_INVALID_ALTERNATIVE_TO,
    REASON_INVALID_CATEGORY,
    REASON_INVALID_HOMEPAGE_URL,
    REASON_INVALID_NOTICE,
    REASON_INVALID_SOURCE_URL,
    REASON_UNKNOWN_SPDX_LICENSE,
    ManifestError,
    parse_manifest,
)

_BASE: dict[str, Any] = {
    "app_id": "waddles.integrations.vendor-42.mybundle",
    "name": "Vendor Bundle",
    "version": "1.0.0",
    "feature": "waddles.integrations.vendor-42",
    "module": "integrations",
    "provider": "builtin",
}


def _data(**overrides: Any) -> dict[str, Any]:
    return {**_BASE, **overrides}


def test_builtin_app_without_attribution_parses() -> None:
    manifest = parse_manifest(_data())
    assert manifest.author is None
    assert manifest.license is None
    assert manifest.license_requires_review is False


def test_thirdparty_app_without_attribution_still_parses() -> None:
    """`provider: thirdparty` here means "wraps a third-party endpoint", not "vendor-submitted".

    Author/license are never required by this schema -- the mandatory-for-
    vendors gate lives in `bundle_manifest_v2` (hub-api's own onboarding
    validator), which actually knows the caller authenticated via
    `vendor:onboard`.
    """
    manifest = parse_manifest(_data(provider="thirdparty"))
    assert manifest.author is None
    assert manifest.license is None


def test_vendor_app_with_author_and_license_parses() -> None:
    manifest = parse_manifest(_data(provider="thirdparty", author="Acme Corp", license="MIT"))
    assert manifest.author == "Acme Corp"
    assert manifest.license == "MIT"
    assert manifest.license_requires_review is False


def test_unknown_spdx_license_rejected() -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest(
            _data(provider="thirdparty", author="Acme Corp", license="Not-A-Real-License")
        )
    assert exc.value.reason == REASON_UNKNOWN_SPDX_LICENSE


def test_copyleft_license_flags_review_required() -> None:
    manifest = parse_manifest(
        _data(provider="thirdparty", author="Acme Corp", license="AGPL-3.0-only")
    )
    assert manifest.license_requires_review is True


def test_http_source_url_rejected() -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest(_data(source_url="http://example.com/repo"))
    assert exc.value.reason == REASON_INVALID_SOURCE_URL


def test_http_homepage_url_rejected() -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest(_data(homepage_url="http://example.com"))
    assert exc.value.reason == REASON_INVALID_HOMEPAGE_URL


def test_empty_alternative_to_entry_rejected() -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest(_data(alternative_to=[""]))
    assert exc.value.reason == REASON_INVALID_ALTERNATIVE_TO


def test_empty_notice_rejected() -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest(_data(notice=""))
    assert exc.value.reason == REASON_INVALID_NOTICE


def test_invalid_category_rejected() -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest(_data(category="not-a-real-category"))
    assert exc.value.reason == REASON_INVALID_CATEGORY


def test_alternatives_category_parses() -> None:
    manifest = parse_manifest(_data(category="alternatives"))
    assert manifest.category == "alternatives"
