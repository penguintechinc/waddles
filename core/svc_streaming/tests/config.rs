//! Integration-level configuration tests: a full env/CLI assembly rather
//! than the field-by-field unit tests in `src/config.rs`. Confirms the
//! whole `Config::load` path (env-only secrets + CLI/env operational
//! settings) behaves as a Helm/Docker deployment would exercise it.

use std::sync::Mutex;

use clap::Parser;

use svc_streaming::config::{CliConfig, Config};

// std::env is process-global; serialize env-mutating tests across the
// whole binary so parallel `cargo test` threads don't race.
static ENV_LOCK: Mutex<()> = Mutex::new(());

const SECRET_VARS: &[&str] = &[
    "DB_PASSWORD",
    "CACHE_PASSWORD",
    "SERVICE_API_KEY",
    "JWT_HMAC_SECRET",
];

fn clear_secret_env() {
    for var in SECRET_VARS {
        // SAFETY: serialized by ENV_LOCK held by every test in this file.
        unsafe { std::env::remove_var(var) };
    }
}

#[test]
fn full_deployment_style_env_and_cli_assembly() {
    let _guard = ENV_LOCK.lock().unwrap();
    clear_secret_env();
    // SAFETY: serialized by ENV_LOCK.
    unsafe {
        std::env::set_var("DB_PASSWORD", "prod-db-secret");
        std::env::set_var("SERVICE_API_KEY", "prod-service-key");
        // A leftover legacy shared secret must be inert: there is no HMAC path any more.
        std::env::set_var("JWT_HMAC_SECRET", "prod-jwt-secret");
    }

    let cli = CliConfig::try_parse_from([
        "svc-streaming",
        "--http-port",
        "8208",
        "--metrics-port",
        "9090",
        "--rtmp-port",
        "1935",
        "--srt-port",
        "9000",
        "--webrtc-udp-range",
        "40000-40100",
        "--db-host",
        "postgres.waddlebot.svc.cluster.local",
        "--db-name",
        "waddlebot_streaming",
    ])
    .expect("valid deployment-style CLI/env assembly must parse");

    let config = Config::from_cli(cli).expect("required secrets are set");

    assert_eq!(config.cli.http_port, 8208);
    assert_eq!(config.cli.db_name, "waddlebot_streaming");
    assert_eq!(config.db_password.expose(), "prod-db-secret");
    assert_eq!(config.service_api_key.expose(), "prod-service-key");
    // Verifying keys come from platform_vault (wired at startup), never from env.
    assert!(matches!(
        config.jwt_keys,
        svc_streaming::jwt_keys::JwtKeys::None
    ));

    clear_secret_env();
}

#[test]
fn missing_service_api_key_fails_closed() {
    let _guard = ENV_LOCK.lock().unwrap();
    clear_secret_env();
    // SAFETY: serialized by ENV_LOCK.
    unsafe {
        std::env::set_var("DB_PASSWORD", "prod-db-secret");
        // SERVICE_API_KEY intentionally left unset.
    }

    let cli = CliConfig::try_parse_from(["svc-streaming"]).unwrap();
    let result = Config::from_cli(cli);
    assert!(result.is_err(), "must fail closed without SERVICE_API_KEY");

    clear_secret_env();
}

#[test]
fn invalid_webrtc_udp_range_fails_closed() {
    let cli =
        CliConfig::try_parse_from(["svc-streaming", "--webrtc-udp-range", "not-a-range"]).unwrap();
    assert!(cli.validate().is_err());
}
