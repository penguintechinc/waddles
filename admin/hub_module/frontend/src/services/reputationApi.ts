import { apiClient } from '../lib/apiClient';
import { expectRecord, readArray, readNumber, readString } from '../lib/wire';

// Typed read of the served reputation leaderboard:
//   GET /api/v1/community/:communityId/reputation/leaderboard?limit=   (scope community.reputation:read)
// Envelope: `{ status: "success", data: { entries: [{ display_name, score, tier }] }, meta }`.
// The legacy admin route `/admin/:id/reputation/leaderboard` (ids, avatars, `users[]`) does not
// exist on hub-api; the served board deliberately exposes display names only, no ids/emails.
// Reputation CONFIG and at-risk lists have no served route (see `UNSERVED_FEATURES`).

export interface ReputationEntry {
  displayName: string;
  score: number;
  tier: string;
}

function parseEntry(raw: unknown): ReputationEntry {
  const row = expectRecord(raw, 'reputation leaderboard[]');
  return {
    displayName: readString(row, 'display_name', 'reputation leaderboard[]'),
    score: readNumber(row, 'score', 'reputation leaderboard[]'),
    tier: readString(row, 'tier', 'reputation leaderboard[]'),
  };
}

/** Parses the leaderboard envelope into ranked entries (server order = rank order). */
export function parseReputationLeaderboard(data: unknown): ReputationEntry[] {
  const payload = expectRecord(expectRecord(data, 'reputation leaderboard')['data'], 'reputation leaderboard data');
  return readArray(payload, 'entries', 'reputation leaderboard').map(parseEntry);
}

/** `GET /api/v1/community/:id/reputation/leaderboard`. */
export async function fetchReputationLeaderboard(communityId: number, limit: number): Promise<ReputationEntry[]> {
  const { data } = await apiClient.get<unknown>(`/api/v1/community/${communityId}/reputation/leaderboard`, {
    params: { limit },
  });
  return parseReputationLeaderboard(data);
}
