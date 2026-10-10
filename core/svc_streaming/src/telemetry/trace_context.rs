//! W3C Trace Context propagation across process boundaries.
//!
//! Inbound: [`request_span`] builds the per-request server span for the
//! control-plane router, parenting it to an incoming `traceparent` header so
//! a call from hub-api (or this service's own loopback `ingest-auth` call)
//! lands in the caller's trace. Outbound: [`inject_current_context`] stamps
//! the current span's context onto an outgoing request's headers.
//!
//! The span records the **matched route template** (`/whip/{token}`), never
//! the raw URI: WHIP/WHEP tokens and stream keys ride in the path, and
//! tower-http's default `TraceLayer` span would export them verbatim as the
//! `uri` attribute -- a credential leak into the trace backend. A request
//! that matches no route is labeled `unmatched`, which also bounds the
//! attribute's cardinality against scanner traffic.

use axum::extract::MatchedPath;
use axum::http::{HeaderMap, HeaderName, HeaderValue, Request};
use opentelemetry::global;
use opentelemetry::propagation::{Extractor, Injector};
use opentelemetry::Context;
use tracing_opentelemetry::OpenTelemetrySpanExt as _;

/// Read-only view of a [`HeaderMap`] for the propagator.
struct HeaderExtractor<'a>(&'a HeaderMap);

impl Extractor for HeaderExtractor<'_> {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).and_then(|value| value.to_str().ok())
    }

    fn keys(&self) -> Vec<&str> {
        self.0.keys().map(HeaderName::as_str).collect()
    }
}

/// Write-only view of a [`HeaderMap`] for the propagator.
struct HeaderInjector<'a>(&'a mut HeaderMap);

impl Injector for HeaderInjector<'_> {
    fn set(&mut self, key: &str, value: String) {
        // A propagator only emits valid header names/values; if one ever
        // doesn't, dropping the header is correct -- propagation is
        // best-effort and must never fail the request it rides on.
        if let (Ok(name), Ok(value)) = (
            HeaderName::from_bytes(key.as_bytes()),
            HeaderValue::from_str(&value),
        ) {
            self.0.insert(name, value);
        }
    }
}

/// Extracts the remote parent [`Context`] from `headers` using the global
/// text-map propagator. Returns an empty context when no (valid) trace
/// headers are present.
pub fn extract_context(headers: &HeaderMap) -> Context {
    global::get_text_map_propagator(|propagator| propagator.extract(&HeaderExtractor(headers)))
}

/// Injects the current `tracing` span's OpenTelemetry context into
/// `headers` (adds `traceparent`, and `tracestate` when set). A no-op when
/// there is no active exported span, so it is safe to call unconditionally
/// on every outbound request.
pub fn inject_current_context(headers: &mut HeaderMap) {
    let context = tracing::Span::current().context();
    global::get_text_map_propagator(|propagator| {
        propagator.inject_context(&context, &mut HeaderInjector(headers));
    });
}

/// Builds the server span for one inbound HTTP request, for
/// `TraceLayer::make_span_with`. See the module docs for why it records the
/// route template rather than the URI.
pub fn request_span<B>(request: &Request<B>) -> tracing::Span {
    let route = request
        .extensions()
        .get::<MatchedPath>()
        .map(MatchedPath::as_str)
        .unwrap_or("unmatched");
    let method = request.method().as_str();
    let name = format!("{method} {route}");
    let span = tracing::info_span!(
        "http_request",
        otel.name = name.as_str(),
        otel.kind = "server",
        http.request.method = method,
        http.route = route,
    );
    if request.headers().contains_key("traceparent") {
        // `Err` only when the span isn't backed by an OTel layer (no
        // exporter configured) -- nothing to parent in that case.
        let _ = span.set_parent(extract_context(request.headers()));
    }
    span
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::routing::get;
    use axum::Router;
    use opentelemetry::trace::{SpanId, TraceContextExt as _, TraceId, TracerProvider as _};
    use opentelemetry_sdk::propagation::TraceContextPropagator;
    use opentelemetry_sdk::trace::{InMemorySpanExporter, SdkTracerProvider};
    use tower::ServiceExt as _;
    use tower_http::trace::TraceLayer;
    use tracing_subscriber::layer::SubscriberExt as _;

    const TRACE_ID: &str = "4bf92f3577b34da6a3ce929d0e0e4736";
    const PARENT_SPAN_ID: &str = "00f067aa0ba902b7";

    fn traceparent() -> String {
        format!("00-{TRACE_ID}-{PARENT_SPAN_ID}-01")
    }

    /// Installs the propagator (idempotent) and returns a tracer provider
    /// exporting to an in-memory sink, plus the subscriber guard that
    /// routes this thread's spans to it.
    fn tracing_harness() -> (
        SdkTracerProvider,
        InMemorySpanExporter,
        tracing::subscriber::DefaultGuard,
    ) {
        global::set_text_map_propagator(TraceContextPropagator::new());
        let exporter = InMemorySpanExporter::default();
        let provider = SdkTracerProvider::builder()
            .with_simple_exporter(exporter.clone())
            .build();
        let tracer = provider.tracer("test");
        let subscriber =
            tracing_subscriber::registry().with(tracing_opentelemetry::layer().with_tracer(tracer));
        (
            provider,
            exporter,
            tracing::subscriber::set_default(subscriber),
        )
    }

    #[test]
    fn extract_reads_a_valid_traceparent() {
        global::set_text_map_propagator(TraceContextPropagator::new());
        let mut headers = HeaderMap::new();
        headers.insert(
            "traceparent",
            HeaderValue::from_str(&traceparent()).unwrap(),
        );
        let context = extract_context(&headers);
        let span_context = context.span().span_context().clone();
        assert!(span_context.is_valid());
        assert_eq!(
            span_context.trace_id(),
            TraceId::from_hex(TRACE_ID).unwrap()
        );
        assert_eq!(
            span_context.span_id(),
            SpanId::from_hex(PARENT_SPAN_ID).unwrap()
        );
    }

    #[test]
    fn extract_without_headers_yields_an_invalid_context() {
        global::set_text_map_propagator(TraceContextPropagator::new());
        let context = extract_context(&HeaderMap::new());
        assert!(!context.span().span_context().is_valid());
    }

    #[test]
    fn extractor_lists_keys_and_ignores_non_utf8_values() {
        let mut headers = HeaderMap::new();
        headers.insert("traceparent", HeaderValue::from_static("x"));
        headers.insert("x-binary", HeaderValue::from_bytes(&[0xff, 0xfe]).unwrap());
        let extractor = HeaderExtractor(&headers);
        assert_eq!(extractor.get("traceparent"), Some("x"));
        assert_eq!(extractor.get("x-binary"), None);
        assert_eq!(extractor.get("absent"), None);
        let mut keys = extractor.keys();
        keys.sort_unstable();
        assert_eq!(keys, vec!["traceparent", "x-binary"]);
    }

    #[test]
    fn injector_drops_an_invalid_header_name_instead_of_panicking() {
        let mut headers = HeaderMap::new();
        let mut injector = HeaderInjector(&mut headers);
        injector.set("bad name with spaces", "v".to_string());
        injector.set("traceparent", "ok".to_string());
        assert_eq!(headers.len(), 1);
        assert_eq!(headers.get("traceparent").unwrap(), "ok");
    }

    #[test]
    fn inject_without_an_active_exported_span_adds_nothing() {
        global::set_text_map_propagator(TraceContextPropagator::new());
        let mut headers = HeaderMap::new();
        inject_current_context(&mut headers);
        assert!(headers.is_empty(), "no current span -> no traceparent");
    }

    #[test]
    fn inject_stamps_the_current_spans_trace_id() {
        let (provider, _exporter, _guard) = tracing_harness();
        let span = tracing::info_span!("outbound_parent");
        let headers = span.in_scope(|| {
            let mut headers = HeaderMap::new();
            inject_current_context(&mut headers);
            headers
        });
        let value = headers
            .get("traceparent")
            .expect("traceparent injected")
            .to_str()
            .unwrap();
        let expected_trace = span.context().span().span_context().trace_id();
        assert!(
            value.contains(&expected_trace.to_string()),
            "{value} must carry the span's trace id {expected_trace}"
        );
        drop(provider);
    }

    #[tokio::test]
    async fn request_span_records_the_route_template_never_the_secret_in_the_path() {
        let (provider, exporter, _guard) = tracing_harness();
        let app = Router::new()
            .route("/whip/{token}", get(|| async { "ok" }))
            .layer(TraceLayer::new_for_http().make_span_with(request_span::<Body>));

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/whip/sk_live_SECRET_TOKEN_123")
                    .header("traceparent", traceparent())
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert!(response.status().is_success());
        // tower-http keeps the request span open until the response body is
        // dropped; it only exports on close.
        drop(response);

        provider.force_flush().expect("flush");
        let spans = exporter.get_finished_spans().expect("spans");
        let span = spans
            .iter()
            .find(|s| s.name == "GET /whip/{token}")
            .unwrap_or_else(|| {
                panic!(
                    "no http_request span; got {:?}",
                    spans.iter().map(|s| s.name.clone()).collect::<Vec<_>>()
                )
            });

        let rendered: Vec<String> = span
            .attributes
            .iter()
            .map(|kv| format!("{}={}", kv.key, kv.value))
            .collect();
        assert!(
            rendered.iter().any(|a| a == "http.route=/whip/{token}"),
            "attrs: {rendered:?}"
        );
        for attr in &rendered {
            assert!(
                !attr.contains("SECRET_TOKEN_123"),
                "secret leaked into span attribute {attr}"
            );
        }
        // Parented to the caller's trace via the incoming traceparent.
        assert_eq!(
            span.span_context.trace_id(),
            TraceId::from_hex(TRACE_ID).unwrap()
        );
        assert_eq!(
            span.parent_span_id,
            SpanId::from_hex(PARENT_SPAN_ID).unwrap()
        );
    }

    #[tokio::test]
    async fn an_unmatched_path_is_labeled_unmatched_and_starts_a_fresh_trace() {
        let (provider, exporter, _guard) = tracing_harness();
        let app = Router::new()
            .route("/known", get(|| async { "ok" }))
            .layer(TraceLayer::new_for_http().make_span_with(request_span::<Body>));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/probe/admin/../../etc/passwd")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::NOT_FOUND);
        drop(response);

        provider.force_flush().expect("flush");
        let spans = exporter.get_finished_spans().expect("spans");
        let span = spans
            .iter()
            .find(|s| s.name == "GET unmatched")
            .expect("unmatched request span");
        assert_eq!(
            span.parent_span_id,
            SpanId::INVALID,
            "no traceparent -> root span"
        );
        assert!(span
            .attributes
            .iter()
            .all(|kv| !kv.value.to_string().contains("passwd")));
    }
}
