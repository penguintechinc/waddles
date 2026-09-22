//! Hand-written SeaORM entities for the tables this service actually reads
//! or writes. No `sea-orm-cli` was run against the cluster (schema/DDL is
//! owned by the SQL migrations under `config/postgres/migrations/`, not
//! this crate) -- each `Model` below only declares the columns this
//! service touches.
//!
//! `action_dispatch_log` (migration 074) is this service's own table (see
//! `rules/backend-database.md` Per-Service Database Accounts) -- no other
//! service writes it. TODO(M3): executor integration -- blocked on M2 --
//! is what actually inserts rows through this entity; this scaffold only
//! declares the shape.

pub mod action_dispatch_log;
