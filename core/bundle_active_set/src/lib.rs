//! Shared, read-only Postgres query + diff logic for the DB-driven
//! active-bundle loader (spec: "the data plane reads ACTIVE, APPROVED
//! bundle config from a READ-ONLY Postgres and hot-swaps bundles in/out
//! with NO pod restart; control plane (hub-api) is the only writer").
//! `core/svc_process` and `core/svc_action` both depend on this crate as a
//! same-repo `path` dependency (never a `penguin-libs` git dependency --
//! this schema is owned by *this* repo's own Alembic migrations, not
//! `penguin-libs`) so the query/diff logic is written and tested exactly
//! once instead of drifting between two copies.
//!
//! # What this crate does NOT do
//!
//! - Open the executor wire connection or send `Load`/`Unload` frames --
//!   that needs each service's own `host_api::Connection` type
//!   (`penguin_bundle_host::wire`), which this crate has no dependency on.
//!   [`diff::plan`] only computes *what* to load/unload; each service's
//!   own `bundle_loader` module (`core/svc_process/src/bundle_loader.rs`,
//!   `core/svc_action/src/bundle_loader.rs`) drives the actual calls.
//! - Gate on the PostHog-compatible feature flag
//!   (`waddles.core.db-bundle-config`) -- that is `penguin_licensing`,
//!   already a per-service dependency; see each service's own
//!   `license.rs` for the flag-gate wiring, mirroring the existing
//!   `waddles.core.rust-data-plane` gate.
//!
//! # RO account SQL grants (documentation only -- no migration in this
//! change; `config/postgres/rbac-matrix.yaml` + a new Alembic migration
//! are the right place to actually create these roles, tracked as a
//! follow-up so this Rust-focused change doesn't also own a schema/RBAC
//! migration)
//!
//! This crate's queries (`crate::query`) touch exactly three tables, all
//! `SELECT`-only:
//!
//! ```sql
//! CREATE ROLE svc_process_ro LOGIN PASSWORD '<secret>';
//! CREATE ROLE svc_action_ro LOGIN PASSWORD '<secret>';
//!
//! GRANT SELECT ON app_active_versions   TO svc_process_ro, svc_action_ro;
//! GRANT SELECT ON app_versions          TO svc_process_ro, svc_action_ro;
//! GRANT SELECT ON app_install_approvals TO svc_process_ro, svc_action_ro;
//! -- No INSERT/UPDATE/DELETE grant on any table, ever -- enforced at the
//! -- database in addition to `crate::reader::connect`'s own defense-in-
//! -- depth `options[default_transaction_read_only]=on` startup parameter
//! -- (applied to every physical connection in the pool, not a one-shot
//! -- post-connect `SET`).
//! ```
//!
//! `app_catalog` itself is never queried directly by this crate (only
//! joined-through via `app_id` FKs already resolved on the rows it does
//! read), so it needs no grant here.

pub mod diff;
pub mod entities;
pub mod query;
pub mod reader;

pub use diff::{plan, DiffPlan};
pub use query::{
    derive_component_keys, read_active_set, read_watermark, ActiveBundleRow, ActiveSetError,
    ActiveSetRead, DegradedReason, ExclusionReason, Watermark, WatermarkTracker,
};
pub use reader::ReaderConfig;
