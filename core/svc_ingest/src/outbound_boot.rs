//! Boot wiring for the generic outbound executor (P6a, flag
//! `waddles.connector-tools`).
//!
//! ```text
//!  CONNECTOR_TOOLS_ENABLED (read once at boot, default OFF)
//!    OFF ──> legacy drains exactly as before      svc_ingest_outbound_path{path="legacy"}
//!    ON  ──> validate EVERY prerequisite (fatal, names the item, never a value)
//!            ├─ mTLS host-API listener  <── bundle executor dials in (ExecutorLink)
//!            ├─ SenderExecutor<WireTemplateBuilder, PlatformSlotRouter, DiscordDuties>
//!            └─ drains: twitch / discord / youtube   svc_ingest_outbound_path{path="executor"}
//! ```
//!
//! The pinned connector digests (`SENDER_EXEC_DIGEST_<PLATFORM>`), the pin keys
//! and every endpoint come from the HOST's environment only -- never the queue
//! entry, never the guest. With the flag ON a missing or malformed item is a
//! startup error ([`BootError`]); there is no silent fallback to the legacy
//! drains.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, LazyLock};

use crate::config::CliConfig;
use crate::connector::host_api_listener::{self, HostApiTls};
use crate::connector::sender_exec::discord_duties::DiscordDuties;
use crate::connector::sender_exec::discord_slots::DiscordSlots;
use crate::connector::sender_exec::kick_slots::KickSlots;
use crate::connector::sender_exec::twitch_slots::TwitchSlots;
use crate::connector::sender_exec::youtube_slots::YoutubeSlots;
use crate::connector::sender_exec::{
    ExecutorConfig, FlagGate, PlatformSlotRouter, SenderExecutor, WireTemplateBuilder,
};
use crate::connector::{ConnectorRegistry, ExecutorLink, PinnedConnector};
use crate::outbound_creds::ResolverTwitchCredentials;
use crate::outbound_creds_discord::ResolverDiscordCredentials;
use crate::outbound_creds_kick::ResolverKickCredentials;
use crate::outbound_creds_youtube::ResolverYoutubeCredentials;
use crate::outbound_pin::PinKeys;
use crate::platform_users::HubPlatformUserResolver;

/// Platforms the executor serves, in start order.
pub const PLATFORMS: [&str; 3] = ["twitch", "discord", "youtube"];

/// Kick is opt-in per deployment: it is served only when `SENDER_EXEC_DIGEST_KICK` is set (the
/// Kick connector is built and pinned), so a deployment without it boots exactly as before.
pub const KICK: &str = "kick";
const KICK_DIGEST_ENV: &str = "SENDER_EXEC_DIGEST_KICK";

/// The connector cutover flags the executor path answers `true` to over the
/// host-API link: each platform connector refuses `sender.build-request` while
/// its own flag reads OFF, and using the connector for outbound is exactly what
/// `waddles.connector-tools` ON decides.
pub const CONNECTOR_FLAGS: [&str; 4] = [
    "waddles.connector-twitch",
    "waddles.connector-discord",
    "waddles.connector-youtube",
    "waddles.connector-kick",
];

/// Default host-API listener port (the executor dials this).
pub const DEFAULT_HOST_API_PORT: u16 = 8301;

/// Core `app_id` of each platform's connector (host constants, not config: the
/// connector world is core-namespace only and the digest pins the content).
fn app_id(platform: &str) -> Option<&'static str> {
    match platform {
        "twitch" => Some("waddles.core.service.twitch"),
        "discord" => Some("waddles.core.service.discord"),
        "youtube" => Some("waddles.core.service.youtube"),
        "kick" => Some("waddles.core.service.kick"),
        _ => None,
    }
}

/// Frame ceiling registered for a build-request connector (the guest returns a
/// template, never a platform frame; the executor bounds the template itself).
const CONNECTOR_MAX_FRAME_BYTES: usize = 64 * 1024;

/// Which outbound implementation boot starts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OutboundMode {
    /// The legacy per-platform drains (`RealIrcOutbound`, `discord_rest`).
    Legacy,
    /// The generic executor path.
    Executor,
}

/// The mode the startup flag selects. Pure: the flag is read once from env at
/// config load, so the answer is cached for the process lifetime.
#[must_use]
pub fn mode(cli: &CliConfig) -> OutboundMode {
    if cli.connector_tools {
        OutboundMode::Executor
    } else {
        OutboundMode::Legacy
    }
}

static OUTBOUND_PATH: LazyLock<prometheus::IntGaugeVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntGaugeVec::new(
        prometheus::Opts::new(
            "svc_ingest_outbound_path",
            "1 for the outbound path that was started for a platform (path=legacy|executor), 0 for the other",
        ),
        &["path", "platform"],
    )
    .expect("valid metric definition")
});

/// Registers the path gauge on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(OUTBOUND_PATH.clone()))
}

fn platform_label(platform: &str) -> &'static str {
    match platform {
        "twitch" => "twitch",
        "discord" => "discord",
        "youtube" => "youtube",
        "kick" => "kick",
        _ => "other",
    }
}

/// Records that `mode` was started for `platform` (and the other path was not),
/// with an INFO log naming only the two bounded labels.
pub fn record_path(mode: OutboundMode, platform: &str) {
    let platform = platform_label(platform);
    let (on, off) = match mode {
        OutboundMode::Legacy => ("legacy", "executor"),
        OutboundMode::Executor => ("executor", "legacy"),
    };
    OUTBOUND_PATH.with_label_values(&[on, platform]).set(1);
    OUTBOUND_PATH.with_label_values(&[off, platform]).set(0);
    tracing::info!(path = on, platform, "outbound path started");
}

/// Current gauge value (test/observability helper).
#[must_use]
pub fn path_value(path: &str, platform: &str) -> i64 {
    OUTBOUND_PATH.with_label_values(&[path, platform]).get()
}

/// Why the executor path cannot start. `Display` names the setting only --
/// never a value, a path's content or a secret.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum BootError {
    /// A required setting is unset or empty.
    #[error("waddles.connector-tools is ON but {0} is not set")]
    Missing(&'static str),
    /// A setting is present but unusable.
    #[error("waddles.connector-tools is ON but {0} is malformed")]
    Malformed(&'static str),
    /// A companion flag the executor path depends on is OFF.
    #[error("waddles.connector-tools is ON but {0} is OFF; the executor resolves per-community credentials")]
    Prerequisite(&'static str),
}

/// Everything the executor path needs, validated.
#[derive(Debug)]
pub struct ExecBootConfig {
    pin: PinKeys,
    digests: HashMap<String, String>,
    twitch_client_id: String,
    hub_grpc_endpoint: String,
    hub_grpc_ca: Option<String>,
    tls: HostApiTls,
    host_api_addr: SocketAddr,
}

fn required(
    lookup: &dyn Fn(&str) -> Option<String>,
    name: &'static str,
) -> Result<String, BootError> {
    lookup(name)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .ok_or(BootError::Missing(name))
}

/// The host-API listener's mTLS material and bind address from `lookup`
/// (shared by the outbound executor and the inbound connector runtime: ONE
/// listener serves the single executor session).
///
/// # Errors
/// The first missing/malformed item, named without values.
pub fn host_api_from_lookup(
    cli: &CliConfig,
    lookup: &dyn Fn(&str) -> Option<String>,
) -> Result<(HostApiTls, SocketAddr), BootError> {
    let tls = HostApiTls {
        server_cert: PathBuf::from(required(lookup, "HOST_API_SERVER_CERT_FILE")?),
        server_key: PathBuf::from(required(lookup, "HOST_API_SERVER_KEY_FILE")?),
        client_ca: PathBuf::from(required(lookup, "HOST_API_CLIENT_CA_FILE")?),
        client_identity: required(lookup, "HOST_API_CLIENT_IDENTITY")?,
    };
    let port = match lookup("SENDER_EXEC_HOST_API_PORT")
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
    {
        None => DEFAULT_HOST_API_PORT,
        Some(v) => v
            .parse::<u16>()
            .ok()
            .filter(|p| *p != 0)
            .ok_or(BootError::Malformed("SENDER_EXEC_HOST_API_PORT"))?,
    };
    Ok((tls, SocketAddr::new(cli.bind_addr, port)))
}

impl ExecBootConfig {
    /// Validates the executor prerequisites from `cli` and `lookup` (env in
    /// production).
    ///
    /// # Errors
    /// The first [`BootError`] found, in a fixed order.
    pub fn from_lookup(
        cli: &CliConfig,
        lookup: &dyn Fn(&str) -> Option<String>,
    ) -> Result<Self, BootError> {
        if !cli.dynamic_connections {
            return Err(BootError::Prerequisite("INGEST_DYNAMIC_CONNECTIONS"));
        }
        let pin = PinKeys::parse(
            &required(lookup, "OUTBOUND_PIN_KEYS")?,
            Some(&required(lookup, "OUTBOUND_PIN_ACTIVE_KID")?),
        )
        .map_err(|_| BootError::Malformed("OUTBOUND_PIN_KEYS/OUTBOUND_PIN_ACTIVE_KID"))?;

        let mut digests = HashMap::new();
        for platform in PLATFORMS {
            let name: &'static str = match platform {
                "twitch" => "SENDER_EXEC_DIGEST_TWITCH",
                "discord" => "SENDER_EXEC_DIGEST_DISCORD",
                _ => "SENDER_EXEC_DIGEST_YOUTUBE",
            };
            let digest = required(lookup, name)?;
            ExecutorConfig::production(HashMap::from([(platform.to_string(), digest.clone())]))
                .map_err(|_| BootError::Malformed(name))?;
            digests.insert(platform.to_string(), digest);
        }
        // Optional Kick: unset = not served; set-but-blank or malformed is fatal, never ignored.
        if lookup(KICK_DIGEST_ENV).is_some() {
            let digest = required(lookup, KICK_DIGEST_ENV)?;
            ExecutorConfig::production(HashMap::from([(KICK.to_string(), digest.clone())]))
                .map_err(|_| BootError::Malformed(KICK_DIGEST_ENV))?;
            digests.insert(KICK.to_string(), digest);
        }

        let twitch_client_id = required(lookup, "TWITCH_CLIENT_ID")?;
        let hub_grpc_endpoint = required(lookup, "HUB_API_GRPC_ENDPOINT")?;
        if cli.service_jwt_token_endpoint.trim().is_empty() {
            return Err(BootError::Missing("SERVICE_JWT_TOKEN_ENDPOINT"));
        }
        let hub_grpc_ca = lookup("HUB_API_GRPC_CA_FILE")
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty());

        let (tls, host_api_addr) = host_api_from_lookup(cli, lookup)?;
        Ok(Self {
            pin,
            digests,
            twitch_client_id,
            hub_grpc_endpoint,
            hub_grpc_ca,
            tls,
            host_api_addr,
        })
    }

    /// The pinned digest of `platform`'s connector.
    #[must_use]
    pub fn digest(&self, platform: &str) -> Option<&str> {
        self.digests.get(platform).map(String::as_str)
    }

    /// The platforms this deployment serves: the core three plus Kick when its digest is pinned.
    #[must_use]
    pub fn served_platforms(&self) -> Vec<&'static str> {
        let mut v = PLATFORMS.to_vec();
        if self.digests.contains_key(KICK) {
            v.push(KICK);
        }
        v
    }

    /// The listener address the executor dials.
    #[must_use]
    pub fn host_api_addr(&self) -> SocketAddr {
        self.host_api_addr
    }
}

/// The boot-time cached flag value: the decision was made once at startup
/// (`CONNECTOR_TOOLS_ENABLED`), so the executor's per-send gate agrees with
/// the path boot chose and never re-reads anything.
#[derive(Clone, Copy, Debug)]
pub struct CachedFlagGate(pub bool);

impl FlagGate for CachedFlagGate {
    async fn enabled(&self, _flag: &str) -> bool {
        self.0
    }
}

type Resolver = connection_resolver::ConnectionResolver;
/// The production per-platform slot router.
pub type ProdSlots = PlatformSlotRouter<
    TwitchSlots<ResolverTwitchCredentials<Resolver>>,
    DiscordSlots<ResolverDiscordCredentials<Resolver>>,
    YoutubeSlots<ResolverYoutubeCredentials<Resolver>>,
    KickSlots<ResolverKickCredentials<Resolver>>,
>;
/// The production Discord host duties.
pub type ProdDuties = DiscordDuties<ResolverDiscordCredentials<Resolver>, HubPlatformUserResolver>;
/// The production executor.
pub type ProdExecutor = SenderExecutor<WireTemplateBuilder, ProdSlots, CachedFlagGate, ProdDuties>;

fn resolver(cli: &CliConfig) -> anyhow::Result<Resolver> {
    connection_resolver::ConnectionResolver::new(
        connection_resolver::ResolverConfig::new(
            cli.hub_api_url.clone(),
            cli.environment.clone(),
            cli.service_jwt_token_endpoint.clone(),
            cli.service_jwt_sa_token_path.clone(),
        ),
        Arc::new(crate::twitch_sessions::boot::ResolverTelemetry),
    )
    .map_err(|e| anyhow::anyhow!("connection resolver unavailable: {e}"))
}

/// The registry of the three pinned connectors (digests from host config).
fn registry(cfg: &ExecBootConfig) -> anyhow::Result<ConnectorRegistry> {
    let mut registry = ConnectorRegistry::new();
    for platform in cfg.served_platforms() {
        let (Some(app), Some(digest)) = (app_id(platform), cfg.digest(platform)) else {
            anyhow::bail!("no pinned connector for {platform}");
        };
        registry
            .register(PinnedConnector::new(
                platform,
                app,
                digest,
                CONNECTOR_MAX_FRAME_BYTES,
            ))
            .map_err(|e| anyhow::anyhow!("connector registry: {e}"))?;
    }
    Ok(registry)
}

/// The executor plus the link the listener feeds.
pub struct ExecParts {
    /// The executor shared by all three drains.
    pub executor: Arc<ProdExecutor>,
    /// svc_ingest's side of the executor host-API connection.
    pub link: Arc<ExecutorLink>,
}

/// Builds the executor from validated config. Network-facing (`hub-api` gRPC
/// connect); a failure is fatal to the caller.
///
/// # Errors
/// Any construction failure, named without values.
pub async fn build(cli: &CliConfig, cfg: &ExecBootConfig) -> anyhow::Result<ExecParts> {
    build_shared(cli, cfg, None).await
}

/// [`build`] over an already-built executor link (`Some`): the inbound
/// connector runtime owns the single executor session and its listener, and the
/// outbound path rides the same link (its build-request flag reads are served by
/// the link's host-call handler, see `CallOrigin::BuildRequest`).
///
/// # Errors
/// Any construction failure, named without values.
pub async fn build_shared(
    cli: &CliConfig,
    cfg: &ExecBootConfig,
    shared_link: Option<Arc<ExecutorLink>>,
) -> anyhow::Result<ExecParts> {
    let users = HubPlatformUserResolver::connect(
        cfg.hub_grpc_endpoint.clone(),
        cli.service_jwt_token_endpoint.clone(),
        cli.service_jwt_sa_token_path.clone(),
        cfg.hub_grpc_ca.as_deref(),
    )
    .await
    .map_err(|e| anyhow::anyhow!("hub-api platform-user resolver unavailable: {e}"))?;
    build_with_link(cli, cfg, users, shared_link)
}

/// [`build`] with the tokenization resolver supplied (the seam tests use).
///
/// # Errors
/// Any construction failure, named without values.
pub fn build_with(
    cli: &CliConfig,
    cfg: &ExecBootConfig,
    users: HubPlatformUserResolver,
) -> anyhow::Result<ExecParts> {
    build_with_link(cli, cfg, users, None)
}

/// [`build_with`] with an optional shared link (see [`build_shared`]).
///
/// # Errors
/// Any construction failure, named without values.
pub fn build_with_link(
    cli: &CliConfig,
    cfg: &ExecBootConfig,
    users: HubPlatformUserResolver,
    shared_link: Option<Arc<ExecutorLink>>,
) -> anyhow::Result<ExecParts> {
    let link = shared_link
        .unwrap_or_else(|| Arc::new(ExecutorLink::new().with_enabled_flags(CONNECTOR_FLAGS)));
    let builder = WireTemplateBuilder::new(Arc::clone(&link), Arc::new(registry(cfg)?));
    let slots = PlatformSlotRouter::new(
        TwitchSlots::new(
            ResolverTwitchCredentials::new(resolver(cli)?),
            cfg.twitch_client_id.clone(),
        ),
        DiscordSlots::new(ResolverDiscordCredentials::new(resolver(cli)?)),
        YoutubeSlots::new(ResolverYoutubeCredentials::new(resolver(cli)?)),
        KickSlots::new(ResolverKickCredentials::new(resolver(cli)?)),
    );
    let duties = DiscordDuties::new(ResolverDiscordCredentials::new(resolver(cli)?), users);
    let exec_cfg = ExecutorConfig::production(cfg.digests.clone())
        .map_err(|e| anyhow::anyhow!("executor config: {e}"))?;
    let executor =
        SenderExecutor::with_duties(builder, slots, CachedFlagGate(true), duties, exec_cfg);
    Ok(ExecParts {
        executor: Arc::new(executor),
        link,
    })
}

/// Validates, builds and starts the executor path: the mTLS listener and the
/// three drains, each ended by `shutdown` exactly like the legacy drains.
///
/// # Errors
/// Any prerequisite failure -- fatal to the process by design.
pub async fn start(
    cli: &CliConfig,
    shutdown: impl Fn() -> tokio::sync::oneshot::Receiver<()>,
) -> anyhow::Result<()> {
    start_shared(cli, shutdown, None).await
}

/// [`start`] riding an executor link (and listener) the inbound connector
/// runtime already owns: no second listener is bound.
///
/// # Errors
/// Any prerequisite failure -- fatal to the process by design.
pub async fn start_shared(
    cli: &CliConfig,
    shutdown: impl Fn() -> tokio::sync::oneshot::Receiver<()>,
    shared_link: Option<Arc<ExecutorLink>>,
) -> anyhow::Result<()> {
    let serve_listener = shared_link.is_none();
    let cfg = ExecBootConfig::from_lookup(cli, &|k| std::env::var(k).ok())?;
    crate::twitch_sessions::boot::validate_dynamic_prereqs(
        cli,
        crate::publish::scope_resolver_installed(),
    )?;
    let spine_cfg = penguin_spine::SpineConfig::from_env()
        .map_err(|e| anyhow::anyhow!("spine config unavailable for the outbound executor: {e}"))?;
    let parts = build_shared(cli, &cfg, shared_link).await?;
    launch_with(cfg, parts, spine_cfg, shutdown, serve_listener).await
}

/// Binds the mTLS listener and spawns it plus the three drains, recording the
/// `executor` path per platform. A bind or TLS-material failure is returned
/// BEFORE anything is spawned.
///
/// # Errors
/// TLS material unusable or the listener address cannot be bound.
pub async fn launch(
    cfg: ExecBootConfig,
    parts: ExecParts,
    spine_cfg: penguin_spine::SpineConfig,
    shutdown: impl Fn() -> tokio::sync::oneshot::Receiver<()>,
) -> anyhow::Result<()> {
    launch_with(cfg, parts, spine_cfg, shutdown, true).await
}

/// [`launch`] with the listener optional: `serve_listener = false` when the
/// inbound runtime already serves the (single) executor session.
///
/// # Errors
/// TLS material unusable or the listener address cannot be bound.
pub async fn launch_with(
    cfg: ExecBootConfig,
    parts: ExecParts,
    spine_cfg: penguin_spine::SpineConfig,
    shutdown: impl Fn() -> tokio::sync::oneshot::Receiver<()>,
    serve_listener: bool,
) -> anyhow::Result<()> {
    if serve_listener {
        let acceptor = host_api_listener::build_acceptor(&cfg.tls)?;
        let listener = tokio::net::TcpListener::bind(cfg.host_api_addr)
            .await
            .map_err(|e| anyhow::anyhow!("host-api listener bind failed: {e}"))?;
        tokio::spawn(host_api_listener::serve(
            listener,
            acceptor,
            Arc::clone(&parts.link),
            shutdown(),
        ));
        tracing::info!(addr = %cfg.host_api_addr, "outbound executor host-api listener started");
    }

    let pin = Arc::new(cfg.pin.clone());
    for platform in cfg.served_platforms() {
        let exec = Arc::clone(&parts.executor);
        let spine_cfg = spine_cfg.clone();
        let pin = Arc::clone(&pin);
        let rx = shutdown();
        tokio::spawn(async move {
            let pin = Some((*pin).clone());
            let res = match platform {
                "twitch" => crate::outbound::run_exec(&spine_cfg, &*exec, pin, rx).await,
                "discord" => crate::outbound::run_discord_exec(&spine_cfg, &*exec, pin, rx).await,
                "youtube" => crate::outbound::run_youtube_exec(&spine_cfg, &*exec, pin, rx).await,
                _ => crate::outbound::run_kick_exec(&spine_cfg, &*exec, pin, rx).await,
            };
            if let Err(err) = res {
                tracing::error!(platform, error = %err, "outbound executor drain exited");
            }
        });
        record_path(OutboundMode::Executor, platform);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    const KEYS: &str = "k1:00112233445566778899aabbccddeeff";
    fn d(c: char) -> String {
        format!("sha256:{}", c.to_string().repeat(64))
    }

    fn cli(dynamic: bool) -> CliConfig {
        let mut c = CliConfig::parse_from(["svc-ingest"]);
        c.dynamic_connections = dynamic;
        c.service_jwt_token_endpoint = "https://hub:50204".to_string();
        c
    }

    fn full() -> HashMap<&'static str, String> {
        HashMap::from([
            ("OUTBOUND_PIN_KEYS", KEYS.to_string()),
            ("OUTBOUND_PIN_ACTIVE_KID", "k1".to_string()),
            ("SENDER_EXEC_DIGEST_TWITCH", d('a')),
            ("SENDER_EXEC_DIGEST_DISCORD", d('b')),
            ("SENDER_EXEC_DIGEST_YOUTUBE", d('c')),
            ("TWITCH_CLIENT_ID", "cid".to_string()),
            ("HUB_API_GRPC_ENDPOINT", "https://hub:50204".to_string()),
            ("HOST_API_SERVER_CERT_FILE", "/tls/tls.crt".to_string()),
            ("HOST_API_SERVER_KEY_FILE", "/tls/tls.key".to_string()),
            ("HOST_API_CLIENT_CA_FILE", "/tls/ca.crt".to_string()),
            ("HOST_API_CLIENT_IDENTITY", "executor".to_string()),
        ])
    }

    fn load(
        cli: &CliConfig,
        env: &HashMap<&'static str, String>,
    ) -> Result<ExecBootConfig, BootError> {
        ExecBootConfig::from_lookup(cli, &|k| env.get(k).cloned())
    }

    #[test]
    fn kick_is_served_only_when_its_digest_is_pinned_and_a_bad_pin_is_fatal() {
        let core = ["twitch", "discord", "youtube"];
        let cfg = load(&cli(true), &full()).unwrap();
        assert_eq!(cfg.served_platforms(), core);
        assert_eq!(cfg.digest("kick"), None);

        let mut env = full();
        env.insert("SENDER_EXEC_DIGEST_KICK", d('d'));
        let cfg = load(&cli(true), &env).unwrap();
        assert_eq!(cfg.served_platforms(), [core[0], core[1], core[2], "kick"]);
        assert_eq!(cfg.digest("kick"), Some(d('d').as_str()));
        let reg = registry(&cfg).unwrap();
        assert_eq!(
            reg.resolve("kick").unwrap().app_id,
            "waddles.core.service.kick"
        );

        env.insert("SENDER_EXEC_DIGEST_KICK", "   ".to_string());
        assert_eq!(
            load(&cli(true), &env).unwrap_err(),
            BootError::Missing("SENDER_EXEC_DIGEST_KICK")
        );
        env.insert("SENDER_EXEC_DIGEST_KICK", "not-a-digest".to_string());
        assert_eq!(
            load(&cli(true), &env).unwrap_err(),
            BootError::Malformed("SENDER_EXEC_DIGEST_KICK")
        );
    }

    #[test]
    fn a_complete_environment_validates() {
        let cfg = load(&cli(true), &full()).unwrap();
        assert_eq!(cfg.digest("twitch"), Some(d('a').as_str()));
        assert_eq!(cfg.digest("youtube"), Some(d('c').as_str()));
        assert_eq!(cfg.digest("slack"), None);
        assert_eq!(cfg.host_api_addr().port(), DEFAULT_HOST_API_PORT);
    }

    #[test]
    fn every_required_item_missing_or_blank_is_fatal_and_named() {
        for name in [
            "OUTBOUND_PIN_KEYS",
            "OUTBOUND_PIN_ACTIVE_KID",
            "SENDER_EXEC_DIGEST_TWITCH",
            "SENDER_EXEC_DIGEST_DISCORD",
            "SENDER_EXEC_DIGEST_YOUTUBE",
            "TWITCH_CLIENT_ID",
            "HUB_API_GRPC_ENDPOINT",
            "HOST_API_SERVER_CERT_FILE",
            "HOST_API_SERVER_KEY_FILE",
            "HOST_API_CLIENT_CA_FILE",
            "HOST_API_CLIENT_IDENTITY",
        ] {
            let mut env = full();
            env.remove(name);
            assert_eq!(
                load(&cli(true), &env).unwrap_err(),
                BootError::Missing(name)
            );
            env.insert(name, "   ".to_string());
            assert_eq!(
                load(&cli(true), &env).unwrap_err(),
                BootError::Missing(name)
            );
        }
        let mut c = cli(true);
        c.service_jwt_token_endpoint = String::new();
        assert_eq!(
            load(&c, &full()).unwrap_err(),
            BootError::Missing("SERVICE_JWT_TOKEN_ENDPOINT")
        );
    }

    #[test]
    fn malformed_values_are_fatal_and_named() {
        for (name, bad, want) in [
            (
                "OUTBOUND_PIN_KEYS",
                "nonsense",
                "OUTBOUND_PIN_KEYS/OUTBOUND_PIN_ACTIVE_KID",
            ),
            (
                "OUTBOUND_PIN_ACTIVE_KID",
                "k9",
                "OUTBOUND_PIN_KEYS/OUTBOUND_PIN_ACTIVE_KID",
            ),
            (
                "SENDER_EXEC_DIGEST_TWITCH",
                "sha256:abc",
                "SENDER_EXEC_DIGEST_TWITCH",
            ),
            (
                "SENDER_EXEC_DIGEST_DISCORD",
                "latest",
                "SENDER_EXEC_DIGEST_DISCORD",
            ),
            (
                "SENDER_EXEC_DIGEST_YOUTUBE",
                "sha256:ZZ",
                "SENDER_EXEC_DIGEST_YOUTUBE",
            ),
            (
                "SENDER_EXEC_HOST_API_PORT",
                "0",
                "SENDER_EXEC_HOST_API_PORT",
            ),
            (
                "SENDER_EXEC_HOST_API_PORT",
                "http",
                "SENDER_EXEC_HOST_API_PORT",
            ),
        ] {
            let mut env = full();
            env.insert(name, bad.to_string());
            assert_eq!(
                load(&cli(true), &env).unwrap_err(),
                BootError::Malformed(want)
            );
        }
    }

    #[test]
    fn errors_never_echo_a_value() {
        let mut env = full();
        env.insert(
            "OUTBOUND_PIN_KEYS",
            "k1:SECRETSECRETSECRETSECRET".to_string(),
        );
        let msg = load(&cli(true), &env).unwrap_err().to_string();
        assert!(!msg.contains("SECRET"), "{msg}");
    }

    #[test]
    fn the_dynamic_connections_prerequisite_is_enforced_first() {
        assert_eq!(
            load(&cli(false), &full()).unwrap_err(),
            BootError::Prerequisite("INGEST_DYNAMIC_CONNECTIONS")
        );
    }

    #[test]
    fn a_custom_port_and_optional_ca_are_honoured() {
        let mut env = full();
        env.insert("SENDER_EXEC_HOST_API_PORT", "9443".to_string());
        env.insert("HUB_API_GRPC_CA_FILE", "/ca/hub.crt".to_string());
        let cfg = load(&cli(true), &env).unwrap();
        assert_eq!(cfg.host_api_addr().port(), 9443);
        assert_eq!(cfg.hub_grpc_ca.as_deref(), Some("/ca/hub.crt"));
    }

    #[test]
    fn the_flag_selects_the_path_and_defaults_to_legacy() {
        let mut c = CliConfig::parse_from(["svc-ingest"]);
        assert_eq!(mode(&c), OutboundMode::Legacy);
        c.connector_tools = true;
        assert_eq!(mode(&c), OutboundMode::Executor);
    }

    #[test]
    fn the_path_gauge_marks_exactly_one_path_per_platform() {
        // `youtube`: no other in-process test records it (the legacy drains
        // only record twitch/discord), so these absolutes cannot race.
        record_path(OutboundMode::Legacy, "youtube");
        assert_eq!(path_value("legacy", "youtube"), 1);
        assert_eq!(path_value("executor", "youtube"), 0);
        record_path(OutboundMode::Executor, "youtube");
        assert_eq!(path_value("legacy", "youtube"), 0);
        assert_eq!(path_value("executor", "youtube"), 1);
        // Unbounded input collapses to one label.
        record_path(OutboundMode::Executor, "x-attacker-1");
        record_path(OutboundMode::Executor, "x-attacker-2");
        assert_eq!(path_value("executor", "other"), 1);
    }

    #[test]
    fn the_registry_pins_all_served_connectors_from_host_config() {
        let mut env = full();
        env.insert("SENDER_EXEC_DIGEST_KICK", d('d'));
        let cfg = load(&cli(true), &env).unwrap();
        let reg = registry(&cfg).unwrap();
        for (p, digest) in [
            ("twitch", d('a')),
            ("discord", d('b')),
            ("youtube", d('c')),
            ("kick", d('d')),
        ] {
            let c = reg.resolve(p).unwrap();
            assert_eq!(c.digest, digest);
            assert_eq!(c.app_id, format!("waddles.core.service.{p}"));
        }
        assert!(reg.resolve("slack").is_err());
    }

    #[tokio::test]
    async fn the_cached_gate_returns_the_boot_decision() {
        assert!(
            CachedFlagGate(true)
                .enabled("waddles.connector-tools")
                .await
        );
        assert!(
            !CachedFlagGate(false)
                .enabled("waddles.connector-tools")
                .await
        );
    }

    #[tokio::test]
    async fn start_with_missing_prerequisites_fails_loud_before_binding_anything() {
        // The real process env has none of the executor settings.
        let mut c = cli(true);
        c.connector_tools = true;
        let err = start(&c, || tokio::sync::oneshot::channel().1)
            .await
            .unwrap_err();
        assert!(
            err.to_string().contains("waddles.connector-tools is ON"),
            "{err}"
        );
    }
}
