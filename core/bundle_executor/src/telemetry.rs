//! The executor's OTel metrics pipeline: builds the global meter provider
//! [`crate::metrics`] records into, and -- only when the operator opts in --
//! attaches an OTLP/gRPC periodic exporter to it.
//!
//! **Destination is configuration, never code.** The standard OTLP
//! environment variables decide everything (`rules/critical-rules.md`
//! Observability): `OTEL_EXPORTER_OTLP_ENDPOINT` /
//! `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL` (this
//! build speaks `grpc` only), `OTEL_EXPORTER_OTLP_HEADERS`,
//! `OTEL_EXPORTER_OTLP_TIMEOUT`, `OTEL_SERVICE_NAME`,
//! `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_METRIC_EXPORT_INTERVAL`. This module
//! resolves the endpoint (the metrics-specific variable wins over the generic
//! one) and decides *whether* to export; the OTLP crate resolves headers and
//! timeout from the same environment.
//!
//! **Export is OFF unless an endpoint variable is set.** The executor is a
//! credential-less, network-egress-restricted sandbox host whose only
//! sanctioned egress is the stage's host-API port and the bundle bucket
//! (`Cargo.toml`'s dependency-policy note and the pod's default-deny
//! `bundle-executor-networkpolicy.yaml`), so a default deployment must not open
//! a new path. Instruments still exist and
//! record when export is off (cheaply -- a provider with no reader keeps no
//! aggregation state); they are simply never read.
//!
//! **Telemetry never fails the process.** A bad endpoint, an unsupported
//! protocol or an exporter that cannot be built degrades to "not exporting"
//! with a WARN; a collector that is down just means batches are dropped by the
//! SDK's periodic reader.

use opentelemetry::global;
use opentelemetry_otlp::WithExportConfig;
use opentelemetry_sdk::metrics::SdkMeterProvider;
use opentelemetry_sdk::Resource;

/// Env var naming the OTLP endpoint for every signal.
const ENDPOINT_VAR: &str = "OTEL_EXPORTER_OTLP_ENDPOINT";
/// Signal-specific override of [`ENDPOINT_VAR`] for metrics.
const METRICS_ENDPOINT_VAR: &str = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT";
/// Env var overriding the resource `service.name`.
const SERVICE_NAME_VAR: &str = "OTEL_SERVICE_NAME";

/// What [`init_metrics_with`] decided to do with the environment it was given.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExportPlan {
    /// No endpoint variable is set: record, but export nowhere.
    Disabled,
    /// An endpoint variable is set: attach the OTLP/gRPC exporter pointed at
    /// `endpoint` (headers and timeout come from the standard variables).
    Otlp {
        /// The resolved collector endpoint.
        endpoint: String,
    },
}

/// Decides the [`ExportPlan`] from the two endpoint variables; the
/// metrics-specific one wins, per the OTLP exporter spec. A whitespace-only
/// value counts as unset, so an empty chart value cannot accidentally enable
/// export to `localhost:4317`.
pub fn export_plan(endpoint: Option<&str>, metrics_endpoint: Option<&str>) -> ExportPlan {
    fn usable(value: Option<&str>) -> Option<&str> {
        value.map(str::trim).filter(|s| !s.is_empty())
    }
    match usable(metrics_endpoint).or_else(|| usable(endpoint)) {
        Some(endpoint) => ExportPlan::Otlp {
            endpoint: endpoint.to_string(),
        },
        None => ExportPlan::Disabled,
    }
}

/// Flushes and shuts the meter provider down when dropped, so the final
/// export interval's data is not lost on an orderly stop. Holding it for the
/// life of `crate::run` is the whole contract.
pub struct MetricsGuard {
    provider: Option<SdkMeterProvider>,
}

impl MetricsGuard {
    /// Whether this guard owns a live provider (false only after
    /// [`MetricsGuard::shutdown`]).
    pub fn is_active(&self) -> bool {
        self.provider.is_some()
    }

    /// A meter on this guard's own provider -- lets a test record through
    /// exactly the pipeline it built, never the process-global one another
    /// concurrently running test may have replaced.
    #[cfg(test)]
    pub(crate) fn meter(&self) -> Option<opentelemetry::metrics::Meter> {
        use opentelemetry::metrics::MeterProvider;
        self.provider
            .as_ref()
            .map(|provider| provider.meter(crate::metrics::SCOPE))
    }

    /// Flushes and shuts the provider down; idempotent. A shutdown error is
    /// logged, never propagated -- stopping must not be able to fail.
    pub fn shutdown(&mut self) {
        if let Some(provider) = self.provider.take() {
            if let Err(err) = provider.shutdown() {
                tracing::warn!(error = %err, "otel meter provider shutdown error");
            }
        }
    }
}

impl Drop for MetricsGuard {
    fn drop(&mut self) {
        self.shutdown();
    }
}

/// Builds the meter provider from the process environment, installs it as the
/// global provider and returns its guard. Call once, early in `crate::run`,
/// and strictly before the first [`crate::metrics::executor_metrics`] use so
/// the instruments bind to this provider.
pub fn init_metrics(default_service_name: &str) -> MetricsGuard {
    init_metrics_with(default_service_name, |key| std::env::var(key).ok())
}

/// [`init_metrics`] with the environment injected as `env`, so tests can
/// exercise every branch without mutating process-global variables.
pub fn init_metrics_with(
    default_service_name: &str,
    env: impl Fn(&str) -> Option<String>,
) -> MetricsGuard {
    let service_name = env(SERVICE_NAME_VAR)
        .filter(|name| !name.trim().is_empty())
        .unwrap_or_else(|| default_service_name.to_string());
    let resource = Resource::builder().with_service_name(service_name).build();

    let mut builder = SdkMeterProvider::builder().with_resource(resource);
    match export_plan(
        env(ENDPOINT_VAR).as_deref(),
        env(METRICS_ENDPOINT_VAR).as_deref(),
    ) {
        ExportPlan::Disabled => {
            tracing::info!(
                "OTLP metrics export disabled ({ENDPOINT_VAR} unset); executor metrics are \
                 recorded but not exported"
            );
        }
        ExportPlan::Otlp { endpoint } => match opentelemetry_otlp::MetricExporter::builder()
            .with_tonic()
            .with_endpoint(endpoint)
            .build()
        {
            Ok(exporter) => {
                builder = builder.with_periodic_exporter(exporter);
                tracing::info!(
                    "OTLP metrics export enabled (gRPC; destination from the OTEL_EXPORTER_OTLP_* \
                     environment). The pod's egress policy must allow the collector."
                );
            }
            Err(err) => {
                tracing::warn!(
                    error = %err,
                    "OTLP metrics exporter could not be built (this binary supports \
                     OTEL_EXPORTER_OTLP_PROTOCOL=grpc only); continuing WITHOUT metrics export"
                );
            }
        },
    }

    let provider = builder.build();
    global::set_meter_provider(provider.clone());
    MetricsGuard {
        provider: Some(provider),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    use bytes::Bytes;
    use opentelemetry_proto::tonic::collector::metrics::v1::ExportMetricsServiceRequest;
    use opentelemetry_proto::tonic::common::v1::any_value::Value as AnyValueKind;
    use prost::Message;

    use super::*;
    use crate::metrics::ExecutorMetrics;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect();
        move |key| map.get(key).cloned()
    }

    #[test]
    fn export_plan_is_disabled_unless_an_endpoint_is_set() {
        assert_eq!(export_plan(None, None), ExportPlan::Disabled);
        assert_eq!(export_plan(Some(""), Some("   ")), ExportPlan::Disabled);
        assert_eq!(
            export_plan(Some("http://collector:4317"), None),
            ExportPlan::Otlp {
                endpoint: "http://collector:4317".to_string()
            }
        );
        // The metrics-specific variable wins over the generic one.
        assert_eq!(
            export_plan(Some("http://generic:4317"), Some(" http://metrics:4317 ")),
            ExportPlan::Otlp {
                endpoint: "http://metrics:4317".to_string()
            }
        );
        // A blank metrics-specific value falls back to the generic one.
        assert_eq!(
            export_plan(Some("http://generic:4317"), Some("")),
            ExportPlan::Otlp {
                endpoint: "http://generic:4317".to_string()
            }
        );
    }

    #[test]
    fn without_an_endpoint_the_pipeline_records_but_exports_nowhere() {
        let mut guard = init_metrics_with("bundle-executor", env_of(&[]));
        assert!(guard.is_active());
        // Instruments from this provider record without error and without a
        // reader to export to.
        let meter = guard.meter().expect("provider is live");
        let metrics = ExecutorMetrics::new(&meter);
        metrics.record_load("ok");
        guard.shutdown();
        assert!(!guard.is_active(), "shutdown consumes the provider");
        guard.shutdown(); // idempotent
        assert!(guard.meter().is_none());
    }

    #[tokio::test]
    async fn an_unreachable_endpoint_still_builds_a_lazy_exporter_without_failing_the_process() {
        // The tonic channel connects lazily, so building the exporter never
        // needs a collector to be listening; a dead collector only means
        // dropped batches, never a startup failure.
        let guard = init_metrics_with(
            "bundle-executor",
            env_of(&[("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")]),
        );
        assert!(guard.is_active());
    }

    #[tokio::test]
    async fn a_malformed_endpoint_degrades_to_no_export_instead_of_panicking() {
        let guard = init_metrics_with(
            "bundle-executor",
            env_of(&[("OTEL_EXPORTER_OTLP_ENDPOINT", "::::not a uri::::")]),
        );
        assert!(
            guard.is_active(),
            "a bad endpoint must degrade, never abort startup"
        );
    }

    /// Every export request the fake collector decoded, flattened to the
    /// resource `service.name` and the metric names it carried.
    #[derive(Default)]
    struct Received {
        service_names: Vec<String>,
        metric_names: Vec<String>,
    }

    /// A minimal OTLP/gRPC metrics collector over raw HTTP/2: accepts
    /// connections, reads each `Export` request's gRPC-framed protobuf body,
    /// records what it carried, and answers with an empty success response.
    async fn spawn_fake_collector() -> (std::net::SocketAddr, Arc<Mutex<Received>>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind collector");
        let addr = listener.local_addr().expect("collector addr");
        let received = Arc::new(Mutex::new(Received::default()));
        let sink = Arc::clone(&received);
        tokio::spawn(async move {
            loop {
                let Ok((socket, _)) = listener.accept().await else {
                    return;
                };
                let sink = Arc::clone(&sink);
                tokio::spawn(async move {
                    let Ok(mut connection) = h2::server::handshake(socket).await else {
                        return;
                    };
                    // The h2 `Connection` only makes progress while it is being
                    // polled, so each request is handled in its OWN task: reading
                    // a body inline here would stall `accept()` and deadlock the
                    // very connection delivering that body.
                    while let Some(Ok((request, respond))) = connection.accept().await {
                        tokio::spawn(handle_export(request, respond, Arc::clone(&sink)));
                    }
                });
            }
        });
        (addr, received)
    }

    /// Answers one OTLP `Export` call: reads the gRPC-framed protobuf body,
    /// records the resource `service.name` and every metric name it carried,
    /// and replies with an empty success response.
    async fn handle_export(
        request: http::Request<h2::RecvStream>,
        mut respond: h2::server::SendResponse<Bytes>,
        sink: Arc<Mutex<Received>>,
    ) {
        let mut body = request.into_body();
        let mut raw = Vec::new();
        while let Some(Ok(chunk)) = body.data().await {
            let _ = body.flow_control().release_capacity(chunk.len());
            raw.extend_from_slice(&chunk);
        }
        // gRPC framing: 1-byte compressed flag + 4-byte length.
        if raw.len() >= 5 {
            if let Ok(decoded) = ExportMetricsServiceRequest::decode(&raw[5..]) {
                let mut sink = sink.lock().expect("sink lock");
                for resource_metrics in &decoded.resource_metrics {
                    for attr in resource_metrics
                        .resource
                        .iter()
                        .flat_map(|r| r.attributes.iter())
                    {
                        if attr.key == "service.name" {
                            if let Some(AnyValueKind::StringValue(name)) =
                                attr.value.as_ref().and_then(|v| v.value.as_ref())
                            {
                                sink.service_names.push(name.clone());
                            }
                        }
                    }
                    for scope in &resource_metrics.scope_metrics {
                        for metric in &scope.metrics {
                            sink.metric_names.push(metric.name.clone());
                        }
                    }
                }
            }
        }
        let response = http::Response::builder()
            .header("content-type", "application/grpc")
            .body(())
            .expect("response");
        if let Ok(mut send) = respond.send_response(response, false) {
            // Empty `ExportMetricsServiceResponse`, gRPC-framed.
            let _ = send.send_data(Bytes::from_static(&[0, 0, 0, 0, 0]), false);
            let mut trailers = http::HeaderMap::new();
            trailers.insert("grpc-status", http::HeaderValue::from_static("0"));
            let _ = send.send_trailers(trailers);
        }
    }

    /// The proof that opting in actually exports: with an endpoint set, the
    /// recorded executor metrics reach a real OTLP/gRPC collector over the wire
    /// under their suffix-free instrument names, tagged with the configured
    /// service name -- through the same exporter configuration production uses
    /// (gRPC only, no `reqwest`).
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn opting_in_exports_the_recorded_metrics_to_an_otlp_grpc_collector() {
        let (addr, received) = spawn_fake_collector().await;
        let endpoint = format!("http://{addr}");
        let mut guard = init_metrics_with(
            "bundle-executor",
            env_of(&[
                ("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint.as_str()),
                ("OTEL_SERVICE_NAME", "bundle-executor-e2e"),
            ]),
        );
        let meter = guard.meter().expect("provider is live");
        let metrics = Arc::new(ExecutorMetrics::new(&meter));
        metrics.record_load("ok");
        metrics.record_load_phase(
            "waddles.e2e.app",
            "total",
            "ok",
            std::time::Duration::from_millis(5),
        );
        metrics.set_connection_up(true);

        // Shutdown flushes the periodic reader synchronously, driving the
        // exporter's gRPC call on the runtime; run it off the async threads.
        tokio::task::spawn_blocking(move || {
            guard.shutdown();
        })
        .await
        .expect("shutdown task");

        let received = received.lock().expect("received lock");
        for expected in [
            "waddles_executor_loads",
            "waddles_executor_load",
            "waddles_executor_connection_up",
        ] {
            assert!(
                received.metric_names.iter().any(|n| n == expected),
                "collector never received {expected}; got {:?}",
                received.metric_names
            );
        }
        assert!(
            received
                .service_names
                .iter()
                .any(|n| n == "bundle-executor-e2e"),
            "the configured OTEL_SERVICE_NAME must label the resource; got {:?}",
            received.service_names
        );
    }
}
