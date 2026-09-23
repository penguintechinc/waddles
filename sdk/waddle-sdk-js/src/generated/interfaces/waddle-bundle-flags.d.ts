// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

declare module 'waddle:bundle/flags@1.0.0' {
  export function enabled(key: string, defaultValue: boolean): boolean;
  /**
   * "free" | "professional" | "enterprise"
   */
  export function tier(): string;
}
