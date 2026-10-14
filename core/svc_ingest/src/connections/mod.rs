//! Dynamic Twitch connections: resolve the IRC channels + OAuth tokens
//! `svc_ingest` should run from hub-api's connection store instead of static
//! environment variables.
//!
//! Gated behind the `waddles.ingest-dynamic-connections` feature flag (default
//! OFF -- see `crate::license::dynamic_connections_enabled`); with the flag OFF
//! the static `TWITCH_IRC_*` path (`crate::try_start_twitch_irc`) runs
//! unchanged.
//!
//! ```text
//! hub-api  GET  /api/v1/internal/provider-connections?provider=twitch&capability=chat_ingest
//!          POST /api/v1/internal/communities/{c}/provider-connections/{id}/resolve
//!             ^   (machine JWT, scope connections:resolve, tenant claim = system)
//!             |
//!   client.rs  HubConnectionsClient -- typed DTOs (dto.rs), cursor paging,
//!             |    409/404 => drop channel, 5xx/429/transport => retry w/ backoff
//!             v
//!   supervisor.rs  poll -> group by bot account -> spawn/update/stop bots
//!             |
//!             v
//!   bot.rs  ONE IRC socket per bot, `JOIN #a,#b,#c`, re-resolve every
//!           (re)connect, route PRIVMSG -> spine under the binding's Scope
//! ```
//!
//! # Security
//!
//! The resolve response is the only secret-bearing payload; the decrypted
//! access token lives in [`dto::AccessToken`] (redacted `Debug`/`Display`),
//! reaches the IRC `PASS` line and nothing else, and is never logged, traced,
//! or used as a metric label. hub-api owns refresh -- this crate never
//! refreshes and never caches a token past a reconnect.
//!
//! # Observability
//!
//! Logs (numeric ids, counts and a hashed bot reference only), traces
//! (`hubconn.list`/`hubconn.resolve`/`hubconn.reconcile` spans), and Prometheus
//! metrics ([`metrics::ConnectionsMetrics`]): request-latency histograms,
//! channels-joined/bots-running gauges, and 409/refresh/skip/reconnect counters.

pub mod bot;
pub mod client;
pub mod dto;
pub mod metrics;
pub mod supervisor;
