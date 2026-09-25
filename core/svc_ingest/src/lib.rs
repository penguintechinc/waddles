//! `svc-ingest`: the Waddles chat/event data-plane ingest service (Rust
//! rewrite, M5).
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess --
//! same split as `core/svc_streaming`.
//!
//! **M5 scope: the full e2e produce path is live.** Per the M5 milestone
//! row in `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`
//! S16 and `docs/superpowers/plans/2026-09-14-rust-data-plane-m5-svc-
//! ingest.md`, this chunk wires the primary e2e path end to end: a real
//! Twitch IRC chat message (`crate::ingest::twitch`, secondary: Discord
//! Gateway, `crate::ingest::discord`) is normalized (`crate::normalize`,
//! ported from the Python predecessor's `bundles/{twitch,discord}_ingest.py`
//! plus `receivers/twitch_irc.py`'s IRCv3 tag decoding), minted into a
//! D30-complete envelope (`workstream_id`/`event_id`/`trace`/
//! `binding.mac`, via `penguin_spine::KeyRing`/`compute_binding_mac`,
//! `crate::publish`), and `XADD`ed via `penguin_spine::SpineClient` onto
//! its ingest source's spine stream -- so `svc_process` (M4) can drain it.
//! The Twitch outbound relay (`crate::outbound`) drains the plain Valkey
//! list `svc_action`'s `relay` host capability `LPUSH`es onto
//! (`waddles:transport:irc:twitch:outbound`) and sends each queued message
//! via `penguin_connector_twitch::irc::TwitchIrcSender`.
//!
//! **Resolved:** `penguin-connector-{twitch,discord}` previously could not
//! be linked into this binary alongside `penguin-spine`/`penguin-logging`
//! (a `serde`/`serde_json`/`tokio`/`thiserror` exact-pin conflict across
//! the two crate families) -- fixed upstream in `penguin-libs` PR #98 (the
//! connectors' pins were bumped to match spine/logging exactly). See
//! `Cargo.toml`'s dependency-pattern comment for the pinned rev and the
//! verification evidence.
//!
//! The whole bundle above is additionally gated on the
//! `waddles.core.rust-data-plane` PostHog flag (spec S13.5,
//! `crate::license`) -- OFF (the default until validated) means this
//! service serves `/health`/`/healthz`/`/metrics` and receives/produces/
//! drains nothing at all, checked once at startup before any
//! `try_start_*` call below. Each receiver/the outbound drain
//! additionally, independently no-ops with a logged reason when its own
//! configuration/secret is absent
//! (`try_start_twitch_irc`/`try_start_discord`/`try_start_twitch_outbound`),
//! the same graceful-degradation contract
//! `core/svc_process::try_start_spine_drain` uses for a missing
//! `SpineConfig`.
//!
//! Left as a documented seam per explicit coordinator direction (a
//! separate follow-up owns it): any `penguin_spine::SpineClient::
//! dead_letter` call-site change -- `penguin-spine` was rev'd for a
//! `dead_letter`/`Delivered.group` fix, but this crate makes no
//! `dead_letter` call and constructs no `Delivered` at all (ingest mints
//! and `append`s only, per D23/D24/S10.6 -- it never reads a stage
//! stream), so there is no call site to chase.
//!
//! Also out of scope, per the milestone's own stated priority order:
//! Slack/YouTube/Kick receivers, the generic signed-webhook and JWT REST
//! intake surfaces (S10.1), `penguin_spine::SocketLease` single-owner-
//! socket guarding (S10.2, a separate crate gap, PA-LEASE), and D31
//! usage-delta metering (`UsageBatcher`/`append_usage`, a `penguin-spine`
//! crate gap at the pinned rev -- see `crate::publish`'s module doc).
//!
//! Per spec S4.1, this service has **no database** -- no `sea-orm`
//! dependency, unlike `svc_process`/`svc_action`.

pub mod config;
pub mod crypto;
pub mod error;
pub mod http;
pub mod ingest;
pub mod license;
pub mod normalize;
pub mod outbound;
pub mod publish;
pub mod telemetry;

use std::net::SocketAddr;
use std::sync::Arc;

use tokio::signal;

/// Default `tracing`/OTel service name, also the fallback `--healthcheck`
/// target and the resource `service.name` when `OTEL_SERVICE_NAME` is
/// unset.
pub const SERVICE_NAME: &str = "svc-ingest";

/// Runs the service: loads config, bootstraps telemetry, builds the
/// control-plane + metrics routers, and serves both until SIGINT/SIGTERM
/// is received.
pub async fn run() -> anyhow::Result<()> {
    let config = config::Config::load()?;
    run_with_shutdown(config, shutdown_signal(), shutdown_signal()).await
}

/// Same as [`run`], but takes an already-loaded [`config::Config`] and
/// caller-supplied shutdown futures for each listener instead of installing
/// OS signal handlers -- this is what makes the bind/serve/telemetry wiring
/// testable (see `tests/run.rs`): a test can pass `config` built via
/// [`config::CliConfig::parse_from`] (an explicit free port) and an
/// already-resolved future so the server binds, logs, and shuts down
/// immediately instead of blocking forever on a real signal.
pub async fn run_with_shutdown<F1, F2>(
    config: config::Config,
    http_shutdown: F1,
    metrics_shutdown: F2,
) -> anyhow::Result<()>
where
    F1: std::future::Future<Output = ()> + Send + 'static,
    F2: std::future::Future<Output = ()> + Send + 'static,
{
    // MUST run before anything builds a TLS-capable client (the OTLP
    // exporter `telemetry::init` may construct next, the license/flag
    // HTTPS client, the Valkey/IRC/Discord TLS sockets) -- see
    // `crate::crypto`'s module doc for why two rustls crypto backends
    // linked into this binary otherwise panic on first TLS use.
    crypto::ensure_installed();

    let (_telemetry_guard, prom_registry) = telemetry::init(SERVICE_NAME);

    tracing::info!(
        http_port = config.cli.http_port,
        metrics_port = config.cli.metrics_port,
        "starting {SERVICE_NAME}"
    );

    let ingest_metrics = Arc::new(telemetry::register_ingest_metrics(&prom_registry));
    let state = http::AppState::new(config.clone(), prom_registry);

    // `waddles.core.rust-data-plane` (spec S13.5): OFF (default until
    // validated) means serve /health + /metrics and start nothing below.
    // `flag_enabled` never performs inline network I/O (see
    // `crate::license`'s module doc), so this check never blocks startup
    // regardless of license/flag-server reachability.
    let license_client = license::build_license_client();
    if license::rust_data_plane_enabled(license_client.as_ref()).await {
        // Twitch IRC (primary e2e path), Discord Gateway (secondary), and
        // the Twitch outbound relay drain each independently no-op with a
        // logged reason when unconfigured -- see each function's own doc
        // comment. Slack/YouTube/Kick receivers, the generic webhook/JWT
        // intake, and D31 usage metering are `// TODO(M5)` -- not started
        // here, see this module's doc comment.
        try_start_twitch_irc(&config, ingest_metrics.clone());
        try_start_discord(&config, ingest_metrics);
        try_start_twitch_outbound(&config);
    } else {
        tracing::info!(
            flag = license::RUST_DATA_PLANE_FLAG,
            "flag is OFF; receive/produce/outbound-drain not started"
        );
    }

    let http_addr = SocketAddr::new(config.cli.bind_addr, config.cli.http_port);
    let metrics_addr = SocketAddr::new(config.cli.bind_addr, config.cli.metrics_port);

    let http_listener = tokio::net::TcpListener::bind(http_addr).await?;
    let metrics_listener = tokio::net::TcpListener::bind(metrics_addr).await?;

    tracing::info!(%http_addr, %metrics_addr, "listening");

    let http_server = axum::serve(http_listener, http::router(state.clone()))
        .with_graceful_shutdown(http_shutdown);
    let metrics_server = axum::serve(metrics_listener, http::metrics_router(state))
        .with_graceful_shutdown(metrics_shutdown);

    tokio::try_join!(
        async { http_server.await.map_err(anyhow::Error::from) },
        async { metrics_server.await.map_err(anyhow::Error::from) },
    )?;

    Ok(())
}

/// Attempts to start the Twitch IRC receiver (`crate::ingest::twitch::run`)
/// as its own background task and returns immediately either way -- never
/// blocks or fails `run_with_shutdown`'s caller. Three independent reasons
/// this never starts the receiver, all logged and none an error (the same
/// graceful-degradation contract `core/svc_process::try_start_spine_drain`
/// uses for a missing `SpineConfig`):
/// - `Config::twitch_irc_enabled()` is false (nick/channel/oauth token not
///   all set)
/// - the D30 binding keyring/active-kid is absent or invalid -- minting
///   without a valid keyring is unsafe, never attempted
///   ([`resolve_binding_keyring`])
/// - `penguin_spine::SpineConfig::from_env()` fails (e.g. `VALKEY_URL`
///   unset)
fn try_start_twitch_irc(config: &config::Config, metrics: Arc<telemetry::IngestMetrics>) {
    if !config.twitch_irc_enabled() {
        tracing::info!(
            "TWITCH_IRC_NICK/_CHANNEL/_OAUTH_TOKEN not fully set; twitch irc receiver not started"
        );
        return;
    }
    let Some(oauth_token) = config
        .twitch_irc_oauth_token
        .as_ref()
        .map(|s| s.expose().to_string())
    else {
        return; // unreachable given twitch_irc_enabled() above; fail-safe.
    };
    let keyring = match resolve_binding_keyring(config, "twitch irc") {
        Some(ring) => ring,
        None => return,
    };

    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; twitch irc receiver not started");
            return;
        }
    };

    let irc_cfg = ingest::twitch::irc_config(
        &config.cli.twitch_irc_host,
        config.cli.twitch_irc_port,
        &config.cli.twitch_irc_nick,
        &config.cli.twitch_irc_channel,
        &oauth_token,
        config.cli.twitch_irc_use_tls,
    );
    let channel = config.cli.twitch_irc_channel.clone();
    let nick = config.cli.twitch_irc_nick.clone();
    let active_kid = config.cli.binding_active_kid.clone();
    let scope = config.ingest_scope();

    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = shutdown_tx.send(());
    });
    tokio::spawn(async move {
        let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = metrics.clone();
        let appender = match penguin_spine::SpineClient::connect(spine_cfg, spine_metrics).await {
            Ok(client) => client,
            Err(err) => {
                tracing::error!(error = %err, "spine connect failed; twitch irc receiver not started");
                return;
            }
        };
        ingest::twitch::run(
            irc_cfg,
            &channel,
            &nick,
            &appender,
            metrics.as_ref(),
            &keyring,
            &active_kid,
            &scope,
            shutdown_rx,
        )
        .await;
    });
}

/// Attempts to start the Discord Gateway receiver
/// (`crate::ingest::discord::run`) as its own background task and returns
/// immediately either way -- same three-reason graceful-degradation
/// contract as [`try_start_twitch_irc`] (bot token, binding keyring, spine
/// config).
fn try_start_discord(config: &config::Config, metrics: Arc<telemetry::IngestMetrics>) {
    if !config.discord_enabled() {
        tracing::info!("DISCORD_BOT_TOKEN not set; discord gateway receiver not started");
        return;
    }
    let Some(bot_token) = config
        .discord_bot_token
        .as_ref()
        .map(|s| s.expose().to_string())
    else {
        return; // unreachable given discord_enabled() above; fail-safe.
    };
    let keyring = match resolve_binding_keyring(config, "discord gateway") {
        Some(ring) => ring,
        None => return,
    };

    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; discord gateway receiver not started");
            return;
        }
    };

    let mut gateway_cfg = penguin_connector_discord::gateway::GatewayConfig::new(bot_token);
    if !config.cli.discord_gateway_url.is_empty() {
        gateway_cfg.gateway_url = config.cli.discord_gateway_url.clone();
    }
    let active_kid = config.cli.binding_active_kid.clone();
    let scope = config.ingest_scope();

    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = shutdown_tx.send(());
    });
    tokio::spawn(async move {
        let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = metrics.clone();
        let appender = match penguin_spine::SpineClient::connect(spine_cfg, spine_metrics).await {
            Ok(client) => client,
            Err(err) => {
                tracing::error!(error = %err, "spine connect failed; discord gateway receiver not started");
                return;
            }
        };
        ingest::discord::run(
            gateway_cfg,
            &appender,
            metrics.as_ref(),
            &keyring,
            &active_kid,
            &scope,
            shutdown_rx,
        )
        .await;
    });
}

/// Attempts to start the Twitch outbound relay drain
/// (`crate::outbound::run`) as its own background task and returns
/// immediately either way. Two independent reasons this never starts,
/// both logged and neither an error: the Twitch identity
/// (nick/oauth token) is not configured (the outbound sender reuses the
/// receive side's own credential -- `crate::outbound`'s "Credential
/// resolution" doc), or `penguin_spine::SpineConfig::from_env()` fails.
/// Unlike the two receivers above, no binding keyring is needed here --
/// the outbound drain never mints or `XADD`s an envelope, it only relays a
/// chat send.
fn try_start_twitch_outbound(config: &config::Config) {
    let Some(oauth_token) = config
        .twitch_irc_oauth_token
        .as_ref()
        .map(|s| s.expose().to_string())
    else {
        tracing::info!("TWITCH_IRC_OAUTH_TOKEN not set; twitch outbound relay drain not started");
        return;
    };
    if config.cli.twitch_irc_nick.is_empty() {
        tracing::info!("TWITCH_IRC_NICK not set; twitch outbound relay drain not started");
        return;
    }

    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; twitch outbound relay drain not started");
            return;
        }
    };

    let identity = outbound::TwitchOutboundIdentity {
        host: config.cli.twitch_irc_host.clone(),
        port: config.cli.twitch_irc_port,
        nick: config.cli.twitch_irc_nick.clone(),
        oauth_token,
        use_tls: config.cli.twitch_irc_use_tls,
    };

    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = shutdown_tx.send(());
    });
    tokio::spawn(async move {
        if let Err(err) = outbound::run(&spine_cfg, identity, shutdown_rx).await {
            tracing::error!(error = %err, "twitch outbound relay drain exited");
        }
    });
}

/// Shared keyring-resolution step for [`try_start_twitch_irc`]/
/// [`try_start_discord`]: `None` (unconfigured), a parse error, or an
/// empty `ENVELOPE_BINDING_ACTIVE_KID` are all logged and treated as "this
/// receiver does not start" -- minting without a valid, named active key
/// is never attempted.
fn resolve_binding_keyring(
    config: &config::Config,
    receiver: &str,
) -> Option<penguin_spine::KeyRing> {
    if config.cli.binding_active_kid.is_empty() {
        tracing::warn!(
            receiver,
            "ENVELOPE_BINDING_ACTIVE_KID not set; receiver not started"
        );
        return None;
    }
    match config.binding_keyring() {
        Some(Ok(ring)) => Some(ring),
        Some(Err(err)) => {
            tracing::warn!(receiver, error = %err, "ENVELOPE_BINDING_KEYS invalid; receiver not started");
            None
        }
        None => {
            tracing::warn!(
                receiver,
                "ENVELOPE_BINDING_KEYS not set; receiver not started"
            );
            None
        }
    }
}

/// Waits for SIGINT (Ctrl-C) or SIGTERM (Kubernetes pod termination) and
/// returns, letting `axum::serve`'s graceful shutdown drain in-flight
/// requests rather than dropping connections mid-response.
async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("failed to install SIGINT handler");
    };

    #[cfg(unix)]
    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

/// Reads `MODULE_PORT` from the environment, defaulting to this service's
/// standard port -- shared by [`run`] (via [`config::Config::load`]'s
/// `clap` parsing) and [`run_healthcheck`], which deliberately avoids
/// pulling in the full [`config::Config`] (and its `clap::Parser::parse()`
/// call against real process argv) just to read one port.
fn healthcheck_port() -> u16 {
    std::env::var("MODULE_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8200)
}

/// Builds the loopback `/healthz` URL [`run_healthcheck`] probes.
fn healthcheck_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/healthz")
}

/// Probes `url` once and classifies the outcome. Split out from
/// [`run_healthcheck`] so the classification logic (success / bad status /
/// transport error) is unit-testable without the `std::process::exit(1)`
/// side effect a failing container healthcheck must have -- that side
/// effect isn't itself testable (it would tear down the test process), so
/// it stays in the thin wrapper below, not here.
async fn healthcheck_probe(client: &reqwest::Client, url: &str) -> Result<(), String> {
    match client.get(url).send().await {
        Ok(resp) if resp.status().is_success() => Ok(()),
        Ok(resp) => Err(format!("{url} returned {}", resp.status())),
        Err(err) => Err(err.to_string()),
    }
}

/// `svc-ingest --healthcheck`: GETs `/healthz` on the locally-bound HTTP
/// port and exits 0/1 accordingly. Reads `MODULE_PORT` the same way
/// [`run`] does. The container `HEALTHCHECK` invokes this directly instead
/// of relying on `curl` being present in the runtime image.
pub async fn run_healthcheck() -> anyhow::Result<()> {
    let url = healthcheck_url(healthcheck_port());
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()?;

    match healthcheck_probe(&client, &url).await {
        Ok(()) => Ok(()),
        Err(reason) => {
            eprintln!("healthcheck failed: {reason}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::routing::get;
    use axum::Router;
    use clap::Parser;
    use std::sync::Mutex;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variable.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn healthcheck_port_defaults_when_unset() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var("MODULE_PORT") };
        assert_eq!(healthcheck_port(), 8200);
    }

    #[test]
    fn healthcheck_port_reads_env_override() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("MODULE_PORT", "9999") };
        assert_eq!(healthcheck_port(), 9999);
        unsafe { std::env::remove_var("MODULE_PORT") };
    }

    #[test]
    fn healthcheck_url_formats_loopback_address() {
        assert_eq!(healthcheck_url(8200), "http://127.0.0.1:8200/healthz");
    }

    async fn spawn_fixed_status_server(status: axum::http::StatusCode) -> std::net::SocketAddr {
        let router = Router::new().route("/healthz", get(move || async move { status }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, router).await.ok();
        });
        addr
    }

    #[tokio::test]
    async fn healthcheck_probe_succeeds_on_200() {
        let addr = spawn_fixed_status_server(axum::http::StatusCode::OK).await;
        let client = reqwest::Client::new();
        let url = healthcheck_url(addr.port());
        assert!(healthcheck_probe(&client, &url).await.is_ok());
    }

    #[tokio::test]
    async fn healthcheck_probe_fails_on_non_2xx_status() {
        let addr = spawn_fixed_status_server(axum::http::StatusCode::SERVICE_UNAVAILABLE).await;
        let client = reqwest::Client::new();
        let url = healthcheck_url(addr.port());
        let err = healthcheck_probe(&client, &url).await.unwrap_err();
        assert!(err.contains("503"));
    }

    #[tokio::test]
    async fn healthcheck_probe_fails_on_connection_error() {
        // Port 0 is never a live listener to connect to.
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_millis(200))
            .build()
            .unwrap();
        let url = "http://127.0.0.1:1/healthz";
        assert!(healthcheck_probe(&client, url).await.is_err());
    }

    fn base_config() -> config::Config {
        let cli = crate::config::CliConfig::parse_from(["svc-ingest"]);
        config::Config {
            cli,
            twitch_irc_oauth_token: None,
            discord_bot_token: None,
            envelope_binding_keys: None,
        }
    }

    fn test_ingest_metrics() -> Arc<telemetry::IngestMetrics> {
        let registry = prometheus::Registry::new();
        Arc::new(telemetry::register_ingest_metrics(&registry))
    }

    #[tokio::test]
    async fn try_start_twitch_irc_noop_when_not_configured() {
        // No `tracing` subscriber installed in this test (see
        // `run.rs`'s own doc on why only one test per binary calls
        // `telemetry::init`) -- `tracing::info!`/`warn!` are harmless
        // no-ops without one.
        try_start_twitch_irc(&base_config(), test_ingest_metrics());
    }

    #[tokio::test]
    async fn try_start_discord_noop_when_not_configured() {
        try_start_discord(&base_config(), test_ingest_metrics());
    }

    #[tokio::test]
    async fn try_start_twitch_outbound_noop_when_not_configured() {
        try_start_twitch_outbound(&base_config());
    }

    #[tokio::test]
    async fn try_start_twitch_irc_noop_when_binding_keyring_missing() {
        let mut config = base_config();
        config.cli.twitch_irc_nick = "waddlebot".to_string();
        config.cli.twitch_irc_channel = "somechannel".to_string();
        config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
        // envelope_binding_keys stays None -> must not start.
        try_start_twitch_irc(&config, test_ingest_metrics());
    }

    #[tokio::test]
    async fn try_start_discord_noop_when_binding_active_kid_missing() {
        let mut config = base_config();
        config.discord_bot_token = Some(crate::config::Secret::new("test-token"));
        config.envelope_binding_keys = Some(crate::config::Secret::new("k1:aabbcc"));
        // binding_active_kid stays "" -> must not start.
        try_start_discord(&config, test_ingest_metrics());
    }

    #[tokio::test]
    async fn try_start_twitch_outbound_noop_when_nick_missing() {
        let mut config = base_config();
        config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
        // cli.twitch_irc_nick stays "" -> must not start.
        try_start_twitch_outbound(&config);
    }

    #[tokio::test]
    async fn try_start_twitch_irc_noop_when_spine_config_unavailable() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::remove_var("VALKEY_URL");
            std::env::remove_var("REDIS_URL");
        }
        let mut config = base_config();
        config.cli.twitch_irc_nick = "waddlebot".to_string();
        config.cli.twitch_irc_channel = "somechannel".to_string();
        config.cli.binding_active_kid = "k1".to_string();
        config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
        config.envelope_binding_keys = Some(crate::config::Secret::new(
            "k1:0102030405060708090a0b0c0d0e0f10",
        ));
        try_start_twitch_irc(&config, test_ingest_metrics());
    }

    #[tokio::test]
    async fn try_start_twitch_outbound_noop_when_spine_config_unavailable() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::remove_var("VALKEY_URL");
            std::env::remove_var("REDIS_URL");
        }
        let mut config = base_config();
        config.cli.twitch_irc_nick = "waddlebot".to_string();
        config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
        try_start_twitch_outbound(&config);
    }

    #[tokio::test]
    async fn try_start_twitch_irc_spawns_when_everything_is_valid() {
        // Mirrors `core/svc_process`'s own
        // `try_start_spine_drain_spawns_when_spine_config_is_valid`: a
        // syntactically valid, TLS-required URL plus a password satisfies
        // `SpineConfig::validate`, but nothing is listening on port 1 (a
        // privileged port, refused immediately) -- exercises the spawn
        // path end to end (including the connect-failure log line)
        // without a live Valkey. Guard is dropped before the `.await`
        // below (clippy `await_holding_lock`) -- the env vars only need to
        // be set for the duration of this synchronous call.
        //
        // `crypto::ensure_installed()` first, defense in depth: this test
        // triggers a real TLS-capable `SpineClient::connect` (which has
        // its own internal installation, per `Cargo.toml`'s comment) --
        // explicit here too so this test never depends on execution order
        // relative to the `license::tests` in the same `--lib` binary.
        crypto::ensure_installed();
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
                std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
            }
            let mut config = base_config();
            config.cli.twitch_irc_nick = "waddlebot".to_string();
            config.cli.twitch_irc_channel = "somechannel".to_string();
            config.cli.binding_active_kid = "k1".to_string();
            config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
            config.envelope_binding_keys = Some(crate::config::Secret::new(
                "k1:0102030405060708090a0b0c0d0e0f10",
            ));
            try_start_twitch_irc(&config, test_ingest_metrics());
            unsafe {
                std::env::remove_var("VALKEY_URL");
                std::env::remove_var("VALKEY_PASSWORD");
            }
        }
        // Real (not paused) sleep: lets the spawned tasks actually run on
        // this same current-thread test runtime and reach their
        // connect-failure log line before the runtime is torn down.
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }

    #[tokio::test]
    async fn try_start_discord_spawns_when_everything_is_valid() {
        crypto::ensure_installed();
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
                std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
            }
            let mut config = base_config();
            config.discord_bot_token = Some(crate::config::Secret::new("test-token"));
            config.cli.binding_active_kid = "k1".to_string();
            config.envelope_binding_keys = Some(crate::config::Secret::new(
                "k1:0102030405060708090a0b0c0d0e0f10",
            ));
            try_start_discord(&config, test_ingest_metrics());
            unsafe {
                std::env::remove_var("VALKEY_URL");
                std::env::remove_var("VALKEY_PASSWORD");
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }

    #[tokio::test]
    async fn try_start_twitch_outbound_spawns_when_everything_is_valid() {
        crypto::ensure_installed();
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
                std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
            }
            let mut config = base_config();
            config.cli.twitch_irc_nick = "waddlebot".to_string();
            config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
            try_start_twitch_outbound(&config);
            unsafe {
                std::env::remove_var("VALKEY_URL");
                std::env::remove_var("VALKEY_PASSWORD");
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }

    #[test]
    fn resolve_binding_keyring_none_when_active_kid_empty() {
        let config = base_config();
        assert!(resolve_binding_keyring(&config, "test").is_none());
    }

    #[test]
    fn resolve_binding_keyring_none_when_keys_unset() {
        let mut config = base_config();
        config.cli.binding_active_kid = "k1".to_string();
        assert!(resolve_binding_keyring(&config, "test").is_none());
    }

    #[test]
    fn resolve_binding_keyring_none_when_keys_malformed() {
        let mut config = base_config();
        config.cli.binding_active_kid = "k1".to_string();
        config.envelope_binding_keys = Some(crate::config::Secret::new("not-valid"));
        assert!(resolve_binding_keyring(&config, "test").is_none());
    }

    #[test]
    fn resolve_binding_keyring_some_when_valid() {
        let mut config = base_config();
        config.cli.binding_active_kid = "k1".to_string();
        config.envelope_binding_keys = Some(crate::config::Secret::new(
            "k1:0102030405060708090a0b0c0d0e0f10",
        ));
        assert!(resolve_binding_keyring(&config, "test").is_some());
    }
}
