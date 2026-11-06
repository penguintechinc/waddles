//! Small unit-level coverage: error codes, tracing audit sink, S3 backend construction.

use bundle_host_storage::s3::{S3Backend, S3Config};
use bundle_host_storage::{AuditEvent, AuditOutcome, AuditSink, StorageError, TracingAuditSink};

#[test]
fn every_error_has_a_distinct_wire_code_and_message() {
    let all = [
        StorageError::NotGranted("x".into()),
        StorageError::NotFound,
        StorageError::Invalid("x".into()),
        StorageError::TooLarge(1),
        StorageError::QuotaExceeded,
        StorageError::RateLimited,
        StorageError::Backend("x".into()),
        StorageError::NotImplemented,
    ];
    let mut codes: Vec<_> = all.iter().map(|e| e.wire_code()).collect();
    codes.sort();
    codes.dedup();
    assert_eq!(codes.len(), all.len());
    assert!(all.iter().all(|e| !e.to_string().is_empty()));
}

#[test]
fn tracing_sink_records_without_panicking() {
    TracingAuditSink.record(AuditEvent {
        caller: "a".into(),
        owner: "b".into(),
        tenant: 1,
        community: 2,
        op: "get",
        key: "k".into(),
        outcome: AuditOutcome::Allowed,
        reason: "r".into(),
    });
}

#[test]
fn s3_backend_builds_without_network() {
    S3Backend::new(&S3Config {
        endpoint: "http://127.0.0.1:1".into(),
        bucket: "b".into(),
        region: "us-east-1".into(),
        access_key_id: "k".into(),
        secret_access_key: "s".into(),
    })
    .expect("builds");
}
