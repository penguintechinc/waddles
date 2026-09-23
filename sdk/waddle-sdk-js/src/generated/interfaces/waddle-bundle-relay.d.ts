// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

declare module 'waddle:bundle/relay@1.0.0' {
  export function push(provider: string, messageJson: string): void;
  export type Error = ErrorDenied | ErrorBackend;
  export interface ErrorDenied {
    tag: 'denied',
    val: string,
  }
  export interface ErrorBackend {
    tag: 'backend',
    val: string,
  }
}
