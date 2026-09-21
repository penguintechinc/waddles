# M1.5 — Bundle DAL Migration Inventory

Status: **Committed artifact — the Inventory deliverable of M1.5** (`docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`
§16 "M1.5 — Bundle DAL migration"). Read this alongside the Gate deliverable:
`scripts/check-bundle-dal-imports.sh` / `make check-bundle-dal`.

Spec wording: "Every bundle under `bundles/python/` that imports
`flask_core.database.AsyncDAL` or reaches a DAL through `get_bundle_dal()` is
listed, with the count reported — a zero count is a failure of the inventory,
not a pass." `bundles/python/` does not exist in this repo (verified: only
`core/svc_action/bundles`, `core/svc_ingest/bundles`, `core/svc_process/bundles`
exist under `core/*/bundles/`) — those three are the real scan targets, matching
the gate script and the in-flight migration branches.

## Denominator

**48 bundle `.py` files** scanned across the three directories (independently
counted via `find`, not assumed):

| Directory | `.py` files |
|---|---|
| `core/svc_action/bundles` | 19 |
| `core/svc_ingest/bundles` | 12 |
| `core/svc_process/bundles` | 17 |
| **Total** | **48** |

Non-zero denominator confirmed — the inventory below is not a zero-count
failure.

## Legacy DAL usage — 16 bundles (verified, not the 17 a naive grep reports)

A plain `grep -rl get_bundle_dal` returns **17** files. One of those,
`core/svc_process/bundles/community_context_process.py:12`, is a **false
positive**: the string appears only in a docstring comparing itself to
`community_reputation_process`'s pattern ("... same separation-of-concerns as
`community_reputation_process` calling `get_bundle_dal()` directly instead of
owning its own query layer") — the file itself imports `get_bundle_context`
(request-scoped tenant/community context, not a DAL handle) and never calls
`get_bundle_dal()`. Excluding it gives **16** bundles that actually need
migration, cross-checked against the independent bundle-migration plan on
`docs/plan-m1.5-bundle-migration` (its Task 5–19 file list names the same 16).

| # | Bundle | Service | Pattern |
|---|---|---|---|
| 1 | `community_announcements_action.py` | svc_action | `get_bundle_dal()` |
| 2 | `community_forums_action.py` | svc_action | `get_bundle_dal()` |
| 3 | `social_quote_action.py` | svc_action | `get_bundle_dal()` |
| 4 | `streaming_stream_action.py` | svc_action | `get_bundle_dal()` **+** `from flask_core import AsyncDAL` (direct) |
| 5 | `twitch_shoutout_action.py` | svc_action | `get_bundle_dal()` |
| 6 | `community_announcements_process.py` | svc_process | `get_bundle_dal()` |
| 7 | `community_chat_process.py` | svc_process | `get_bundle_dal()` |
| 8 | `community_loyalty_process.py` | svc_process | `get_bundle_dal()` |
| 9 | `community_polls_process.py` | svc_process | `get_bundle_dal()` |
| 10 | `community_reputation_process.py` | svc_process | `get_bundle_dal()` |
| 11 | `inventory_process.py` | svc_process | `get_bundle_dal()` |
| 12 | `social_alias_process.py` | svc_process | `get_bundle_dal()` |
| 13 | `social_music_process.py` | svc_process | `get_bundle_dal()` |
| 14 | `social_quote_process.py` | svc_process | `get_bundle_dal()` |
| 15 | `social_shoutout_process.py` | svc_process | `get_bundle_dal()` |
| 16 | `social_welcome_process.py` | svc_process | `get_bundle_dal()` |

`core/svc_ingest/bundles` (12 files: 7 `*_ingest.py` normalizers + 5
`*_gateway_manifest.py` registration modules): **0** hits — ingest bundles
normalize platform events and register `AppManifest`s, they don't touch the
DAL.

### `flask_core.database.AsyncDAL` import — literal count is 0, not 2

No bundle file contains the literal substring `flask_core.database` as an
import (`grep -rl 'flask_core\.database' core/*/bundles --include='*.py'` →
zero results). `streaming_stream_action.py:37` does
`from flask_core import AsyncDAL` — a **package-level re-export**
(`libs/flask_core/flask_core/__init__.py:25` does
`from .database import AsyncDAL, ...`), so it reaches
`flask_core.database.AsyncDAL` indirectly. This file is already counted in
the 16 above (it also calls `get_bundle_dal()`); it is the only bundle with
this second, direct-import pattern, called out here because its migration
also needs the `AsyncDAL` type hints removed, not just the `get_bundle_dal()`
call sites. Two other files (`community_forums_action.py:22`,
`community_announcements_action.py:34`) mention `` `flask_core/database.py` ``
in prose (slash form, describing the module file), not as an import — not
counted.

**Discrepancy from the task's stated "known state" (48 files, 2 with
`flask_core.database`, 17 with `get_bundle_dal`):** independently verified as
48 / 0-direct-plus-1-indirect / 16-real (17 naive). Reported rather than
silently reconciled, per Verification Integrity.

## Gate cross-check (why it fails today for a different reason)

`pydal` (bare word, any casing of usage) appears in **5** files as
docstring/comment prose describing the `AsyncDAL` wrapper's pydal-`Set`/`Field`
quirks (`community_announcements_action.py`, `community_forums_action.py`,
`streaming_stream_action.py`, `twitch_shoutout_action.py`,
`community_announcements_process.py`) — 14 occurrences total. `make
check-bundle-dal` / `scripts/check-bundle-dal-imports.sh` fails on these today
(`files_scanned=48 legacy_occurrences=14 in 5 file(s)`), which is correct: the
Gate's Done-when is a hard zero, and these comments won't be zero until the
Migration deliverable (tracked on `fix/m15-dal-svc-action` and its
svc_process counterpart) lands and the prose is rewritten alongside the code.

## Related deliverables (not in this inventory's scope)

- **Migration** (deliverable 2) and its **Tests** (deliverable 3): in flight
  on parallel branches `fix/m15-dal-svc-action` (svc_action) and a
  svc_process counterpart — not touched here per this task's hard
  constraint.
- **`consumes` migration** (deliverable 5): reported separately (not a
  committed artifact per this task's scope) — no `bundle.yaml`/manifest YAML
  file exists anywhere on disk today (`find . -iname bundle.yaml -o -iname
  'manifest.y*ml'` → zero results). Manifests live in two places instead:
  (1) in-process Python dict literals (e.g.
  `core/svc_ingest/bundles/twitch_gateway_manifest.py`'s
  `TWITCH_GATEWAY_MANIFEST`), parsed by
  `libs/flask_core/flask_core/app_manifest.py`'s `parse_manifest()`/
  `AppManifest`/`StageSpec` (the v1 schema) and registered into
  `flask_core.app_registry.AppRegistry` at each service's own startup; and
  (2) Postgres `app_catalog.stages` JSONB rows seeded via Alembic
  (`alembic/versions/`, e.g. `0014_wave1a_bundle_seeds.py`), which is what
  `core/svc_ingest/fanout.py`'s `resolve_consuming_apps()` and the
  process/action poll-drain loop actually resolve against — legacy
  `consumes` tags (`"twitch.eventsub"`, `"kick.message"`, etc.) live on
  `StageSpec.consumes` sourced from whichever of the two registered the
  App. The `consumes` migration (deliverable 5) therefore targets
  `app_catalog.stages.process.consumes` via a new Alembic migration, not a
  `bundle.yaml` file — `bundle.yaml` itself doesn't land until M2/M6's
  directory move.
