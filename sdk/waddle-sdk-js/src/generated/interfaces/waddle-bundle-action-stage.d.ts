// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

/// <reference path="./waddle-bundle-types.d.ts" />
declare module 'waddle:bundle/action-stage@1.0.0' {
  /**
   * `config` is canonical JSON object text (the resolved 3-tier config).
   */
  export function dispatch(envelope: StageEnvelope, config: string): TransportResult;
  export type StageEnvelope = import('waddle:bundle/types@1.0.0').StageEnvelope;
  export type TransportResult = import('waddle:bundle/types@1.0.0').TransportResult;
  export type TransportError = import('waddle:bundle/types@1.0.0').TransportError;
}
