//! Thin observing wrappers the supervisor puts around the invoker and the
//! transports: they delegate every call unchanged and only record readiness
//! facts (`pinned_ok`, `live`), pace connects and release the start permit.

use std::sync::Arc;
use std::time::Duration;

use prometheus::IntGauge;
use tokio::sync::{Mutex, OwnedSemaphorePermit};
use tokio::time::Instant;

use super::ready::RuntimeStatus;
use crate::connector::secrets::{Destination, ResolvedAuth};
use crate::connector::webhook::{BoxFut, SecretError};
use crate::connector::{
    ConnectionCtx, ConnectorEvent, ConnectorInvokeError, ConnectorInvoker, Handshake, RawFrame,
    Transport, TransportError, TransportSecretSource,
};
use connection_resolver::ConnectionRef;

/// Wraps the real invoker; an invocation the executor ANSWERED (success or a
/// per-frame guest error) proves the pinned digest is live for the platform.
pub struct ObservedInvoker<I: ConnectorInvoker> {
    inner: Arc<I>,
    status: Arc<RuntimeStatus>,
}

impl<I: ConnectorInvoker> ObservedInvoker<I> {
    /// Wraps `inner`, reporting answers to `status`.
    pub fn new(inner: Arc<I>, status: Arc<RuntimeStatus>) -> Self {
        Self { inner, status }
    }

    fn observe<T>(&self, platform: &str, r: &Result<T, ConnectorInvokeError>) {
        if matches!(r, Ok(_) | Err(ConnectorInvokeError::Guest(_))) {
            self.status.mark_pinned(platform);
        }
    }
}

impl<I: ConnectorInvoker> ConnectorInvoker for ObservedInvoker<I> {
    async fn on_connect(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        let r = self.inner.on_connect(platform, ctx).await;
        self.observe(platform, &r);
        r
    }

    async fn on_frame(
        &self,
        platform: &str,
        frame: RawFrame,
    ) -> Result<Vec<ConnectorEvent>, ConnectorInvokeError> {
        let r = self.inner.on_frame(platform, frame).await;
        self.observe(platform, &r);
        r
    }

    async fn on_heartbeat_due(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError> {
        let r = self.inner.on_heartbeat_due(platform, ctx).await;
        self.observe(platform, &r);
        r
    }

    async fn on_disconnect(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
        reason: &str,
    ) -> Result<(), ConnectorInvokeError> {
        let r = self.inner.on_disconnect(platform, ctx, reason).await;
        self.observe(platform, &r);
        r
    }
}

/// Enforces a minimum gap between connect attempts of one platform (Discord
/// IDENTIFY pacing while the Valkey identify budget is deferred).
pub struct Pacer {
    gap: Duration,
    next: Mutex<Instant>,
}

impl Pacer {
    /// A pacer admitting one connect per `gap`.
    #[must_use]
    pub fn new(gap: Duration) -> Self {
        Self {
            gap,
            next: Mutex::new(Instant::now()),
        }
    }

    /// Waits until the next slot, then reserves the following one. The lock is
    /// held across the sleep so waiters are served strictly one gap apart.
    pub async fn wait(&self) {
        let mut next = self.next.lock().await;
        let now = Instant::now();
        if *next > now {
            tokio::time::sleep_until(*next).await;
        }
        *next = Instant::now().max(*next) + self.gap;
    }
}

/// Wraps a transport: paces connects, releases the start permit after the
/// first connect outcome, and reports `live` (established past handshake).
pub struct ObservedTransport<T: Transport> {
    inner: T,
    status: Arc<RuntimeStatus>,
    gauge: IntGauge,
    platform: &'static str,
    pacer: Option<Arc<Pacer>>,
    permit: Option<OwnedSemaphorePermit>,
    live: bool,
}

impl<T: Transport> ObservedTransport<T> {
    /// Wraps `inner` for `platform`; `permit` is dropped on the first connect
    /// outcome so a failing connect cannot hold a start slot forever.
    pub fn new(
        inner: T,
        status: Arc<RuntimeStatus>,
        gauge: IntGauge,
        platform: &'static str,
        pacer: Option<Arc<Pacer>>,
        permit: Option<OwnedSemaphorePermit>,
    ) -> Self {
        Self {
            inner,
            status,
            gauge,
            platform,
            pacer,
            permit,
            live: false,
        }
    }

    fn set_live(&mut self, live: bool) {
        if self.live == live {
            return;
        }
        self.live = live;
        let n = if live {
            self.status.live_inc(self.platform)
        } else {
            self.status.live_dec(self.platform)
        };
        self.gauge.set(i64::from(n));
    }
}

impl<T: Transport> Drop for ObservedTransport<T> {
    fn drop(&mut self) {
        self.set_live(false);
    }
}

impl<T: Transport> Transport for ObservedTransport<T> {
    fn platform(&self) -> &str {
        self.inner.platform()
    }

    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        if let Some(p) = &self.pacer {
            p.wait().await;
        }
        let r = self.inner.connect().await;
        if r.is_err() {
            self.permit = None;
        }
        r
    }

    async fn send_handshake(&mut self, handshake: Handshake) -> Result<(), TransportError> {
        let r = self.inner.send_handshake(handshake).await;
        self.permit = None;
        if r.is_ok() {
            self.set_live(true);
        }
        r
    }

    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        let r = self.inner.next_frame().await;
        if !matches!(r, Ok(Some(_))) {
            self.set_live(false);
        }
        r
    }

    fn heartbeat_interval(&self) -> Option<Duration> {
        self.inner.heartbeat_interval()
    }

    async fn send_heartbeat(&mut self, bytes: Vec<u8>) -> Result<(), TransportError> {
        self.inner.send_heartbeat(bytes).await
    }

    async fn send_default_heartbeat(&mut self) -> Result<(), TransportError> {
        self.inner.send_default_heartbeat().await
    }

    fn handshake_after_first_frame(&self) -> Option<Duration> {
        self.inner.handshake_after_first_frame()
    }

    fn first_heartbeat_jitter(&self) -> bool {
        self.inner.first_heartbeat_jitter()
    }

    async fn close(&mut self, reason: &str) {
        self.set_live(false);
        self.inner.close(reason).await;
    }

    async fn on_frame_outcome(&mut self, outcome: crate::connector::FrameOutcome) {
        self.inner.on_frame_outcome(outcome).await;
    }
}

/// Lets a per-connection `Arc<dyn TransportSecretSource>` be used where the
/// transports want an owned, sized secret source.
#[derive(Clone)]
pub struct SharedSecrets(pub Arc<dyn TransportSecretSource>);

impl TransportSecretSource for SharedSecrets {
    fn resolve(
        &self,
        requested: ConnectionRef,
        dest: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        self.0.resolve(requested, dest)
    }
}

#[cfg(test)]
mod tests;
