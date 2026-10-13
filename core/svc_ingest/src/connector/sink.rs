//! Where a connector's normalized events go: the host publishes them onto the
//! Valkey spine through the same single `StageEnvelope` construction site
//! every other receiver uses (`crate::publish::publish_event`), so a
//! connector-sourced event is indistinguishable downstream from a native one
//! (same D30 identity/trace/binding minting, same stream key derivation).

use penguin_spine::{KeyRing, PlatformEvent, Scope, SpineMetrics};

use super::types::{ConnectionCtx, ConnectorEvent};
use crate::publish::{deterministic_workstream_id, publish_event, EventAppender, PublishError};

/// Why a sink refused or failed an event. The driver counts the outcome and
/// moves to the next event; it never retries a rejected one.
#[derive(Debug, thiserror::Error)]
pub enum SinkError {
    /// The event violated the platform-event contract (non-object payload,
    /// bad timestamp, empty event type). The connector produced garbage.
    #[error("connector event is invalid: {0}")]
    Invalid(String),
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

/// Converts a connector's `normalized-event` into the platform-event
/// contract, validating it through `penguin_spine`'s own `Deserialize` (the
/// single source of truth for timestamp shape, non-empty fields, and the
/// source/platform consistency rule) rather than re-implementing those checks.
pub fn to_platform_event(
    ctx: &ConnectionCtx,
    event: ConnectorEvent,
) -> Result<PlatformEvent, SinkError> {
    let payload: serde_json::Value = serde_json::from_str(&event.payload_json)
        .map_err(|e| SinkError::Invalid(format!("payload is not valid JSON: {e}")))?;
    if !payload.is_object() {
        return Err(SinkError::Invalid("payload must be a JSON object".to_string()));
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
        .map_err(|e| SinkError::Invalid(e.to_string()))
}

/// The production sink: publishes onto the spine via `publish_event`.
pub struct SpineEventSink<A: EventAppender, M: SpineMetrics> {
    appender: A,
    metrics: M,
    keyring: KeyRing,
    active_kid: String,
    scope: Scope,
}

impl<A: EventAppender, M: SpineMetrics> SpineEventSink<A, M> {
    /// Builds a sink over an appender (a `SpineClient` in production), the
    /// binding keyring/kid and the deployment scope.
    pub fn new(
        appender: A,
        metrics: M,
        keyring: KeyRing,
        active_kid: impl Into<String>,
        scope: Scope,
    ) -> Self {
        Self {
            appender,
            metrics,
            keyring,
            active_kid: active_kid.into(),
            scope,
        }
    }
}

impl<A: EventAppender, M: SpineMetrics> EventSink for SpineEventSink<A, M> {
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
        )
        .await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
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
        assert!(matches!(to_platform_event(&ctx(), e), Err(SinkError::Invalid(_))));
        let mut e = event();
        e.event_type.clear();
        assert!(matches!(to_platform_event(&ctx(), e), Err(SinkError::Invalid(_))));
    }

    #[test]
    fn an_event_whose_platform_differs_from_the_connection_is_invalid() {
        let mut e = event();
        e.platform = "discord".to_string();
        assert!(matches!(to_platform_event(&ctx(), e), Err(SinkError::Invalid(_))));
    }
}
