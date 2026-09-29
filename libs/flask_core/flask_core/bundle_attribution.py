"""Shared SPDX-license and attribution-metadata validation for App/Bundle manifests.

Both `flask_core.app_manifest` (the install/available/activate manifest
schema `marketplace_lifecycle_service.install_bundle` parses) and
`hub_api`'s `bundle_manifest_v2` (the vendor-onboarding `bundle.yaml` v2
pre-check `bundle_version_service.create_version` runs) accept the same
optional attribution block -- `author`/`license`/`source_url`/
`alternative_to`/`homepage_url`/`notice`/`category` -- so a third-party
port can be credited and license-reviewed consistently regardless of
which pipeline a bundle enters through. This module is the single place
that owns the SPDX allowlist and the shared shape rules (https-only URLs,
non-empty/bounded strings); each caller wraps a failure in its OWN typed
error (`ManifestError`/`ManifestV2Error`) so the existing per-module
reason-code contract is untouched -- this module never raises.
"""

from __future__ import annotations

#: Permissive/OSS-friendly SPDX license identifiers auto-approved for a
#: vendor bundle without further review (general.md Supply Chain Security:
#: prefer well-known Western/international open-source licenses).
SPDX_ALLOWLIST = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BSD-3-Clause-Clear",
        "ISC",
        "0BSD",
        "Unlicense",
        "MPL-2.0",
        "PostgreSQL",
        "Python-2.0",
        "Zlib",
        "CC0-1.0",
        "BSL-1.0",
    }
)

#: Copyleft SPDX ids -- valid and NOT rejected outright (a vendor may have
#: a legitimate reason to ship one), but never silently accepted either:
#: `license_requires_review()` flags these so a human reviews the license
#: terms before the bundle is approved, per house supply-chain policy.
SPDX_COPYLEFT_REVIEW = frozenset(
    {
        "GPL-2.0",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "AGPL-3.0",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "LGPL-2.1",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
    }
)

#: Every SPDX id this module recognizes at all -- anything else is rejected
#: as unknown/unallowlisted rather than silently accepted.
SPDX_KNOWN = SPDX_ALLOWLIST | SPDX_COPYLEFT_REVIEW

#: Marketplace `category` enum. `alternatives` marks a bundle positioned as
#: a swap-in for a feature/product it does not itself implement (paired
#: with the manifest's own `alternative_to` list) -- the only value in use
#: today; deliberately a closed set so a typo'd category never silently
#: falls through as an unindexed value.
MARKETPLACE_CATEGORIES = frozenset({"alternatives"})

_MAX_NOTICE_LEN = 10_000
_MAX_ALT_ENTRY_LEN = 200


def license_requires_review(spdx_id: str) -> bool:
    """Whether `spdx_id` is a recognized copyleft license needing human review before approval."""
    return spdx_id in SPDX_COPYLEFT_REVIEW


def is_known_spdx(spdx_id: str) -> bool:
    """Whether `spdx_id` is on either the allow- or review-list."""
    return spdx_id in SPDX_KNOWN


def is_https_url(url: str) -> bool:
    """Whether `url` is a well-formed `https://` URL -- `http://` and other schemes are rejected."""
    return url.startswith("https://") and len(url) > len("https://")


def valid_alternative_to_entry(entry: str) -> bool:
    """Whether one `alternative_to` list entry (an app_id or free-text feature name) is usable."""
    return bool(entry) and len(entry) <= _MAX_ALT_ENTRY_LEN


def valid_notice(notice: str) -> bool:
    """Whether an inline/path `notice` value is non-empty and within the size ceiling."""
    return bool(notice) and len(notice) <= _MAX_NOTICE_LEN
