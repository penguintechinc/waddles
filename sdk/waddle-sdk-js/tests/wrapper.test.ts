import { test } from "node:test";
import assert from "node:assert/strict";
import {
  defineBundle,
  platformEventFromRaw,
  platformEventToRaw,
  stageEnvelopeFromRaw,
  transportResultToRaw,
  toDbValue,
  type PlatformEvent,
} from "../src/boundary.js";
import type { PlatformEvent as RawPlatformEvent, StageEnvelope as RawStageEnvelope } from "waddle:bundle/types@1.0.0";

test("defineBundle returns its input unchanged", () => {
  const transform = (event: PlatformEvent): PlatformEvent => event;
  const def = defineBundle({ transform });
  assert.equal(def.transform, transform);
  assert.equal(def.dispatch, undefined);
});

test("platformEventFromRaw parses payload-json into a typed object", () => {
  const raw: RawPlatformEvent = {
    platform: "discord",
    eventType: "chat.message",
    actor: "u1",
    payloadJson: JSON.stringify({ text: "!echo hi" }),
    occurredAt: "2026-09-14T12:00:00.000Z",
  };
  const event = platformEventFromRaw(raw);
  assert.equal(event.platform, "discord");
  assert.equal(event.actor, "u1");
  assert.deepEqual(event.payload, { text: "!echo hi" });
});

test("platformEventFromRaw rejects non-object payload-json", () => {
  const raw: RawPlatformEvent = {
    platform: "discord",
    eventType: "chat.message",
    payloadJson: JSON.stringify([1, 2, 3]),
    occurredAt: "2026-09-14T12:00:00.000Z",
  };
  assert.throws(() => platformEventFromRaw(raw), TypeError);
});

test("platformEventToRaw round-trips through platformEventFromRaw", () => {
  const event: PlatformEvent = {
    platform: "twitch",
    eventType: "chat.message",
    actor: null,
    payload: { text: "hello" },
    occurredAt: "2026-09-14T12:00:00.000Z",
  };
  const raw = platformEventToRaw(event);
  assert.equal("actor" in raw, false);
  assert.deepEqual(platformEventFromRaw(raw), event);
});

test("stageEnvelopeFromRaw parses the nested event's payload", () => {
  const raw: RawStageEnvelope = {
    tenant: "t1",
    appId: "app1",
    stage: "action",
    event: {
      platform: "discord",
      eventType: "chat.message",
      payloadJson: JSON.stringify({ text: "hi" }),
      occurredAt: "2026-09-14T12:00:00.000Z",
    },
    ts: "2026-09-14T12:00:00.000Z",
  };
  const envelope = stageEnvelopeFromRaw(raw);
  assert.equal(envelope.community, null);
  assert.deepEqual(envelope.event.payload, { text: "hi" });
});

test("transportResultToRaw omits null-valued optional fields", () => {
  const raw = transportResultToRaw({ ok: true, status: null, detail: null, providerMessageId: null });
  assert.deepEqual(raw, { ok: true });
});

test("toDbValue tags each JS primitive per the WIT db value variant", () => {
  assert.deepEqual(toDbValue(null), { tag: "null-value" });
  assert.deepEqual(toDbValue(true), { tag: "bool-value", val: true });
  assert.deepEqual(toDbValue(42n), { tag: "int-value", val: 42n });
  assert.deepEqual(toDbValue(1.5), { tag: "float-value", val: 1.5 });
  assert.deepEqual(toDbValue("x"), { tag: "text-value", val: "x" });
  assert.deepEqual(toDbValue(new Uint8Array([1, 2])), { tag: "bytes-value", val: new Uint8Array([1, 2]) });
});
