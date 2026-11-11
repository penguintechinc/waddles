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

A `bundle` row with no derivable vendor is a data defect: the API returns 500 and logs
`marketplace bundle has no derivable vendor`; the core-bundle seeder refuses such a manifest up
front (`_assert_vendor_derivable`). Nothing is silently bucketed.

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
