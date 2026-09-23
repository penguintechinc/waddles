/**
 * Exercises `build-shim.mjs`'s fallback stubs for the export a single-stage
 * bundle does NOT implement -- the example bundle used by
 * `conformance.test.ts` implements both `transform` and `dispatch`, so
 * neither stub ever fires there. This test runs `build-shim.mjs` (as a
 * child process, matching how the real compiler invokes it) against two
 * minimal single-stage fixture bundles and asserts each stub throws the
 * exact WIT error shape Python/Rust emit for the export it doesn't
 * implement: `UnsupportedStage{stage: "process"}` for `transform`,
 * `TransportError{retryable: false, code: "UNSUPPORTED_STAGE", ...}` for
 * `dispatch`. No `jco`/`wasm-tools` toolchain needed -- the flattened
 * output is plain JS, importable directly under a normal Node runtime.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const PACKAGE_ROOT = process.cwd();
const BUILD_SHIM = join(PACKAGE_ROOT, "src", "build-shim.mjs");

/** Runs `build-shim.mjs` against `bundleSource` and imports the flattened result. */
async function flatten(bundleSource: string): Promise<{ processStage: { transform: (e: unknown) => unknown }; actionStage: { dispatch: (e: unknown, c: unknown) => unknown } }> {
  const dir = mkdtempSync(join(tmpdir(), "waddle-js-build-shim-"));
  const entryPath = join(dir, "bundle.mjs");
  const outPath = join(dir, "_flattened.mjs");
  writeFileSync(entryPath, bundleSource, "utf-8");
  execFileSync("node", [BUILD_SHIM, entryPath, outPath], { cwd: PACKAGE_ROOT });
  return import(pathToFileURL(outPath).href) as Promise<{
    processStage: { transform: (e: unknown) => unknown };
    actionStage: { dispatch: (e: unknown, c: unknown) => unknown };
  }>;
}

test("actionStage.dispatch stub throws TransportError{UNSUPPORTED_STAGE} when the bundle only implements transform", async () => {
  const { actionStage } = await flatten(`export default { transform(event) { return event; } };\n`);
  assert.throws(
    () => actionStage.dispatch({}, "{}"),
    (err: unknown) => {
      assert.deepEqual(err, {
        retryable: false,
        code: "UNSUPPORTED_STAGE",
        message: "this bundle does not implement the action stage",
      });
      return true;
    },
  );
});

test("processStage.transform stub throws UnsupportedStage{stage: \"process\"} when the bundle only implements dispatch", async () => {
  const { processStage } = await flatten(`export default { dispatch(envelope, config) { return { ok: true }; } };\n`);
  assert.throws(
    () => processStage.transform({}),
    (err: unknown) => {
      assert.deepEqual(err, { stage: "process" });
      return true;
    },
  );
});
