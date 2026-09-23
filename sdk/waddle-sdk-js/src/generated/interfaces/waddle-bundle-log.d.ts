// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

declare module 'waddle:bundle/log@1.0.0' {
  /**
   * `fields-json` is a canonical JSON object; the host sanitizes it with the
   * penguin logging SENSITIVE_KEYS rule before emission.
   */
  export function write(lvl: Level, message: string, fieldsJson: string): void;
  /**
   * # Variants
   * 
   * ## `"error"`
   * 
   * ## `"warn"`
   * 
   * ## `"info"`
   * 
   * ## `"debug"`
   */
  export type Level = 'error' | 'warn' | 'info' | 'debug';
}
