// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

declare module 'waddle:bundle/context@1.0.0' {
  export function getContext(): BundleContext;
  export interface BundleContext {
    tenant: string,
    community?: string,
    appId: string,
    feature: string,
    version: string,
    /**
     * The Valkey stream entry id of the event being processed. Stable and
     * unique per delivery target; the de-duplication key a bundle records
     * to stay idempotent under at-least-once redelivery (spec SS5.4).
     */
    messageId: string,
    /**
     * Resolved 3-tier config (activation > tenant availability > bundle default),
     * as canonical JSON object text.
     */
    configJson: string,
  }
}
