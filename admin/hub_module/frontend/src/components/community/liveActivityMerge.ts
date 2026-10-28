import type { LiveActivityEvent } from '../../services/communityActivityApi';

/** Most events kept on screen; older ones scroll off so a long-running session stays bounded. */
export const MAX_EVENTS = 100;

/** Newest-first merge of streamed and backfilled events, de-duplicated by id and capped. */
export function mergeEvents(streamed: readonly LiveActivityEvent[], backfill: readonly LiveActivityEvent[]): LiveActivityEvent[] {
  const seen = new Set<number>();
  const merged: LiveActivityEvent[] = [];
  for (const event of [...streamed, ...backfill]) {
    if (seen.has(event.id)) continue;
    seen.add(event.id);
    merged.push(event);
  }
  return merged.sort((a, b) => b.id - a.id).slice(0, MAX_EVENTS);
}
