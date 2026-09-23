// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

declare module 'waddle:bundle/kv@1.0.0' {
  export function get(key: string): Uint8Array | undefined;
  /**
   * ttl-seconds = 0 means "no expiry"; the host clamps to KV_MAX_TTL_S.
   */
  export function set(key: string, value: Uint8Array, ttlSeconds: number): void;
  export { _delete as delete };
  function _delete(key: string): void;
  export function increment(key: string, delta: bigint, ttlSeconds: number): bigint;
  export type Error = ErrorTooLarge | ErrorBackend;
  export interface ErrorTooLarge {
    tag: 'too-large',
    val: bigint,
  }
  export interface ErrorBackend {
    tag: 'backend',
    val: string,
  }
}
