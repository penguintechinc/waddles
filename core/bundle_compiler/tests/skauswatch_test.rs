//! Coverage for `scan::skauswatch` -- `SKAUSWATCH_URL` unset is reported
//! as `NotConfigured` (never a silent pass), and pass/warn/fail verdicts
//! from a mocked Skauswatch are classified correctly.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bundle_compiler::errors::CompilerError;
use bundle_compiler::scan::skauswatch::{scan_with_skauswatch, SkauswatchVerdict};
use std::path::Path;

#[tokio::test]
async fn unset_url_is_not_configured_not_a_pass() {
    let verdict = scan_with_skauswatch(Path::new("tests/fixtures/bundles/scan-clean"), None)
        .await
        .unwrap();
    assert!(matches!(verdict, SkauswatchVerdict::NotConfigured));
}

#[tokio::test]
async fn pass_verdict_is_recorded() {
    let mut server = mockito::Server::new_async().await;
    server
        .mock("POST", "/api/v1/scan")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"verdict":"pass"}"#)
        .create_async()
        .await;
    let verdict = scan_with_skauswatch(
        Path::new("tests/fixtures/bundles/scan-clean"),
        Some(&server.url()),
    )
    .await
    .unwrap();
    assert_eq!(verdict, SkauswatchVerdict::Pass);
}

#[tokio::test]
async fn fail_verdict_blocks() {
    let mut server = mockito::Server::new_async().await;
    let mock = server
        .mock("POST", "/api/v1/scan")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"verdict":"fail","reason":"known-malware-signature-abc123"}"#)
        .create_async()
        .await;
    let err = scan_with_skauswatch(
        Path::new("tests/fixtures/bundles/scan-clean"),
        Some(&server.url()),
    )
    .await
    .unwrap_err();
    mock.assert_async().await;
    match err {
        CompilerError::ScanBlocked { reason, message } => {
            assert_eq!(reason, "skauswatch_fail");
            assert!(message.contains("known-malware-signature-abc123"));
        }
        other => panic!("expected ScanBlocked, got {other:?}"),
    }
}

#[tokio::test]
async fn warn_verdict_records_but_does_not_block() {
    let mut server = mockito::Server::new_async().await;
    server
        .mock("POST", "/api/v1/scan")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"verdict":"warn","findings":2}"#)
        .create_async()
        .await;
    let verdict = scan_with_skauswatch(
        Path::new("tests/fixtures/bundles/scan-clean"),
        Some(&server.url()),
    )
    .await
    .unwrap();
    assert!(matches!(verdict, SkauswatchVerdict::Warn { findings: 2 }));
}

#[tokio::test]
async fn unreachable_server_is_an_error_not_not_configured() {
    // Port 1 is reserved/unroutable -- guaranteed connection failure
    // without depending on any real network condition.
    let err = scan_with_skauswatch(
        Path::new("tests/fixtures/bundles/scan-clean"),
        Some("http://127.0.0.1:1"),
    )
    .await
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "skauswatch_unreachable"),
        other => panic!("expected ScanBlocked(skauswatch_unreachable), got {other:?}"),
    }
}

#[tokio::test]
async fn unparseable_response_body_is_a_bad_response() {
    let mut server = mockito::Server::new_async().await;
    server
        .mock("POST", "/api/v1/scan")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body("not json at all")
        .create_async()
        .await;
    let err = scan_with_skauswatch(
        Path::new("tests/fixtures/bundles/scan-clean"),
        Some(&server.url()),
    )
    .await
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "skauswatch_bad_response"),
        other => panic!("expected ScanBlocked(skauswatch_bad_response), got {other:?}"),
    }
}

#[tokio::test]
async fn unknown_verdict_is_a_bad_response() {
    let mut server = mockito::Server::new_async().await;
    server
        .mock("POST", "/api/v1/scan")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"verdict":"maybe"}"#)
        .create_async()
        .await;
    let err = scan_with_skauswatch(
        Path::new("tests/fixtures/bundles/scan-clean"),
        Some(&server.url()),
    )
    .await
    .unwrap_err();
    match err {
        CompilerError::ScanBlocked { reason, .. } => assert_eq!(reason, "skauswatch_bad_response"),
        other => panic!("expected ScanBlocked(skauswatch_bad_response), got {other:?}"),
    }
}
