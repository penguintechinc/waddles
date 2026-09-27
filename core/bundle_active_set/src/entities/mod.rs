//! Read-only SeaORM entities for the hub-api-owned bundle-activation
//! schema (migrations `0022_app_versions_and_rbac`,
//! `0023_bundle_install_schema`, and `app_source_bindings`'s own migration
//! landing in parallel with this crate's own change -- see
//! `app_source_bindings`'s module doc for the table contract). This crate
//! never writes any of these tables -- see the crate root doc for the RO
//! account grants that make that a database-enforced guarantee, not just a
//! code convention.
//!
//! Each entity mirrors `core/svc_action/src/db/entities`'s established
//! pattern (`tenants.rs`/`communities.rs`): declare only the columns this
//! crate actually reads, no `Relation`/`Related` wiring (the join across
//! `app_active_versions` -> `app_versions` -> `app_install_approvals` is
//! done in Rust in `crate::query`, not via a SeaORM relation), and a
//! `table_name_matches_migration` test pinning the table name.

pub mod app_active_versions;
pub mod app_install_approvals;
pub mod app_source_bindings;
pub mod app_versions;
