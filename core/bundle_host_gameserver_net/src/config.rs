//! Limits and deadlines. Defaults are the contract in `bundles/rust/gameserver/README.md`.

use std::time::Duration;

/// Tunable limits for [`crate::GameNet`]. Every field is a hard cap, never a hint.
#[derive(Debug, Clone)]
pub struct GameNetConfig {
    /// TCP/UDP connect deadline (contract: 3 s).
    pub connect_timeout: Duration,
    /// Per-`read`/UDP-recv deadline and RCON login deadline (contract: 5 s).
    pub read_timeout: Duration,
    /// Max bytes a guest may read from an RCON session (contract: 64 KiB).
    pub rcon_max_in: usize,
    /// Max bytes a guest may read from a plain TCP session (MC status; contract: 128 KiB).
    pub tcp_max_in: usize,
    /// Max UDP response / ceiling for the guest's `max-response` (contract: 1400 B).
    pub udp_max_response: usize,
    /// Max UDP request payload.
    pub udp_max_request: usize,
    /// Max bytes a guest may write to one RCON session in total.
    pub rcon_max_out: usize,
    /// Max bytes a guest may write to one plain TCP session in total.
    pub tcp_max_out: usize,
    /// Max RCON command body length (bytes). The bundle caps at 256; the host never exceeds 512.
    pub max_command_len: usize,
    /// Session/exchange opens allowed per connection per minute.
    pub per_connection_per_min: u32,
    /// Session/exchange opens allowed per community per minute.
    pub per_community_per_min: u32,
    /// Concurrent open sessions allowed per community.
    pub max_concurrent_per_community: usize,
    /// Concurrent open sessions allowed process-wide.
    pub max_concurrent_global: usize,
    /// Directory (hub-api) lookup deadline.
    pub directory_timeout: Duration,
    /// TEST ONLY (feature `test-loopback`): permit loopback targets. Absent from release builds.
    #[cfg(feature = "test-loopback")]
    pub allow_loopback: bool,
}

impl Default for GameNetConfig {
    fn default() -> Self {
        Self {
            connect_timeout: Duration::from_secs(3),
            read_timeout: Duration::from_secs(5),
            rcon_max_in: 64 * 1024,
            tcp_max_in: 128 * 1024,
            udp_max_response: 1400,
            udp_max_request: 512,
            rcon_max_out: 4 * 1024,
            tcp_max_out: 1024,
            max_command_len: 512,
            per_connection_per_min: 30,
            per_community_per_min: 120,
            max_concurrent_per_community: 4,
            max_concurrent_global: 64,
            directory_timeout: Duration::from_secs(5),
            #[cfg(feature = "test-loopback")]
            allow_loopback: false,
        }
    }
}

impl GameNetConfig {
    /// Whether loopback targets are permitted (always `false` unless the test feature is on).
    pub(crate) fn loopback_allowed(&self) -> bool {
        #[cfg(feature = "test-loopback")]
        {
            self.allow_loopback
        }
        #[cfg(not(feature = "test-loopback"))]
        {
            false
        }
    }
}
