//! Trace plumbing: parent a stage span on the envelope's W3C trace context, and read the ids back
//! out of a live span so the first stage can mint the envelope trace FROM its own root span.
//!
//! Spans are plain `tracing` spans bridged to OTel by `tracing-opentelemetry` (the layer
//! `penguin-logging` installs). Without an OTLP endpoint there is no tracer: every helper degrades to
//! a no-op and the envelope keeps its locally minted ids. Attributes are bounded and non-PII only
//! (numeric tenant/community ids, bundle `app_id`, registered command, hop outcome).

use opentelemetry::trace::{SpanContext, SpanId, TraceContextExt, TraceFlags, TraceId, TraceState};
use tracing_opentelemetry::OpenTelemetrySpanExt;

/// Parses a W3C `traceparent` (`00-<32 hex>-<16 hex>-<2 hex>`) plus optional `tracestate` into a
/// remote [`SpanContext`]. `None` for anything malformed (the span then simply has no remote parent).
pub fn parse_remote_context(traceparent: &str, tracestate: Option<&str>) -> Option<SpanContext> {
    let mut it = traceparent.split('-');
    let (version, trace_id, span_id, flags) = (it.next()?, it.next()?, it.next()?, it.next()?);
    if it.next().is_some() || version != "00" || trace_id.len() != 32 || span_id.len() != 16 {
        return None;
    }
    let trace_id = TraceId::from_hex(trace_id).ok()?;
    let span_id = SpanId::from_hex(span_id).ok()?;
    if trace_id == TraceId::INVALID || span_id == SpanId::INVALID {
        return None;
    }
    let flags = TraceFlags::new(u8::from_str_radix(flags, 16).ok()?);
    let state = match tracestate.map(parse_tracestate) {
        None => TraceState::NONE,
        Some(Ok(state)) => state,
        Some(Err(err)) => {
            // W3C Trace Context: an invalid tracestate is discarded, the traceparent stays valid.
            // Not silent: logged (the header content is deliberately not).
            tracing::debug!(error = %err, "dropping invalid tracestate; keeping traceparent");
            TraceState::NONE
        }
    };
    Some(SpanContext::new(trace_id, span_id, flags, true, state))
}

/// Parses a `tracestate` header into a [`TraceState`]; malformed list members are an error rather
/// than being skipped.
fn parse_tracestate(ts: &str) -> Result<TraceState, String> {
    let members = ts
        .split(',')
        .map(|m| {
            m.trim()
                .split_once('=')
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .ok_or_else(|| "tracestate member without '='".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    TraceState::from_key_value(members).map_err(|e| e.to_string())
}

/// Parents `span` on the envelope's trace context. Returns `true` when a remote parent was set.
pub fn set_remote_parent(
    span: &tracing::Span,
    traceparent: &str,
    tracestate: Option<&str>,
) -> bool {
    let Some(sc) = parse_remote_context(traceparent, tracestate) else {
        return false;
    };
    let cx = opentelemetry::Context::new().with_remote_span_context(sc);
    span.set_parent(cx).is_ok()
}

/// The `(trace_id_hex, span_id_hex, sampled)` of a live span, or `None` when no OTel tracer backs it
/// (no OTLP endpoint) or the ids are the invalid sentinel.
pub fn span_ids(span: &tracing::Span) -> Option<(String, String, bool)> {
    let cx = span.context();
    let span_ref = cx.span();
    let sc = span_ref.span_context();
    if !sc.is_valid() {
        return None;
    }
    Some((
        format!("{:032x}", sc.trace_id()),
        format!("{:016x}", sc.span_id()),
        sc.is_sampled(),
    ))
}

/// Formats a `traceparent` from ids returned by [`span_ids`].
pub fn format_traceparent(trace_id: &str, span_id: &str, sampled: bool) -> String {
    format!(
        "00-{trace_id}-{span_id}-{}",
        if sampled { "01" } else { "00" }
    )
}

/// Records an error on `span`: the exception type and message as span attributes plus an error
/// status, so a failed command is findable by `error=true` in the trace backend. `kind` is a closed
/// vocabulary or a Rust error type name; `message` must already be PII-free (never user text).
pub fn record_error(span: &tracing::Span, kind: &str, message: &str) {
    span.set_attribute("exception.type", kind.to_string());
    span.set_attribute("exception.message", message.to_string());
    span.set_status(opentelemetry::trace::Status::error(message.to_string()));
}

#[cfg(test)]
mod tests {
    use super::*;

    const TP: &str = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01";

    #[test]
    fn well_formed_traceparent_parses_with_flags_and_state() {
        let sc = parse_remote_context(TP, Some("waddles=t0:1;d:g,vendor=x")).unwrap();
        assert_eq!(
            format!("{:032x}", sc.trace_id()),
            "0af7651916cd43dd8448eb211c80319c"
        );
        assert_eq!(format!("{:016x}", sc.span_id()), "b7ad6b7169203331");
        assert!(sc.is_sampled());
        assert!(sc.is_remote());
        assert_eq!(sc.trace_state().get("vendor"), Some("x"));
    }

    #[test]
    fn malformed_traceparents_are_refused() {
        for bad in [
            "",
            "garbage",
            "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            "00-0af7-b7ad6b7169203331-01",
            "00-00000000000000000000000000000000-b7ad6b7169203331-01",
            "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01-extra",
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-zz",
        ] {
            assert!(parse_remote_context(bad, None).is_none(), "{bad:?}");
        }
    }

    #[test]
    fn span_ids_are_none_without_a_tracer_and_set_parent_is_harmless() {
        let span = tracing::info_span!("unit");
        assert!(span_ids(&span).is_none());
        // No subscriber/tracer: parenting must not panic, whatever it returns.
        let _ = set_remote_parent(&span, TP, None);
        record_error(&span, "unit", "no tracer, still fine");
    }

    #[test]
    fn invalid_tracestate_is_dropped_but_traceparent_survives() {
        for bad in ["no-equals-member", "ok=1,broken", "UPPER=1", ""] {
            let sc = parse_remote_context(TP, Some(bad))
                .unwrap_or_else(|| panic!("traceparent must survive tracestate {bad:?}"));
            assert!(sc.trace_state().get("ok").is_none(), "state dropped for {bad:?}");
            assert!(sc.is_remote());
        }
        assert!(parse_tracestate("a=1,b=2").is_ok());
        assert!(parse_tracestate("a=1,b").is_err());
    }

    #[test]
    fn traceparent_round_trips_through_format() {
        let tp = format_traceparent("0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331", true);
        assert_eq!(tp, TP);
        assert!(parse_remote_context(&tp, None).is_some());
    }
}
