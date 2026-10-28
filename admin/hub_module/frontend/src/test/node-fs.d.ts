/**
 * Minimal ambient typing for the one Node API the design-token test needs.
 * Avoids adding `@types/node` (and its global Node types) to browser code.
 */
declare module 'node:fs' {
  export function readFileSync(path: URL | string, encoding: 'utf8'): string;
}
