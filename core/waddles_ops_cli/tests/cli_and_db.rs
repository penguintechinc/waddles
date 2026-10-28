//! Binary-level exit-code contract and DB-config tests that need no database.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::io::Write;
use std::process::{Command, Stdio};

use waddles_ops_cli::db::connect_options_from;
use waddles_ops_cli::suppression_cli::{py_isoformat, read_platform_user_id};

fn bin() -> Command {
    let mut c = Command::new(env!("CARGO_BIN_EXE_waddles-ops"));
    // A database that cannot be reached, and no secrets, unless a test opts in.
    c.env_remove("BUNDLE_SIGNING_PRIVATE_KEY")
        .env_remove("BUNDLE_SIGNING_KEY_ID")
        .env_remove("TENANT_KEK_HEX")
        .env_remove("S3_ACCESS_KEY_ID")
        .env_remove("OBJECT_STORAGE_KMS_ENABLED")
        .env("DB_CONNECT_TIMEOUT_SECS", "1")
        .env("DATABASE_URL", "postgres://u:p@127.0.0.1:1/none");
    c
}

fn code(c: &mut Command, stdin: &str) -> (i32, String, String) {
    let mut child = c
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(stdin.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    (
        out.status.code().unwrap(),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

#[test]
fn usage_errors_exit_2_without_touching_the_database() {
    let h = "a".repeat(64);
    let cases: Vec<(Vec<String>, &str)> = vec![
        (
            vec![
                "verify-audit-chain".into(),
                "--expect".into(),
                "nope".into(),
            ],
            "",
        ),
        (
            vec![
                "verify-audit-chain".into(),
                "--expect".into(),
                format!("c=0:{h}"),
            ],
            "",
        ),
        (
            vec![
                "suppression".into(),
                "lift".into(),
                "--tenant-id".into(),
                "1".into(),
                "--platform".into(),
                "discord".into(),
            ],
            "",
        ),
        (
            vec![
                "suppression".into(),
                "check".into(),
                "--tenant-id".into(),
                "0".into(),
                "--platform".into(),
                "discord".into(),
                "--platform-user-id-stdin".into(),
            ],
            "1\n",
        ),
        (
            vec![
                "suppression".into(),
                "check".into(),
                "--tenant-id".into(),
                "1".into(),
                "--platform".into(),
                "9bad".into(),
                "--platform-user-id-stdin".into(),
            ],
            "1\n",
        ),
        (
            vec![
                "suppression".into(),
                "check".into(),
                "--tenant-id".into(),
                "1".into(),
                "--platform".into(),
                "discord".into(),
                "--platform-user-id-stdin".into(),
            ],
            "\n",
        ),
        (
            vec![
                "suppression".into(),
                "check".into(),
                "--tenant-id".into(),
                "x".into(),
                "--platform".into(),
                "discord".into(),
                "--platform-user-id-stdin".into(),
            ],
            "1\n",
        ),
    ];
    for (args, stdin) in cases {
        let (c, _, _) = code(bin().args(&args), stdin);
        assert_eq!(c, 2, "args {args:?}");
    }
}

#[test]
fn unreachable_database_and_missing_secrets_exit_3() {
    let h = "a".repeat(64);
    for args in [
        vec!["verify-audit-chain".to_string()],
        vec![
            "verify-audit-chain".into(),
            "--expect".into(),
            format!("c=1:{h}"),
        ],
        vec!["suppression".into(), "stats".into()],
        vec!["suppression".into(), "purge".into()],
        vec!["sign-approved-bundles".into()],
        vec!["public-key".into()],
        vec![
            "sign".into(),
            "--app-id".into(),
            "a".into(),
            "--version".into(),
            "1".into(),
            "--digest".into(),
            "d".into(),
            "--approval-id".into(),
            "1".into(),
        ],
        vec!["verify".into()],
    ] {
        let (c, _, e) = code(bin().args(&args), "{}");
        assert_eq!(c, 3, "args {args:?}: {e}");
        assert!(!e.contains("u:p"), "credentials must never be echoed: {e}");
    }
    let (c, _, e) = code(
        bin().args([
            "suppression",
            "lift",
            "--tenant-id",
            "+1_0",
            "--platform",
            "Discord",
            "--platform-user-id-stdin",
        ]),
        "123\r\n",
    );
    assert_eq!(c, 3, "valid input + unreachable DB = cannot run: {e}");
}

#[test]
fn sign_and_verify_round_trip_through_the_binary() {
    let seed = "BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc=";
    let (c, pubkey, _) = code(
        bin()
            .env("BUNDLE_SIGNING_PRIVATE_KEY", seed)
            .env("BUNDLE_SIGNING_KEY_ID", "k1")
            .arg("public-key"),
        "",
    );
    assert_eq!(c, 0);
    let (c, sidecar, _) = code(
        bin()
            .env("BUNDLE_SIGNING_PRIVATE_KEY", seed)
            .env("BUNDLE_SIGNING_KEY_ID", "k1")
            .args([
                "sign",
                "--app-id",
                "core-ping",
                "--version",
                "1.0.0",
                "--digest",
                "sha256:ab",
                "--approval-id",
                "42",
            ]),
        "",
    );
    assert_eq!(c, 0);
    let keys = format!("{{\"k1\":\"{}\"}}", pubkey.trim());
    let (c, out, e) = code(
        bin().env("BUNDLE_SIGNING_PUBLIC_KEYS", &keys).arg("verify"),
        &sidecar,
    );
    assert_eq!(c, 0, "{e}");
    assert!(out.starts_with("verified app_id=core-ping version=1.0.0 key_id=k1 approval_id=42"));
    let (c, _, _) = code(
        bin().env("BUNDLE_SIGNING_PUBLIC_KEYS", "{}").arg("verify"),
        &sidecar,
    );
    assert_eq!(c, 3, "zero keys fails closed");
}

fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
    let m: std::collections::HashMap<String, String> = pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
    move |k| m.get(k).cloned()
}

#[test]
fn db_config_resolution_mirrors_hub_api() {
    assert!(connect_options_from(&env_of(&[("DATABASE_URL", "postgres://u:p@h:5/db")])).is_ok());
    let e = connect_options_from(&env_of(&[("DATABASE_URL", "not a url p4ss")]))
        .err()
        .unwrap()
        .to_string();
    assert!(!e.contains("p4ss"), "URL must not be echoed: {e}");
    assert!(connect_options_from(&env_of(&[])).is_ok(), "defaults");
    assert!(connect_options_from(&env_of(&[("DB_TYPE", "mysql")])).is_err());
    assert!(connect_options_from(&env_of(&[("DB_PORT", "x")])).is_err());
    assert!(connect_options_from(&env_of(&[
        ("DB_HOST", "h"),
        ("DB_PORT", "6543"),
        ("DB_PASS", "s3cret")
    ]))
    .is_ok());
    // Empty DATABASE_URL falls through to the DB_* components (Python `or`).
    assert!(connect_options_from(&env_of(&[("DATABASE_URL", ""), ("DB_TYPE", "mysql")])).is_err());
}

#[test]
fn stdin_line_and_isoformat_match_python() {
    assert_eq!(
        read_platform_user_id("123\r\nrest".as_bytes()).unwrap(),
        "123"
    );
    assert_eq!(read_platform_user_id("".as_bytes()).unwrap(), "");
    assert_eq!(read_platform_user_id("a b \n".as_bytes()).unwrap(), "a b ");
    let t = chrono::DateTime::from_timestamp(1_760_000_000, 0).unwrap();
    assert_eq!(py_isoformat(t), "2025-10-09T08:53:20+00:00");
    let t = chrono::DateTime::from_timestamp(1_760_000_000, 123_456_000).unwrap();
    assert_eq!(py_isoformat(t), "2025-10-09T08:53:20.123456+00:00");
}
