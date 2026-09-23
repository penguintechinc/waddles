/**
 * Waddles JS/TS bundle SDK -- idiomatic TypeScript bindings over the
 * normative `waddle:bundle/stage@1.0.0` WIT world (`wit/waddle-bundle/stage.wit`).
 *
 * Bundle authors call {@link defineBundle} with their `transform`/`dispatch`
 * implementations written against the ergonomic types in `boundary.ts`
 * (parsed JSON payloads, camelCase fields), and the `context`/`http`/`kv`/
 * `db`/`relay`/`flags`/`log`/`clock` helpers from `host.ts`. `build-shim.mjs`
 * is the build-time-only glue that flattens a bundle module's default
 * export into the top-level `export function transform`/`export function
 * dispatch` `jco componentize` requires, converting to/from the raw
 * WIT-generated shapes at the component boundary.
 */

export * from "./boundary.js";
export * from "./host.js";
