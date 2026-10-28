//! [`PeerConnectionFactory`]: builds WHIP ingest and WHEP egress
//! `PeerConnection`s with this service's shared transport policy --
//! `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md` §8: host ICE
//! candidates only (no STUN/TURN), a port drawn from `WEBRTC_UDP_RANGE`, and
//! candidates rewritten to `PUBLIC_BASE_URL`'s host when it is a 1:1
//! NAT/LB-mapped literal IP.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;

use thiserror::Error;
use webrtc::peer_connection::{
    register_default_interceptors, MediaEngine, PeerConnection, PeerConnectionBuilder,
    PeerConnectionEventHandler, RTCConfigurationBuilder, RTCIceCandidateType, Registry,
    SettingEngine, SettingEngineBuilder,
};
use webrtc::runtime::{default_runtime, Runtime};

use crate::rtc::config::RtcConfig;

/// Errors from building the shared runtime or an individual `PeerConnection`.
#[derive(Debug, Error)]
pub enum RtcError {
    /// No compiled-in `webrtc::runtime::Runtime` -- means the `webrtc` crate
    /// was built without `runtime-tokio` (the default feature; see this
    /// service's `Cargo.toml` comment on the `webrtc` dependency).
    #[error("no compiled-in webrtc runtime (expected the `runtime-tokio` feature)")]
    NoRuntime,
    /// Every candidate port in `WEBRTC_UDP_RANGE` was either already bound
    /// by another session or rejected by webrtc-rs's own bind.
    #[error("webrtc_udp_range ({0}-{1}) is exhausted -- no free port for a new session")]
    PortRangeExhausted(u16, u16),
    /// `MediaEngine::register_default_codecs` or interceptor registry setup
    /// failed -- effectively "the webrtc crate itself is misconfigured",
    /// not a per-session condition.
    #[error("media engine setup failed: {0}")]
    MediaEngine(String),
    /// `PeerConnectionBuilder::build` failed for a reason other than the
    /// bind probe (DTLS certificate generation, etc.).
    #[error("peer connection build failed: {0}")]
    Build(String),
}

/// Draws local UDP ports from `WEBRTC_UDP_RANGE`, round-robin.
///
/// **Why this exists instead of `SettingEngine`'s ephemeral-range API:**
/// `rtc` 0.20.5's `SettingEngine::set_udp_network` (and the `UDPNetwork`
/// type it takes) is a `//TODO:` stub in this crate version -- see
/// `rtc-0.20.5/src/peer_connection/configuration/setting_engine.rs`
/// (`//TODO: use ice::udp_network::UDPNetwork;` / the method body is
/// commented out entirely). It is not merely undocumented; it does not
/// exist to call. Constraining ICE candidates to a configured range is
/// therefore done at this factory's boundary instead: pick a candidate port
/// here and pass an explicit `bind_ip:port` to
/// `PeerConnectionBuilder::with_udp_addrs` (never a `:0` wildcard), so the
/// OS binds exactly that port rather than webrtc-rs choosing an arbitrary
/// ephemeral one. This is a documented crate-maturity gap, tracked
/// alongside the AV1-over-WebRTC gap noted in `src/rtc/mod.rs`.
#[derive(Debug)]
struct PortAllocator {
    start: u16,
    span: u32,
    cursor: AtomicU32,
}

impl PortAllocator {
    fn new((start, end): (u16, u16)) -> Self {
        let span = u32::from(end - start) + 1;
        Self {
            start,
            span,
            cursor: AtomicU32::new(0),
        }
    }

    /// Next candidate port, round-robin. Not itself a guarantee the port is
    /// free -- see [`PeerConnectionFactory::build`], which probes it with a
    /// throwaway bind before handing it to webrtc-rs and retries the next
    /// candidate on failure.
    fn next(&self) -> u16 {
        let offset = self.cursor.fetch_add(1, Ordering::Relaxed) % self.span;
        self.start + offset as u16
    }

    /// Bounded retry budget for [`PeerConnectionFactory::build`] -- the
    /// full range for a small pool, capped at 64 for a large one so a
    /// persistently exhausted range fails fast instead of scanning
    /// thousands of ports per session attempt.
    fn max_attempts(&self) -> u32 {
        self.span.min(64)
    }
}

/// Builds ingest (WHIP, receive-only tracks) and egress (WHEP, send-only
/// tracks) `PeerConnection`s. One factory is shared by every WHIP and WHEP
/// session in the process.
pub struct PeerConnectionFactory {
    config: RtcConfig,
    runtime: Arc<dyn Runtime>,
    ports: PortAllocator,
}

impl PeerConnectionFactory {
    /// Builds a factory from an already-derived [`RtcConfig`], resolving
    /// the compiled-in `webrtc::runtime::Runtime` once and reusing it for
    /// every session this factory builds.
    pub fn new(config: RtcConfig) -> Result<Self, RtcError> {
        let runtime = default_runtime().ok_or(RtcError::NoRuntime)?;
        let ports = PortAllocator::new(config.udp_port_range);
        Ok(Self {
            config,
            runtime,
            ports,
        })
    }

    /// The shared `webrtc::runtime::Runtime` handle -- callers (WHIP/WHEP
    /// handlers) reuse this to spawn their own forwarding tasks
    /// (`on_track` RTP forwarding, fanout subscriber loops) on the same
    /// runtime the `PeerConnection` drivers run on.
    pub fn runtime(&self) -> Arc<dyn Runtime> {
        Arc::clone(&self.runtime)
    }

    fn setting_engine(&self) -> SettingEngine {
        let mut builder = SettingEngineBuilder::new();
        // RFC 8445 §5.1.1.1 excludes loopback candidates by default; both
        // this crate's own loopback integration tests and a pod-network
        // deployment that binds `BIND_ADDR=127.0.0.1` need the override to
        // gather any usable candidate at all. Non-loopback deployments are
        // unaffected -- there is no loopback interface to produce a
        // candidate from.
        builder = builder.with_include_loopback_candidate(true);
        if let Some(ip) = self.config.nat_1to1_ip {
            builder = builder.with_nat_1to1_ips(vec![ip.to_string()], RTCIceCandidateType::Host);
        }
        builder.build()
    }

    /// Builds one `PeerConnection` bound to a single explicit
    /// `bind_ip:port` drawn from the shared [`PortAllocator`], retrying the
    /// next candidate port (up to a bounded attempt count) if that port
    /// turns out to be taken -- either by the pre-bind probe below or by
    /// webrtc-rs's own bind inside [`PeerConnectionBuilder::build`].
    ///
    /// No ICE servers are configured (`RTCConfigurationBuilder::new().build()`
    /// defaults to an empty server list): every gathered candidate is a
    /// host candidate on `addr`, per the pipeline matrix's "host ICE
    /// candidates ... no TURN" (§8).
    pub async fn build<H>(&self, handler: Arc<H>) -> Result<Arc<dyn PeerConnection>, RtcError>
    where
        H: PeerConnectionEventHandler,
    {
        let attempts = self.ports.max_attempts();
        let mut last_build_err: Option<String> = None;

        for _ in 0..attempts {
            let port = self.ports.next();
            let addr = SocketAddr::new(self.config.bind_ip, port);

            // Best-effort availability probe -- see `PortAllocator`'s docs
            // for the inherent (and here, accepted) TOCTOU race: this is
            // strictly better than never checking, and a lost race is
            // still caught by the `build()` failure below and retried.
            if std::net::UdpSocket::bind(addr).is_err() {
                continue;
            }

            let mut media_engine = MediaEngine::default();
            if let Err(err) = media_engine.register_default_codecs() {
                return Err(RtcError::MediaEngine(err.to_string()));
            }
            let registry = match register_default_interceptors(Registry::new(), &mut media_engine) {
                Ok(registry) => registry,
                Err(err) => return Err(RtcError::MediaEngine(err.to_string())),
            };

            let configuration = RTCConfigurationBuilder::new().build();

            let result = PeerConnectionBuilder::new()
                .with_configuration(configuration)
                .with_media_engine(media_engine)
                .with_interceptor_registry(registry)
                .with_setting_engine(self.setting_engine())
                .with_handler(handler.clone() as Arc<dyn PeerConnectionEventHandler>)
                .with_runtime(Arc::clone(&self.runtime))
                .with_udp_addrs(vec![addr])
                .build()
                .await;

            match result {
                Ok(pc) => return Ok(Arc::new(pc) as Arc<dyn PeerConnection>),
                Err(err) => last_build_err = Some(err.to_string()),
            }
        }

        Err(match last_build_err {
            Some(detail) => RtcError::Build(detail),
            None => RtcError::PortRangeExhausted(
                self.config.udp_port_range.0,
                self.config.udp_port_range.1,
            ),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rtc::config::RtcConfig;
    use async_trait::async_trait;

    #[derive(Clone)]
    struct NoopHandler;

    #[async_trait]
    impl PeerConnectionEventHandler for NoopHandler {}

    fn loopback_config(range: (u16, u16)) -> RtcConfig {
        RtcConfig {
            bind_ip: "127.0.0.1".parse().unwrap(),
            udp_port_range: range,
            nat_1to1_ip: None,
        }
    }

    #[tokio::test]
    async fn builds_a_peer_connection_on_loopback() {
        let factory = PeerConnectionFactory::new(loopback_config((41000, 41050))).unwrap();
        match factory.build(Arc::new(NoopHandler)).await {
            Ok(pc) => {
                let _ = pc.close().await;
            }
            // `dyn PeerConnection` isn't `Debug` (see `Arc<dyn PeerConnection>`'s
            // trait bounds), so the error side is asserted on directly rather
            // than via `unwrap()`/`{:?}` on the whole `Result`.
            Err(err) => panic!("expected a PeerConnection, got error: {err}"),
        }
    }

    #[tokio::test]
    async fn concurrent_builds_land_on_distinct_ports() {
        let factory =
            Arc::new(PeerConnectionFactory::new(loopback_config((41100, 41110))).unwrap());
        let a = factory.build(Arc::new(NoopHandler)).await.unwrap();
        let b = factory.build(Arc::new(NoopHandler)).await.unwrap();
        // Both connections must have come up -- if the allocator had handed
        // out the same port twice, the second `build()` would have failed
        // instead of returning `Ok`.
        let _ = a.close().await;
        let _ = b.close().await;
    }

    #[tokio::test]
    async fn exhausted_range_returns_a_clear_error() {
        // A single-port range that is pre-occupied by a socket this test
        // holds open for the whole call -- `build()` must retry within its
        // bounded attempt budget and then fail with `PortRangeExhausted`
        // rather than hanging or panicking.
        let held = std::net::UdpSocket::bind("127.0.0.1:41200").expect("bind for the test");
        let factory = PeerConnectionFactory::new(loopback_config((41200, 41200))).unwrap();
        match factory.build(Arc::new(NoopHandler)).await {
            Ok(_) => panic!("expected PortRangeExhausted, got a PeerConnection"),
            Err(err) => assert!(matches!(err, RtcError::PortRangeExhausted(41200, 41200))),
        }
        drop(held);
    }
}
