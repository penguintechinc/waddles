# Bundle Manifest Attribution & Marketplace Metadata

Added in migration `0026_bundle_attribution_metadata`. `bundle.yaml` (schema
`v2`) may declare an optional attribution block so a third-party port can be
credited, its license terms tracked, and the marketplace listing can call
out apps positioned as alternatives to a feature a tenant doesn't want to
self-host.

## Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `author` | string | Yes, for a vendor-onboarded bundle (`provider: thirdparty` uploaded via `POST /apps/{app_id}/versions` under a `vendor:onboard` token) | Display name, e.g. `"Acme Corp"` |
| `license` | string | Yes, alongside `author`, for the same vendor-onboarded case | SPDX identifier, validated against an allowlist (see below) |
| `source_url` | string | No | `https://` only |
| `alternative_to` | list of strings | No | Each entry is an `app_id` (`waddles.<module>.<feature>.<app>`) or a free-text feature/product name |
| `homepage_url` | string | No | `https://` only |
| `notice` | string | No | Inline attribution text, or a path to a `NOTICE` file inside the bundle's own source tree |
| `category` | string | No | Marketplace listing category. Only `alternatives` exists today (paired with `alternative_to`) |

```yaml
provider: thirdparty
author: Acme Corp
license: MIT
source_url: https://github.com/acme/waddles-bundle
homepage_url: https://acme.example.com
alternative_to:
  - waddles.socials.music.default
notice: "Portions (c) Acme Corp, under MIT."
category: alternatives
```

## Where `author`/`license` are mandatory

**Only for a bundle onboarded through the vendor-upload pipeline**
(`hub_api/services/bundle_version_service.py::create_version`, gated by
`services/vendor_bundle_authz.py`'s `vendor:onboard` scope check) --
enforced by `hub_api/services/bundle_manifest_v2.py`'s
`parse_bundle_manifest_v2`, keyed off the manifest's own `provider:
thirdparty` field. Reason codes: `vendor_author_required`,
`vendor_license_required`.

`libs/flask_core/flask_core/app_manifest.py`'s `parse_manifest` -- the
older manifest schema behind the admin-only `POST /api/v1/marketplace/bundles`
install endpoint -- accepts the same optional block (validated with the
same shape rules) but never requires `author`/`license`: `provider:
thirdparty` in that schema means "wraps a third-party endpoint"
(`execution_model`), not "submitted by a vendor", and pre-existing
first-party fixtures rely on that distinction.

## SPDX license allowlist

Owned by `libs/flask_core/flask_core/bundle_attribution.py` (imported by
both manifest validators above -- the two schemas can never drift on what
counts as a valid license):

- **`SPDX_ALLOWLIST`** -- accepted without further review: `MIT`,
  `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `BSD-3-Clause-Clear`, `ISC`,
  `0BSD`, `Unlicense`, `MPL-2.0`, `PostgreSQL`, `Python-2.0`, `Zlib`,
  `CC0-1.0`, `BSL-1.0`.
- **`SPDX_COPYLEFT_REVIEW`** -- accepted, but flags
  `license_review_required = true` on the persisted row for human review
  before approval, never silently accepted: `GPL-2.0(-only|-or-later)`,
  `GPL-3.0(-only|-or-later)`, `AGPL-3.0(-only|-or-later)`,
  `LGPL-2.1(-only|-or-later)`, `LGPL-3.0(-only|-or-later)`.
- Any other SPDX id (or a non-SPDX string) is rejected outright:
  `unknown_spdx_license`.

## Persistence

- **`app_catalog`** -- the marketplace's current/display record for an
  `app_id` (written by `marketplace_lifecycle_service.install_bundle`):
  the full block above, plus the derived `license_review_required` column.
  Exposed via `GET /api/v1/marketplace/bundles`'s `BundleDTO`
  (`hub_api/blueprints/v1/marketplace_lifecycle.py`), which also accepts a
  `?category=` filter.
- **`app_versions`** -- a per-published-version snapshot
  (`author`/`license`/`license_review_required`/`source_url` only; no
  `homepage_url`/`notice`/`alternative_to`/`category`, which are app-level,
  not version-level), written by
  `bundle_version_service.py::_publish_prebuilt_version` from the
  upload's own `manifest_json` blob.

All columns are nullable -- a bundle that predates this migration, or a
first-party `builtin` bundle that never declares the block, reads back
with `NULL`/empty values.
