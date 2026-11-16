import { apiClient } from '../lib/apiClient';
import { expectRecord, isRecord, readBool, readOptNumber, readOptString, readString, WireShapeError, type WireRecord } from '../lib/wire';

// Typed read of `GET /api/v1/communities/{id}/music` (docs/music-requests-service.md).
// The wire shape deliberately carries NO requester identity. The parsers below copy an
// explicit field allowlist, so even if a server ever sent a requester field it could
// never reach a component.

/** One track row (queue entry, now-playing or history item). */
export interface MusicTrack {
  title: string;
  artist: string | null;
  /** Raw URL from the server; the UI renders it as a link only when `safeHttpsUrl` accepts it. */
  sourceUrl: string | null;
  durationMs: number | null;
  kind: string | null;
  /** ISO timestamp; set on history rows only. */
  playedAt: string | null;
}

/** Aggregate play-count row; `title` is set for top songs only. No requester data. */
export interface TopEntry {
  artist: string | null;
  title: string | null;
  plays: number;
}

export interface MusicQueueView {
  nowPlaying: MusicTrack | null;
  queue: MusicTrack[];
  history: MusicTrack[];
  aiDjEnabled: boolean;
  /** True when only royalty-free / NCS tracks are allowed. */
  dmcaSafe: boolean;
  topArtists: TopEntry[];
  topSongs: TopEntry[];
}

/** Parses one track row, copying only allowlisted fields (never a requester). */
export function parseTrack(raw: unknown, what: string): MusicTrack {
  const row: WireRecord = expectRecord(raw, what);
  return {
    title: readString(row, 'title', what),
    artist: readOptString(row, 'artist'),
    sourceUrl: readOptString(row, 'source_url'),
    durationMs: readOptNumber(row, 'duration_ms'),
    kind: readOptString(row, 'kind'),
    playedAt: readOptString(row, 'played_at'),
  };
}

/** Parses an optional top-N list (artist/title/plays), capped at 5; absent means empty. */
function parseTopList(body: WireRecord, key: string): TopEntry[] {
  const value = body[key];
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new WireShapeError(`music ${key}`, `"${key}" is not a list`);
  return value.slice(0, 5).map((item) => {
    const row = expectRecord(item, `music ${key}`);
    return {
      artist: readOptString(row, 'artist'),
      title: readOptString(row, 'title'),
      plays: readOptNumber(row, 'plays') ?? 0,
    };
  });
}

/** Parses an optional list field into tracks, capped at `limit`; absent means empty. */
function parseTrackList(body: WireRecord, key: string, limit: number): MusicTrack[] {
  const value = body[key];
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new WireShapeError(`music ${key}`, `"${key}" is not a list`);
  return value.slice(0, limit).map((item) => parseTrack(item, `music ${key}`));
}

/** Parses a music-page response body into the view model; history is capped at 5. */
export function parseMusicQueue(data: unknown): MusicQueueView {
  const body = expectRecord(data, 'music');
  const nowRaw = body['now_playing'];
  const aiDj = body['ai_dj'];
  return {
    nowPlaying: nowRaw === undefined || nowRaw === null ? null : parseTrack(nowRaw, 'music now_playing'),
    queue: parseTrackList(body, 'queue', 200),
    history: parseTrackList(body, 'history', 5),
    aiDjEnabled: isRecord(aiDj) ? readBool(aiDj, 'enabled') : false,
    dmcaSafe: readBool(body, 'dmca_safe'),
    topArtists: parseTopList(body, 'top_artists'),
    topSongs: parseTopList(body, 'top_songs'),
  };
}

/** Fetches the community's music queue snapshot. */
export async function fetchMusicQueue(communityId: number): Promise<MusicQueueView> {
  const response = await apiClient.get<unknown>(`/api/v1/communities/${communityId}/music`);
  return parseMusicQueue(response.data);
}

/** Returns the URL string only when it parses as https; anything else (http, javascript:, data:) is null. */
export function safeHttpsUrl(raw: string | null): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === 'https:' ? url.toString() : null;
  } catch {
    return null;
  }
}
