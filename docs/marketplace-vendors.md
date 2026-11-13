# Marketplace vendors

The marketplace catalog lists every active bundle, grouped by **vendor marketplace = one GitHub
organization**. A GitHub repo is a *type/sub-group inside* its vendor (a vendor with several
product repos shows each repo separately); it is not a marketplace of its own.

## Derivation (single source: `marketplace_catalog` view, alembic 0091)

| Field | Rule |
|---|---|
| `vendorKey` | lower-cased org from a canonical `github:<org>` author; else from a `https://github.com/<org>/...` `source_url`; else `NULL` |
| `sourceRepo` | lower-cased `org/repo` from `source_url` (trailing `.git` stripped) |
| `vendorName` | the author's GitHub handle with original casing, else the key |

`bundle` rows come from `app_catalog` (`status = 'active'`), version = newest `app_versions` row.
They have `sourceId = null` and are addressed by `sourceRef` (the `app_id`). Legacy `core` /
`marketplace` rows keep working; with no GitHub author they report `vendorKey = null`
(filter value `_unaffiliated`).

A `bundle` row with no derivable vendor (legacy rows without attribution, plain-name authors)
is listed as `_unaffiliated` everywhere (catalog, filter, `/bundle/<app_id>`, `/vendors`) -- never
a 500, never hidden -- and logged once per `app_id` per process at ERROR. The core-bundle seeder
still refuses to seed a manifest with no derivable vendor (`_assert_vendor_derivable`).

All active `app_catalog` rows are public in browse: they are only created by a global admin
(`install_bundle`) or the system seeder, and `app_tenant_availability` gates per-tenant
*activation*, not visibility.

## API (public browse, same visibility rules as the catalog)

- `GET /api/v1/marketplace/vendors` -> `{success, total, vendors: [{key, name, count, repos: [{repo, count}]}]}`;
  counts sum to the unfiltered catalog total; `_unaffiliated` sorts last.
- `GET /api/v1/marketplace/catalog?vendor=<key>&repo=<org/repo>` -> filters (case-insensitive;
  malformed values are 400).
- `GET /api/v1/marketplace/catalog/bundle/<app_id>` -> one bundle entry.

## Existing deployments

Alembic 0091 (applied by the `db-migrate` hook) backfills `app_catalog.author/source_url` from
the newest `app_versions` row where NULL, then rebuilds the view (SELECT grants re-issued). The
core-bundle seeder now writes/reconciles attribution on every run.

## Future: vendor fees for paid bundles

> **Nothing is decided and nothing is charged today.** This is a forward-looking note only.

Penguin Technologies, Inc. may eventually charge vendors a fee for **paid** app bundles listed in the marketplace.
Two models are under consideration: a one-time or periodic **registration fee**, or a
**transaction fee** on sales. A registration fee is the likely direction. No model, amount, or
date has been chosen.

- **Free bundles are unaffected** -- no fee of any kind is planned for them.
- **Penguin Technologies, Inc. will never charge for an open source, free app bundle** listed on the marketplace.
- Any change will be announced in advance and documented here before it takes effect.
