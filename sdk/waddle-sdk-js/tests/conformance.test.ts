/**
 * Real end-to-end conformance check: builds the SDK, flattens the example
 * bundle via `build-shim.mjs`, componentizes it with `jco componentize`,
 * and asserts `wasm-tools component wit` parses the result and reproduces
 * the exact `waddle:bundle/stage@1.0.0` imports/exports. This does not
 * replay golden events through the component (that requires
 * `tools/wit-conformance-harness`, a separate Rust tool not yet built in
 * this repo -- M2a Task 22) but does prove the SDK + example bundle
 * produce a real, WIT-conformant WASI 0.2 component, not a scaffold.
 *
 * Not part of `npm test` (network/toolchain heavy, ~5s); run explicitly via
 * `npm run test:conformance`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// `npm test`/`node --test` both run with cwd = this package's root
// (sdk/waddle-sdk-js), regardless of whether this file runs from `tests/`
// (native TS execution) or `dist-tests/tests/` (compiled) -- `process.cwd()`
// is therefore the reliable anchor, not `import.meta.dirname`, which
// differs between those two locations.
const PACKAGE_ROOT = process.cwd();
const EXAMPLE_DIR = join(PACKAGE_ROOT, "..", "..", "bundles", "javascript", "example");

test("example bundle componentizes into a WIT-conformant WASI 0.2 component", () => {
  const dir = mkdtempSync(join(tmpdir(), "waddle-js-conformance-"));
  const flattened = join(dir, "_flattened.mjs");

  execFileSync("node", [join(PACKAGE_ROOT, "src", "build-shim.mjs"), join(EXAMPLE_DIR, "bundle.ts"), flattened], {
    cwd: PACKAGE_ROOT,
  });
  assert.ok(existsSync(flattened), "build-shim.mjs did not write the flattened module");

  const wasmPath = join(dir, "component.wasm");
  execFileSync(
    "npx",
    [
      "jco",
      "componentize",
      "--bundle",
      flattened,
      "--wit",
      join(EXAMPLE_DIR, "wit"),
      "--world-name",
      "stage",
      "--disable",
      "all",
      "-o",
      wasmPath,
    ],
    { cwd: PACKAGE_ROOT },
  );
  assert.ok(existsSync(wasmPath), "jco componentize did not write component.wasm");

  const wit = execFileSync("wasm-tools", ["component", "wit", wasmPath]).toString();
  assert.match(wit, /export waddle:bundle\/process-stage@1\.0\.0;/);
  assert.match(wit, /export waddle:bundle\/action-stage@1\.0\.0;/);
  assert.match(wit, /import waddle:bundle\/kv@1\.0\.0;/);
  assert.match(wit, /import waddle:bundle\/db@1\.0\.0;/);
});
