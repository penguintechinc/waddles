# @waddles/waddle-sdk-js

Tier-1 JavaScript/TypeScript SDK for Waddles app bundles. Idiomatic TypeScript
bindings over the normative `waddle:bundle/stage@1.0.0` WIT world
(`wit/waddle-bundle/stage.wit`) -- no DAL facade, no compatibility surface to
a prior API, per spec S6.5.

## Layout

- `src/boundary.ts` -- ergonomic types (`PlatformEvent`, `StageEnvelope`,
  `TransportResult`, ...) and JSON boundary conversions. Pure logic, no
  `waddle:bundle/*` value imports, unit testable under plain Node.
- `src/host.ts` -- thin wrappers over `jco`'s generated ESM imports for each
  host capability (`context`/`http`/`kv`/`db`/`relay`/`flags`/`log`/`clock`).
  Only resolves inside a `jco`-componentized WASI 0.2 component.
- `src/index.ts` -- the public entry point, re-exporting both.
- `src/build-shim.mjs` -- build-time-only Node script that flattens a
  bundle's `defineBundle({ transform, dispatch })` default export into the
  nested-namespace shape (`processStage`/`actionStage`) `jco componentize`
  requires for this WIT world, converting to/from the raw JSON-string
  payload shapes at the boundary.
- `src/generated/` -- ambient `declare module 'waddle:bundle/...'` typings,
  generated from the WIT file via `jco guest-types` (committed; regenerate
  with `npm run generate:types` whenever the WIT file changes).

## Usage (bundle authors)

```typescript
import { defineBundle, kv, log, type PlatformEvent } from "@waddles/waddle-sdk-js";

function transform(event: PlatformEvent): PlatformEvent | null {
  const text = String(event.payload["text"] ?? "");
  if (!text.startsWith("!echo ")) return null;
  const count = kv.increment("echo_count", 1n, 0);
  log.info("handled a command", { count: Number(count) });
  return { ...event, payload: { text: `${text.slice(6)} (echo #${count})` } };
}

export default defineBundle({ transform });
```

See `bundles/javascript/example/bundle.ts` for the full canonical example
(also implementing `dispatch`).

## Componentizing a bundle

```bash
node src/build-shim.mjs <bundle-entry.ts> /tmp/_flattened.mjs
npx jco componentize --bundle /tmp/_flattened.mjs \
  --wit <bundle-dir>/wit --world-name stage --disable all \
  -o /tmp/component.wasm
wasm-tools component wit /tmp/component.wasm   # verify it round-trips the WIT world
```

`npm run test:conformance` runs exactly this pipeline against
`bundles/javascript/example/bundle.ts` and asserts the result.

## Scripts

| Script | Purpose |
|---|---|
| `npm run build` | Compile `src/` to `dist/` (library build) |
| `npm run typecheck` | `tsc --noEmit` over `src/` + `tests/` |
| `npm test` | Fast unit tests (`tests/wrapper.test.ts`, pure boundary logic) |
| `npm run test:conformance` | Real `jco componentize` + `wasm-tools component wit` round-trip (~5s) |
| `npm run lint` | ESLint (`typescript-eslint` strict-type-checked) |
| `npm run generate:types` | Regenerate `src/generated/*.d.ts` from the WIT file |

## Known gaps (explicit, not silent)

- `tools/wit-conformance-harness` (a separate Rust tool, M2a Task 22) does
  not exist yet in this repo -- `test:conformance` proves the example bundle
  componentizes into a WIT-conformant component, but does not replay golden
  events through it under wasmtime. That behavioral check is TODO once the
  harness lands.
- Node 26.x is the standard's target; this package was developed and
  verified against the Node 24.21.0 available in this environment (all
  features used -- native TS execution, `node:test`, `module.register` --
  are stable on both).
