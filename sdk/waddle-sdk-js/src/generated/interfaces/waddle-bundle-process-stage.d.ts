// GENERATED FILE -- do not edit by hand.
// Source: wit/waddle-bundle/stage.wit, regenerated via
// scripts/generate-guest-types.sh (jco guest-types).

/// <reference path="./waddle-bundle-types.d.ts" />
declare module 'waddle:bundle/process-stage@1.0.0' {
  /**
   * `none` means "no reply"; the event is dropped, exactly as v1's
   * `transform() -> PlatformEvent | None`.
   */
  export function transform(event: PlatformEvent): PlatformEvent | undefined;
  export type PlatformEvent = import('waddle:bundle/types@1.0.0').PlatformEvent;
  export type UnsupportedStage = import('waddle:bundle/types@1.0.0').UnsupportedStage;
}
