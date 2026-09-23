// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

declare module 'waddle:bundle/types@1.0.0' {
  export interface PlatformEvent {
    platform: string,
    eventType: string,
    actor?: string,
    /**
     * Canonical JSON object text. Never a scalar or array.
     */
    payloadJson: string,
    /**
     * RFC 3339 UTC, millisecond precision.
     */
    occurredAt: string,
  }
  export interface StageEnvelope {
    tenant: string,
    community?: string,
    appId: string,
    stage: string,
    event: PlatformEvent,
    ts: string,
    targetAppId?: string,
    /**
     * W3C traceparent, when the stage had one.
     */
    traceContext?: string,
  }
  export interface TransportResult {
    ok: boolean,
    status?: number,
    detail?: string,
    providerMessageId?: string,
  }
  export interface TransportError {
    /**
     * The single field the action stage branches on.
     */
    retryable: boolean,
    code: string,
    message: string,
    retryAfterMs?: number,
  }
  /**
   * Returned by a stage export the bundle does not implement.
   */
  export interface UnsupportedStage {
    stage: string,
  }
}
