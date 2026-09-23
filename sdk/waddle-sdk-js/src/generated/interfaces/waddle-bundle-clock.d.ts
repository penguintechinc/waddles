// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

declare module 'waddle:bundle/clock@1.0.0' {
  /**
   * Milliseconds since the Unix epoch, as the stage sees it.
   */
  export function nowMillis(): bigint;
  /**
   * RFC 3339 UTC, millisecond precision.
   */
  export function nowRfc3339(): string;
  /**
   * Monotonic nanoseconds, for in-bundle duration measurement only.
   */
  export function monotonicNanos(): bigint;
}
