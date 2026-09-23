/**
 * Example Tier 1 JS/TS bundle -- behaviorally identical to the Python/Rust
 * Tier 1 examples (`!echo <text>` replies with an incrementing counter),
 * authored against `@waddles/waddle-sdk-js`'s ergonomic `defineBundle` API.
 */
import { defineBundle, kv, log, type PlatformEvent, type StageEnvelope, type TransportResult } from "../../../sdk/waddle-sdk-js/dist/index.js";

function transform(event: PlatformEvent): PlatformEvent | null {
  const text = String(event.payload["text"] ?? "");
  if (!text.startsWith("!echo ")) {
    return null;
  }
  const replyText = text.slice("!echo ".length).trim();
  const count = kv.increment("echo_count", 1n, 0);
  log.info("echo bundle handled a command", { count: Number(count) });
  return {
    platform: event.platform,
    eventType: "chat.message",
    actor: null,
    payload: { text: `${replyText} (echo #${count})` },
    occurredAt: event.occurredAt,
  };
}

function dispatch(_envelope: StageEnvelope, _config: Record<string, unknown>): TransportResult {
  return { ok: true, status: 200, detail: "echo bundle has no action-stage behavior", providerMessageId: null };
}

export default defineBundle({ transform, dispatch });
