//! The only network surface: a plaintext `grpc.health.v1` listener for native kubelet gRPC
//! probes. It carries no data, no other service and no REST. The consumer publishes its
//! readiness through [`Ready`]; a flag-OFF consumer reports ready (idle but healthy).

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tonic_health::pb::health_check_response::ServingStatus;
use tonic_health::pb::health_client::HealthClient;
use tonic_health::pb::HealthCheckRequest;

/// Shared readiness bit.
#[derive(Clone)]
pub struct Ready(Arc<AtomicBool>);

impl Ready {
    /// New, initially ready.
    pub fn new() -> Self {
        Self(Arc::new(AtomicBool::new(true)))
    }
    /// Sets readiness.
    pub fn set(&self, v: bool) {
        self.0.store(v, Ordering::Relaxed);
    }
    /// Current readiness.
    pub fn get(&self) -> bool {
        self.0.load(Ordering::Relaxed)
    }
}

impl Default for Ready {
    fn default() -> Self {
        Self::new()
    }
}

/// Serves `grpc.health.v1` on `addr` until `shutdown` resolves.
///
/// # Errors
/// Bind/serve failure.
pub async fn serve(
    addr: SocketAddr,
    ready: Ready,
    shutdown: impl std::future::Future<Output = ()> + Send + 'static,
) -> Result<(), tonic::transport::Error> {
    let (reporter, service) = tonic_health::server::health_reporter();
    let mirror = tokio::spawn(async move {
        loop {
            let status = if ready.get() {
                tonic_health::ServingStatus::Serving
            } else {
                tonic_health::ServingStatus::NotServing
            };
            reporter.set_service_status("", status).await;
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    });
    let res = tonic::transport::Server::builder()
        .add_service(service)
        .serve_with_shutdown(addr, shutdown)
        .await;
    mirror.abort();
    res
}

/// Container healthcheck: one `Check` against `addr`; `Ok` only when SERVING.
///
/// # Errors
/// Connect failure, RPC failure or a non-SERVING status.
pub async fn check(addr: &str) -> anyhow::Result<()> {
    let ch = tonic::transport::Endpoint::from_shared(format!("http://{addr}"))?
        .timeout(Duration::from_secs(2))
        .connect_timeout(Duration::from_secs(2))
        .connect()
        .await?;
    let resp = HealthClient::new(ch)
        .check(HealthCheckRequest {
            service: String::new(),
        })
        .await?;
    if resp.into_inner().status == ServingStatus::Serving as i32 {
        Ok(())
    } else {
        anyhow::bail!("not serving")
    }
}
