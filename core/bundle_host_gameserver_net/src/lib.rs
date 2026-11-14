//! `gameserver-net`: the host side of the bundle `gameserver-net` WIT interface
//! (`wit/waddle-bundle/tool.wit`), the riskiest host capability so far -- it opens
//! sockets to customer-registered hosts on behalf of a sandboxed guest.
//!
//! Security model (every item has a test):
//!
//! * **No guest-chosen destination.** The guest names a connection by UUID only; the
//!   [`directory::ServerDirectory`] resolves it, scoped to the invocation's tenant AND
//!   community, to an admin-registered target. Anything else is `denied("not_registered")`.
//! * **SSRF guard on the RESOLVED address** ([`guard`]): DNS is resolved once, EVERY answer
//!   must pass `bundle_host_http`'s classifier (private, loopback, link-local, CGNAT,
//!   metadata, multicast, ULA, 6to4/Teredo, v4-embedded v6) plus this crate's extra reserved
//!   ranges, and the socket is connected to that exact IP -- never re-resolved (DNS rebinding).
//! * **The RCON password never enters the guest.** [`rcon`] performs the login host-side; the
//!   password lives in a zeroizing [`secret::Secret`] with a redacting `Debug` and appears in
//!   no error string, audit record or log.
//! * **Bounded everywhere**: per-connection and per-community rate limits, concurrent-session
//!   caps, connect/login/read deadlines, byte budgets in and out, an RCON command length cap
//!   and single-printable-line rule enforced on the wire bytes (the bundle's allowlist stays
//!   bundle-side; the host only caps).
//! * **Full audit** ([`audit`]): connection id, command text, sizes, outcome; never the password,
//!   host or IP.
//! * **Fail closed**: every refusal is an explicit [`error::NetError`].

pub mod audit;
pub mod config;
pub mod directory;
pub mod error;
pub mod guard;
mod limits;
mod metrics;
pub mod net;
pub mod rcon;
pub mod secret;

pub use audit::{AuditEvent, AuditSink, TracingAudit};
pub use config::GameNetConfig;
pub use directory::{HubServerDirectory, Scope, ServerDirectory, ServerTarget};
pub use error::NetError;
pub use net::{GameNet, Scheme, Session};
pub use secret::Secret;
