//! The bundle `storage` host capability (`wit/waddle-bundle/storage.wit`).
//!
//! Spec `docs/superpowers/specs/2026-11-06-bundle-object-storage.md`. One bucket,
//! one prefix per bundle (`<owner>/<tenant>/<community>/<key>`), approved
//! cross-prefix grants, host-side tag filtering, host-signed presigned GETs.
//! Bundles never hold S3 credentials; tenant/community never come from the guest.

pub mod audit;
pub mod backend;
pub mod denial_queue;
pub mod error;
pub mod host;
pub mod key;
pub mod limits;
mod metrics;
pub mod pg_audit;
pub mod policy;
pub mod s3;
pub mod tags;

pub use audit::{AuditEvent, AuditOutcome, AuditSink, TracingAuditSink};
pub use backend::{MemoryBackend, ObjectBackend, ObjectMeta, StoredObject};
pub use error::StorageError;
pub use host::{StorageHost, StorageScope};
pub use pg_audit::PgAuditSink;
pub use policy::{GrantSet, Op};
