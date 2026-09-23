/**
 * Pure ergonomic types and JSON boundary conversions for the
 * `waddle:bundle/stage@1.0.0` WIT world -- no `waddle:bundle/*` value
 * imports, so this module is plain, host-independent JS and is unit
 * testable under a normal Node runtime (unlike `host.ts`, whose imports
 * only resolve inside a `jco`-componentized WASI 0.2 component).
 * The ambient `declare module 'waddle:bundle/...'` typings under
 * `src/generated` (regenerated via `scripts/generate-guest-types.sh`) are
 * picked up automatically -- both tsconfigs `include` all of `src`.
 */

import type {
  PlatformEvent as RawPlatformEvent,
  StageEnvelope as RawStageEnvelope,
  TransportResult as RawTransportResult,
} from "waddle:bundle/types@1.0.0";
import type { Value as RawDbValue } from "waddle:bundle/db@1.0.0";

// ---------------------------------------------------------------------------
// Ergonomic types -- typed accessors over the JSON-crossing payloads (A2).
// The raw WIT records carry open-ended structures as canonical UTF-8 JSON
// text (`payload-json`, `config-json`, `fields-json`); every ergonomic type
// below replaces the `*Json` string field with a parsed, typed value so
// bundle authors never touch `JSON.parse`/`JSON.stringify` themselves.
// ---------------------------------------------------------------------------

/** A JSON object -- the only shape `payload-json`/`config-json` may carry per the WIT contract. */
export type JsonObject = Record<string, unknown>;

/** A platform event with its `payload-json` text parsed into a typed object. */
export interface PlatformEvent {
  platform: string;
  eventType: string;
  actor: string | null;
  payload: JsonObject;
  occurredAt: string;
}

/** A stage envelope with its nested event's payload already parsed. */
export interface StageEnvelope {
  tenant: string;
  community: string | null;
  appId: string;
  stage: string;
  event: PlatformEvent;
  ts: string;
  targetAppId: string | null;
  traceContext: string | null;
}

/** Mirrors the WIT `transport-result` record field-for-field (no JSON fields to parse). */
export interface TransportResult {
  ok: boolean;
  status: number | null;
  detail: string | null;
  providerMessageId: string | null;
}

/** Thrown by a `dispatch` implementation to signal the WIT `transport-error` Err variant. */
export interface TransportError {
  retryable: boolean;
  code: string;
  message: string;
  retryAfterMs: number | null;
}

/** Thrown by a `transform`/`dispatch` implementation to signal "this bundle does not implement this stage". */
export interface UnsupportedStage {
  stage: string;
}

/** Parses a `*-json` field, validating it decodes to a JSON object (never a scalar or array). */
export function parseJsonObject(text: string, fieldName: string): JsonObject {
  const parsed: unknown = JSON.parse(text);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new TypeError(`${fieldName} must decode to a JSON object, got ${JSON.stringify(parsed)}`);
  }
  return parsed as JsonObject;
}

/** Converts a raw jco-generated `platform-event` (JSON-string payload) into the ergonomic, parsed form. */
export function platformEventFromRaw(raw: RawPlatformEvent): PlatformEvent {
  return {
    platform: raw.platform,
    eventType: raw.eventType,
    actor: raw.actor ?? null,
    payload: parseJsonObject(raw.payloadJson, "payload-json"),
    occurredAt: raw.occurredAt,
  };
}

/** Converts an ergonomic `PlatformEvent` back into the raw shape `jco`'s generated exports require. */
export function platformEventToRaw(event: PlatformEvent): RawPlatformEvent {
  return {
    platform: event.platform,
    eventType: event.eventType,
    payloadJson: JSON.stringify(event.payload),
    occurredAt: event.occurredAt,
    // `exactOptionalPropertyTypes` treats `actor: undefined` as distinct from
    // omitting `actor` entirely -- the raw WIT `option<string>` binding
    // requires the latter, so the key is included only when non-null.
    ...(event.actor !== null ? { actor: event.actor } : {}),
  };
}

/** Converts a raw jco-generated `stage-envelope` into the ergonomic, parsed form. */
export function stageEnvelopeFromRaw(raw: RawStageEnvelope): StageEnvelope {
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

/** Converts an ergonomic `TransportResult` back into the raw record shape (no JSON fields to convert). */
export function transportResultToRaw(result: TransportResult): RawTransportResult {
  return {
    ok: result.ok,
    ...(result.status !== null ? { status: result.status } : {}),
    ...(result.detail !== null ? { detail: result.detail } : {}),
    ...(result.providerMessageId !== null ? { providerMessageId: result.providerMessageId } : {}),
  };
}

/** Constructs a WIT `db` `value` variant from a plain JS value -- the typed accessor
 * bundle authors use instead of hand-building the tagged-union shape. */
export function toDbValue(value: string | number | bigint | boolean | Uint8Array | null): RawDbValue {
  if (value === null) {
    return { tag: "null-value" };
  }
  if (typeof value === "boolean") {
    return { tag: "bool-value", val: value };
  }
  if (typeof value === "bigint") {
    return { tag: "int-value", val: value };
  }
  if (typeof value === "number") {
    return { tag: "float-value", val: value };
  }
  if (typeof value === "string") {
    return { tag: "text-value", val: value };
  }
  return { tag: "bytes-value", val: value };
}

/** The two exports a bundle may implement -- matches the WIT `process-stage`/`action-stage`
 * interfaces field-for-field, over the ergonomic (parsed-JSON) types above. Throw
 * {@link UnsupportedStage} from either to signal the WIT `unsupported-stage` Err variant;
 * throw {@link TransportError} from `dispatch` to signal the `transport-error` Err variant. */
export interface BundleDefinition {
  transform?: (event: PlatformEvent) => PlatformEvent | null;
  dispatch?: (envelope: StageEnvelope, config: JsonObject) => TransportResult;
}

/**
 * Ergonomic entry point -- the ONLY thing a bundle author calls directly.
 * Returns its input unchanged; `build-shim.mjs` reads this module's default
 * export at build time and emits the flat, raw-typed top-level functions
 * `jco componentize` requires (converting to/from the ergonomic types above
 * at the boundary).
 */
export function defineBundle(def: BundleDefinition): BundleDefinition {
  return def;
}
