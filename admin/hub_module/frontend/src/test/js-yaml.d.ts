/**
 * Minimal ambient typing for the one js-yaml call the OpenAPI contract test needs
 * (avoids adding `@types/js-yaml` for a single function).
 */
declare module 'js-yaml' {
  export function load(input: string): unknown;
}
