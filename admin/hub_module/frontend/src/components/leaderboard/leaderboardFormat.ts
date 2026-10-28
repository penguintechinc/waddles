import type { LeaderboardEntry, LeaderboardPeriod, LeaderboardType } from '../../services/communityActivityApi';

/** Compact duration (`1h 5m`) for a watch-time value the server did not pre-format. */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

/** Display value for a row: formatted watch time, or a localised message count. */
export function formatEntryValue(type: LeaderboardType, entry: LeaderboardEntry): string {
  return type === 'watch-time' ? (entry.valueFormatted ?? formatDuration(entry.value)) : entry.value.toLocaleString();
}

/** First letter of a username for the avatar fallback. */
export function avatarInitial(username: string | null): string {
  return username ? username.charAt(0).toUpperCase() : '?';
}

/** Human label for a period (`alltime` -> `All Time`). */
export function periodLabel(period: LeaderboardPeriod, compact = false): string {
  if (period === 'alltime') return compact ? 'All' : 'All Time';
  const name = period.charAt(0).toUpperCase() + period.slice(1);
  return compact ? name.slice(0, 4) : name;
}
