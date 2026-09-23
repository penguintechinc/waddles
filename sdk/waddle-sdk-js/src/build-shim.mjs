/**
 * Build-time-only Node script (never imported by a bundle or shipped in the
 * final component; baked into the compiler image at
 * `/opt/waddle-sdk-js/build-shim.mjs` per the JS build recipe). Reads a
 * bundle module's `defineBundle({ transform, dispatch })` default export
 * and writes a flattened module exporting the nested-namespace shape `jco
 * componentize` requires for a world that exports whole interfaces
 * (`export const processStage = { transform(...) {...} }`, `export const
 * actionStage = { dispatch(...) {...} }`) -- verified empirically against
 * jco 1.34.0, which rejects the flat `export function transform` shape for
 * this WIT world with "does not export a \"actionStage\" interface".
 *
 * This is also the ONLY place the ergonomic (parsed-JSON) types the SDK
 * exposes to bundle authors are converted to/from the raw, JSON-string
 * shapes `jco`'s generated bindings require -- kept self-contained (no
 * import of the SDK's own compiled output) since only this one file is
 * copied into the compiler image, not the rest of the package.
 *
 * Usage: node build-shim.mjs <bundle-entry.ts|js> <flattened-output.mjs>
 */
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { register } from "node:module";

const [, , entryPath, outPath] = process.argv;
if (!entryPath || !outPath) {
  console.error("usage: build-shim.mjs <bundle-entry.js> <flattened-output.js>");
  process.exit(2);
}

// A bundle entry transitively imports the SDK's `host.ts`, which imports
// real functions from `waddle:bundle/*@1.0.0` specifiers -- a scheme
// Node's own ESM loader rejects (`ERR_UNSUPPORTED_ESM_URL_SCHEME`), since
// those imports only resolve inside a `jco`-componentized WASI runtime.
// This introspection import only needs `defineBundle`'s returned shape
// (which of transform/dispatch exist), never actually calling a host
// import, so a throwing stub is registered for the `waddle:` scheme for
// the duration of this one dynamic import.
const STUB_HOST_EXPORTS = [
  "send", "get", "set", "increment", "execute", "push",
  "enabled", "tier", "write", "nowMillis", "nowRfc3339", "monotonicNanos", "getContext",
];
const stubSource = [
  ...STUB_HOST_EXPORTS.map(
    (name) => `export function ${name}() { throw new Error("build-shim stub: ${name}() is unavailable during build-time introspection"); }`,
  ),
  `function _delete() { throw new Error("build-shim stub: delete() is unavailable during build-time introspection"); }`,
  `export { _delete as delete };`,
].join("\n");
const stubHooksSource = `
export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith("waddle:")) {
    return { url: specifier, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
export async function load(url, context, nextLoad) {
  if (url.startsWith("waddle:")) {
    return { format: "module", source: ${JSON.stringify(stubSource)}, shortCircuit: true };
  }
  return nextLoad(url, context);
}
`;
register(`data:text/javascript,${encodeURIComponent(stubHooksSource)}`, import.meta.url);

const mod = await import(pathToFileURL(resolve(entryPath)).href);
const def = mod.default;
if (!def || typeof def !== "object") {
  console.error(`${entryPath} must have a default export from defineBundle({...})`);
  process.exit(2);
}
if (typeof def.transform !== "function" && typeof def.dispatch !== "function") {
  console.error(`${entryPath}'s defineBundle({...}) must implement at least one of transform/dispatch`);
  process.exit(2);
}

// The WIT world `stage` exports both `process-stage` and `action-stage`
// unconditionally, so a component must always export `transform` and
// `dispatch`, even when a bundle only logically implements one. The
// generated shim's fallback throws the `unsupported-stage` shape
// (`{ stage: string }`) for `transform` -- matching the Python/Rust SDKs'
// `UnsupportedStage(stage="process")` -- and, for `dispatch`, the WIT
// `action-stage.dispatch` error type is `transport-error` (not
// `unsupported-stage`), so its fallback throws the `{ retryable, code,
// message, retryAfterMs? }` shape, matching Python/Rust's
// `TransportError{retryable: false, code: "UNSUPPORTED_STAGE", message:
// "this bundle does not implement the action stage"}`.
const boundaryHelpers = `
function parseJsonObject(text, fieldName) {
  const parsed = JSON.parse(text);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new TypeError(fieldName + " must decode to a JSON object, got " + JSON.stringify(parsed));
  }
  return parsed;
}
function platformEventFromRaw(raw) {
  return {
    platform: raw.platform,
    eventType: raw.eventType,
    actor: raw.actor ?? null,
    payload: parseJsonObject(raw.payloadJson, "payload-json"),
    occurredAt: raw.occurredAt,
  };
}
function platformEventToRaw(event) {
  const raw = {
    platform: event.platform,
    eventType: event.eventType,
    payloadJson: JSON.stringify(event.payload),
    occurredAt: event.occurredAt,
  };
  if (event.actor !== null && event.actor !== undefined) {
    raw.actor = event.actor;
  }
  return raw;
}
function stageEnvelopeFromRaw(raw) {
  return {
    tenant: raw.tenant,
    community: raw.community ?? null,
    appId: raw.appId,
    stage: raw.stage,
    event: platformEventFromRaw(raw.event),
    ts: raw.ts,
    targetAppId: raw.targetAppId ?? null,
    traceContext: raw.traceContext ?? null,
  };
}
function transportResultToRaw(result) {
  const raw = { ok: result.ok };
  if (result.status !== null && result.status !== undefined) raw.status = result.status;
  if (result.detail !== null && result.detail !== undefined) raw.detail = result.detail;
  if (result.providerMessageId !== null && result.providerMessageId !== undefined) raw.providerMessageId = result.providerMessageId;
  return raw;
}
`;

const lines = [
  `import bundleModule from ${JSON.stringify(resolve(entryPath))};`,
  boundaryHelpers,
];

// The WIT world `stage` exports the two INTERFACES `process-stage` and
// `action-stage` (each wrapping a single function), not bare top-level
// functions -- so `jco componentize` requires the nested-namespace export
// shape (`export const processStage = { transform(...) {...} }`), not flat
// top-level `export function transform`. Confirmed against jco 1.34.0's
// own error message for the flat shape: `does not export a "actionStage"
// interface as expected by the world`.
if (typeof def.transform === "function") {
  lines.push(`export const processStage = {
  transform(rawEvent) {
    const result = bundleModule.transform(platformEventFromRaw(rawEvent));
    return result == null ? undefined : platformEventToRaw(result);
  },
};`);
} else {
  lines.push(`export const processStage = {
  transform(_rawEvent) {
    throw { stage: "process" };
  },
};`);
}

if (typeof def.dispatch === "function") {
  lines.push(`export const actionStage = {
  dispatch(rawEnvelope, rawConfig) {
    const envelope = stageEnvelopeFromRaw(rawEnvelope);
    const config = parseJsonObject(rawConfig, "config");
    const result = bundleModule.dispatch(envelope, config);
    return transportResultToRaw(result);
  },
};`);
} else {
  lines.push(`export const actionStage = {
  dispatch(_rawEnvelope, _rawConfig) {
    throw { retryable: false, code: "UNSUPPORTED_STAGE", message: "this bundle does not implement the action stage" };
  },
};`);
}

writeFileSync(outPath, lines.join("\n") + "\n", "utf-8");
console.log(`wrote ${outPath} with ${lines.length - 2} flattened export(s)`);
