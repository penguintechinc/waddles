//! Where a connector's normalized events go: the host publishes them onto the
//! Valkey spine through the same single `StageEnvelope` construction site
//! every other receiver uses (`crate::publish::publish_event`), so a
//! connector-sourced event is indistinguishable downstream from a native one
//! (same D30 identity/trace/binding minting, same stream key derivation).
//!
//! Everything a connector returns is guest-controlled, so nothing here ever
//! echoes a field VALUE into an error: [`SinkError::Invalid`] carries a
//! `&'static str` (a description, never the offending text) and
//! [`SinkError::Oversize`] carries only the field name and two lengths.

use penguin_spine::{KeyRing, PlatformEvent, Scope, SpineMetrics};

use super::registry::EventLimits;
use super::types::{ConnectionCtx, ConnectorEvent};
use crate::identity_crypto::DekProvider;
use crate::publish::{deterministic_workstream_id, publish_event, EventAppender, PublishError};

/// Why a sink refused or failed an event. The driver counts the outcome and
/// moves to the next event; it never retries a rejected one.
#[derive(Debug, thiserror::Error)]
pub enum SinkError {
    /// The event violated the platform-event contract (non-object payload,
    /// bad timestamp, empty or malformed event type). The connector produced
    /// garbage. The text is a fixed description: guest-supplied values are
    /// never echoed (they may be PII or a credential).
    #[error("connector event is invalid: {0}")]
    Invalid(&'static str),
    /// A field exceeded its host-enforced size ceiling (spec S3.5). Fail loud
    /// rather than silently truncate: a truncated JSON payload is corrupt.
    #[error("connector event field {field} is {len} bytes, over the {max}-byte ceiling")]
    Oversize {
        /// Which field (`payload_json`, `actor`, `event_type`,
        /// `frame_events_bytes`).
        field: &'static str,
        /// The offending length in bytes.
        len: usize,
        /// The ceiling it exceeded.
        max: usize,
    },
    /// The spine write itself failed.
    #[error("publish failed: {0}")]
    Publish(#[from] PublishError),
}

impl SinkError {
    /// Stable, low-cardinality label for metrics.
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Invalid(_) => "invalid",
            Self::Oversize { .. } => "oversize",
            Self::Publish(_) => "publish_error",
        }
    }
}

/// Receives one validated-for-platform event. A generic bound, not `dyn`.
#[allow(async_fn_in_trait)]
pub trait EventSink: Send + Sync {
    /// Publishes `event`, which arrived on `ctx`'s connection.
    async fn publish(&self, ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError>;
}

/// True when `event_type` is a dotted lowercase namespace: one or more
/// `.`-separated segments, each `[a-z0-9][a-z0-9_-]*` (`message`,
/// `channel.raid`). Rejects empty segments, uppercase, whitespace, control
/// characters and anything else a guest could smuggle into a stream/metric key.
#[must_use]
pub fn is_valid_event_type(event_type: &str) -> bool {
    !event_type.is_empty()
        && event_type.split('.').all(|segment| {
            let mut chars = segment.chars();
            matches!(chars.next(), Some(c) if c.is_ascii_lowercase() || c.is_ascii_digit())
                && chars
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
        })
}

/// Host-side per-event size caps and `event_type` shape check, run by the
/// driver BEFORE the sink with the platform's pinned [`EventLimits`]. Typed,
/// loud failure -- never a silent truncation.
pub fn check_event_limits(event: &ConnectorEvent, limits: &EventLimits) -> Result<(), SinkError> {
    if event.event_type.len() > limits.max_event_type_bytes {
        return Err(SinkError::Oversize {
            field: "event_type",
            len: event.event_type.len(),
            max: limits.max_event_type_bytes,
        });
    }
    if !is_valid_event_type(&event.event_type) {
        return Err(SinkError::Invalid(
            "event_type must be a dotted lowercase namespace",
        ));
    }
    if let Some(actor) = &event.actor {
        if actor.len() > limits.max_actor_bytes {
            return Err(SinkError::Oversize {
                field: "actor",
                len: actor.len(),
                max: limits.max_actor_bytes,
            });
        }
    }
    if event.payload_json.len() > limits.max_payload_bytes {
        return Err(SinkError::Oversize {
            field: "payload_json",
            len: event.payload_json.len(),
            max: limits.max_payload_bytes,
        });
    }
    Ok(())
}

/// Converts a connector's `normalized-event` into the platform-event
/// contract, validating it through `penguin_spine`'s own `Deserialize` (the
/// single source of truth for timestamp shape, non-empty fields, and the
/// source/platform consistency rule) rather than re-implementing those checks.
/// Every error is a fixed description, never a guest-supplied value.
pub fn to_platform_event(
    ctx: &ConnectionCtx,
    event: ConnectorEvent,
) -> Result<PlatformEvent, SinkError> {
    if event.event_type.is_empty() {
        return Err(SinkError::Invalid("event_type is empty"));
    }
    if event.actor.as_deref() == Some("") {
        return Err(SinkError::Invalid("actor is empty"));
    }
    if event.platform != ctx.platform {
        return Err(SinkError::Invalid(
            "event platform differs from the connection's platform",
        ));
    }
    if chrono::DateTime::parse_from_rfc3339(&event.occurred_at).is_err() {
        return Err(SinkError::Invalid("occurred_at is not RFC 3339"));
    }
    let payload: serde_json::Value = serde_json::from_str(&event.payload_json)
        .map_err(|_| SinkError::Invalid("payload is not valid JSON"))?;
    if !payload.is_object() {
        return Err(SinkError::Invalid("payload must be a JSON object"));
    }
    let candidate = serde_json::json!({
        "platform": event.platform,
        "event_type": event.event_type,
        "actor": event.actor,
        "payload": payload,
        "occurred_at": event.occurred_at,
        "source": {
            "platform": ctx.platform,
            "account_id": ctx.source_id,
            "channel_id": serde_json::Value::Null,
        },
    });
    serde_json::from_value::<PlatformEvent>(candidate)
        .map_err(|_| SinkError::Invalid("event does not satisfy the platform-event contract"))
}

/// The production sink: publishes onto the spine via `publish_event`.
pub struct SpineEventSink<A: EventAppender, M: SpineMetrics, D: DekProvider> {
    appender: A,
    metrics: M,
    keyring: KeyRing,
    active_kid: String,
    scope: Scope,
    dek_provider: D,
    identity_metrics: prometheus::IntCounterVec,
}

impl<A: EventAppender, M: SpineMetrics, D: DekProvider> SpineEventSink<A, M, D> {
    /// Builds a sink over an appender (a `SpineClient` in production), the
    /// binding keyring/kid and the deployment scope.
    pub fn new(
        appender: A,
        metrics: M,
        keyring: KeyRing,
        active_kid: impl Into<String>,
        scope: Scope,
        dek_provider: D,
        identity_metrics: prometheus::IntCounterVec,
    ) -> Self {
        Self {
            appender,
            metrics,
            keyring,
            active_kid: active_kid.into(),
            scope,
            dek_provider,
            identity_metrics,
        }
    }
}

impl<A: EventAppender, M: SpineMetrics, D: DekProvider> SpineEventSink<A, M, D> {
    /// The underlying appender (tests read a recording fake back through it).
    pub fn appender(&self) -> &A {
        &self.appender
    }
}

impl<A: EventAppender, M: SpineMetrics, D: DekProvider> EventSink for SpineEventSink<A, M, D> {
    async fn publish(&self, ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError> {
        let platform_event = to_platform_event(ctx, event)?;
        let workstream_id = deterministic_workstream_id(&ctx.source_id);
        publish_event(
            &self.appender,
            &self.metrics,
            &self.keyring,
            &self.active_kid,
            &self.scope,
            &ctx.source_id,
            &workstream_id,
            Some(&ctx.connection_id),
            platform_event,
            &self.dek_provider,
            &self.identity_metrics,
        )
        .await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn ctx() -> ConnectionCtx {
        ConnectionCtx {
            platform: "echo".to_string(),
            source_id: "src-1".to_string(),
            connection_id: "conn-1".to_string(),
            session_token: None,
        }
    }

    fn event() -> ConnectorEvent {
        ConnectorEvent {
            platform: "echo".to_string(),
            event_type: "message".to_string(),
            actor: Some("alice".to_string()),
            payload_json: r#"{"text":"hi"}"#.to_string(),
            occurred_at: "2026-10-12T00:00:00.000Z".to_string(),
        }
    }

    #[test]
    fn a_valid_event_converts_with_the_connection_as_its_source() {
        let pe = to_platform_event(&ctx(), event()).unwrap();
        assert_eq!(pe.platform, "echo");
        assert_eq!(pe.payload["text"], "hi");
        let source = pe.source.unwrap();
        assert_eq!(source.account_id, "src-1");
        assert_eq!(source.channel_id, None);
    }

    #[test]
    fn a_non_object_or_unparseable_payload_is_invalid() {
        for bad in ["[1,2]", "\"s\"", "7", "null", "{not json"] {
            let mut e = event();
            e.payload_json = bad.to_string();
            assert!(
                matches!(to_platform_event(&ctx(), e), Err(SinkError::Invalid(_))),
                "payload {bad:?} must be refused"
            );
        }
    }

    #[test]
    fn a_bad_timestamp_or_empty_event_type_is_invalid() {
        let mut e = event();
        e.occurred_at = "yesterday".to_string();
        assert!(matches!(
            to_platform_event(&ctx(), e),
            Err(SinkError::Invalid(_))
        ));
        let mut e = event();
        e.event_type.clear();
        assert!(matches!(
            to_platform_event(&ctx(), e),
            Err(SinkError::Invalid(_))
        ));
    }

    /// regression: guest-supplied field VALUES must never appear in a
    /// `SinkError` (they can be PII or a credential, and the driver logs it).
    #[test]
    fn invalid_events_never_echo_guest_supplied_values() {
        let secret = "SECRET-TOKEN-xyz";
        let mut cases = Vec::new();
        let mut e = event();
        e.occurred_at = format!("not-a-time-{secret}");
        cases.push(e);
        let mut e = event();
        e.actor = Some(String::new());
        e.event_type = format!("Bad Type {secret}");
        cases.push(e);
        let mut e = event();
        e.payload_json = format!("{{\"k\": {secret}}}");
        cases.push(e);
        let mut e = event();
        e.platform = format!("evil-{secret}");
        cases.push(e);
        let mut e = event();
        e.event_type = format!("a.{secret}");
        cases.push(e);
        for case in cases {
            let err = to_platform_event(&ctx(), case.clone())
                .err()
                .or_else(|| check_event_limits(&case, &EventLimits::default()).err())
                .expect("each case is invalid");
            let text = format!("{err} {err:?}");
            assert!(!text.contains(secret), "{text}");
        }
    }

    #[test]
    fn event_type_shape_is_a_dotted_lowercase_namespace() {
        for ok in ["message", "channel.raid", "a.b-c.d_e", "v2.thing"] {
            assert!(is_valid_event_type(ok), "{ok}");
        }
        for bad in [
            "",
            ".",
            "a..b",
            ".a",
            "a.",
            "Message",
            "has space",
            "a/b",
            "a\nb",
            "-a",
            "a.B",
            "é",
        ] {
            assert!(!is_valid_event_type(bad), "{bad:?}");
        }
    }

    #[test]
    fn size_caps_fail_loud_with_a_typed_oversize_error() {
        let limits = EventLimits {
            max_payload_bytes: 16,
            max_actor_bytes: 4,
            max_event_type_bytes: 8,
            max_frame_events_bytes: 1024,
        };
        assert!(check_event_limits(&event(), &EventLimits::default()).is_ok());

        let mut e = event();
        e.actor = None;
        e.payload_json = format!(r#"{{"t":"{}"}}"#, "x".repeat(32));
        assert!(matches!(
            check_event_limits(&e, &limits),
            Err(SinkError::Oversize {
                field: "payload_json",
                max: 16,
                ..
            })
        ));
        let mut e = event();
        e.payload_json = "{}".to_string();
        e.actor = Some("toolong".to_string());
        assert!(matches!(
            check_event_limits(&e, &limits),
            Err(SinkError::Oversize {
                field: "actor",
                max: 4,
                ..
            })
        ));
        let mut e = event();
        e.actor = None;
        e.payload_json = "{}".to_string();
        e.event_type = "channel.raid".to_string();
        assert!(matches!(
            check_event_limits(&e, &limits),
            Err(SinkError::Oversize {
                field: "event_type",
                max: 8,
                ..
            })
        ));
        let mut e = event();
        e.event_type = "Bad".to_string();
        assert!(matches!(
            check_event_limits(&e, &EventLimits::default()),
            Err(SinkError::Invalid(_))
        ));
        assert_eq!(
            SinkError::Oversize {
                field: "actor",
                len: 1,
                max: 0
            }
            .kind(),
            "oversize"
        );
    }

    #[test]
    fn an_event_whose_platform_differs_from_the_connection_is_invalid() {
        let mut e = event();
        e.platform = "discord".to_string();
        assert!(matches!(
            to_platform_event(&ctx(), e),
            Err(SinkError::Invalid(_))
        ));
    }
}
