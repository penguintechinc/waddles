/**
 * Thin, ergonomic wrappers over `jco`'s generated ESM imports for every
 * `waddle:bundle/*` capability interface (context/http/kv/db/relay/flags/
 * log/clock). These value imports only resolve once this module is part of
 * a component `jco componentize` builds -- Node's own ESM loader rejects
 * the `waddle:` URL scheme outside that pipeline, so unit tests exercise
 * `boundary.ts`'s pure conversions instead of importing this file directly.
 * The ambient `declare module 'waddle:bundle/...'` typings under
 * `src/generated` (regenerated via `scripts/generate-guest-types.sh`) are
 * picked up automatically -- both tsconfigs `include` all of `src`.
 */

import { send as httpSend } from "waddle:bundle/http@1.0.0";
import type { Request as RawHttpRequest, Response as RawHttpResponse } from "waddle:bundle/http@1.0.0";
import { get as kvGet, set as kvSet, delete as kvDelete, increment as kvIncrement } from "waddle:bundle/kv@1.0.0";
import { execute as dbExecute } from "waddle:bundle/db@1.0.0";
import type { Value as RawDbValue, Rows as RawDbRows } from "waddle:bundle/db@1.0.0";
import { push as relayPush } from "waddle:bundle/relay@1.0.0";
import { enabled as flagsEnabled, tier as flagsTier } from "waddle:bundle/flags@1.0.0";
import { write as logWrite } from "waddle:bundle/log@1.0.0";
import type { Level as RawLogLevel } from "waddle:bundle/log@1.0.0";
import { nowMillis, nowRfc3339, monotonicNanos } from "waddle:bundle/clock@1.0.0";
import { getContext } from "waddle:bundle/context@1.0.0";

import { parseJsonObject, type JsonObject } from "./boundary.js";

/** Per-call scope: tenant, community, app id, feature, version, message id, and parsed 3-tier config. */
export interface BundleContext {
  tenant: string;
  community: string | null;
  appId: string;
  feature: string;
  version: string;
  messageId: string;
  config: JsonObject;
}

/** Immutable, per-call scope. Capability: always granted. */
export const context = {
  get: (): BundleContext => {
    const raw = getContext();
    return {
      tenant: raw.tenant,
      community: raw.community ?? null,
      appId: raw.appId,
      feature: raw.feature,
      version: raw.version,
      messageId: raw.messageId,
      config: parseJsonObject(raw.configJson, "config-json"),
    };
  },
};

/** Guarded outbound HTTP -- granted only when the bundle manifest declares a non-empty `egress`. */
export const http = {
  send: (req: RawHttpRequest): RawHttpResponse => httpSend(req),
};

/** Bundle-scoped key/value storage, always granted. `ttlSeconds = 0` means "no expiry". */
export const kv = {
  get: kvGet,
  set: kvSet,
  delete: kvDelete,
  increment: kvIncrement,
};

/** Parameterized SQL executed by the stage under the bundle's own Postgres role,
 * scoped to `data.tables` and row-level security. Granted only when `data.tables` is non-empty. */
export const db = {
  execute: (statement: string, params: RawDbValue[]): RawDbRows => dbExecute(statement, params),
};

/** Push onto a provider-scoped outbound relay queue. Granted only to action-stage bundles. */
export const relay = {
  push: relayPush,
};

/** PostHog flag + license entitlement, cached, fail-open to the supplied default. Always granted. */
export const flags = {
  enabled: flagsEnabled,
  tier: flagsTier,
};

const LOG_LEVELS: Record<"debug" | "info" | "warn" | "error", RawLogLevel> = {
  debug: "debug",
  info: "info",
  warn: "warn",
  error: "error",
};

function writeLog(level: RawLogLevel, message: string, fields: JsonObject): void {
  logWrite(level, message, JSON.stringify(fields));
}

/** Sanitized, levelled logging into the stage's OTel pipeline (host sanitizes SENSITIVE_KEYS). Always granted. */
export const log = {
  debug: (message: string, fields: JsonObject = {}): void => { writeLog(LOG_LEVELS.debug, message, fields); },
  info: (message: string, fields: JsonObject = {}): void => { writeLog(LOG_LEVELS.info, message, fields); },
  warn: (message: string, fields: JsonObject = {}): void => { writeLog(LOG_LEVELS.warn, message, fields); },
  error: (message: string, fields: JsonObject = {}): void => { writeLog(LOG_LEVELS.error, message, fields); },
};

/** Always-granted stage clock -- `nowMillis`/`monotonicNanos` are `bigint` per the WIT `u64` mapping. */
export const clock = {
  nowMillis,
  nowRfc3339,
  monotonicNanos,
};
