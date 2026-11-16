import { MusicalNoteIcon } from '@heroicons/react/24/outline';
import { useParams } from 'react-router-dom';
import { Badge, Card, CardBody, CardHeader, EmptyState, ErrorState, LoadingState, PageHeader, QueryBoundary } from '../../components/ui';
import { useMusicQueue } from '../../hooks/useMusicQueue';
import { useFeatureFlagStatus } from '../../lib/useFeatureFlag';
import { safeHttpsUrl, type MusicTrack, type TopEntry } from '../../services/musicQueueApi';

/** Formats milliseconds as m:ss; null for a missing or invalid duration. */
function formatDuration(ms: number | null): string | null {
  if (ms === null || ms < 0) return null;
  const total = Math.floor(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

/**
 * Track title that becomes an external link only for https source URLs; every other
 * scheme renders as plain text. Text is rendered via React children (never raw HTML).
 */
function TrackTitle({ track }: { track: MusicTrack }) {
  const href = safeHttpsUrl(track.sourceUrl);
  if (!href) return <span className="font-medium text-fg">{track.title}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="font-medium text-fg underline underline-offset-2 hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
    >
      {track.title}
      <span className="sr-only"> (opens in a new tab)</span>
    </a>
  );
}

/** One list row: title, artist, duration (and when it played, for history). */
function TrackRow({ track }: { track: MusicTrack }) {
  const duration = formatDuration(track.durationMs);
  return (
    <li className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 py-2">
      <div className="min-w-0">
        <TrackTitle track={track} />
        {track.artist ? <span className="text-fg-secondary"> · {track.artist}</span> : null}
      </div>
      <div className="text-sm text-fg-muted">
        {duration}
        {track.playedAt ? (
          <time dateTime={track.playedAt}>
            {duration ? ' · ' : ''}
            {new Date(track.playedAt).toLocaleString()}
          </time>
        ) : null}
      </div>
    </li>
  );
}

/** Ranked list of top artists or songs with an empty state. */
function TopList({ id, title, entries, emptyText }: { id: string; title: string; entries: TopEntry[]; emptyText: string }) {
  return (
    <Card as="section" aria-labelledby={id}>
      <CardHeader title={<span id={id}>{title}</span>} />
      <CardBody>
        {entries.length === 0 ? (
          <p className="text-fg-secondary">{emptyText}</p>
        ) : (
          <ol aria-label={title} className="divide-y divide-line">
            {entries.map((entry, index) => (
              <li key={`${index}-${entry.artist ?? ''}-${entry.title ?? ''}`} className="flex justify-between gap-3 py-2">
                <span className="min-w-0">
                  <span className="font-medium text-fg">{entry.title ?? entry.artist ?? 'Unknown'}</span>
                  {entry.title && entry.artist ? <span className="text-fg-secondary"> · {entry.artist}</span> : null}
                </span>
                <span className="text-sm text-fg-muted">{entry.plays} {entry.plays === 1 ? 'play' : 'plays'}</span>
              </li>
            ))}
          </ol>
        )}
      </CardBody>
    </Card>
  );
}

interface MusicBodyProps {
  nowPlaying: MusicTrack | null;
  queue: MusicTrack[];
  history: MusicTrack[];
  topArtists: TopEntry[];
  topSongs: TopEntry[];
}

/** Renders the music sections for a loaded snapshot inside a polite live region. */
function MusicBody({ nowPlaying, queue, history, topArtists, topSongs }: MusicBodyProps) {
  return (
    <div className="space-y-6" aria-live="polite" aria-atomic="false">
      <Card as="section" aria-labelledby="music-now-playing">
        <CardHeader title={<span id="music-now-playing">Now Playing</span>} />
        <CardBody>
          {nowPlaying ? (
            <ul aria-label="Now playing">
              <TrackRow track={nowPlaying} />
            </ul>
          ) : (
            <p className="text-fg-secondary">Nothing is playing right now.</p>
          )}
        </CardBody>
      </Card>
      <Card as="section" aria-labelledby="music-up-next">
        <CardHeader title={<span id="music-up-next">Up Next</span>} />
        <CardBody>
          {queue.length === 0 ? (
            <EmptyState title="The queue is empty" description="Requested songs will show up here." icon={<MusicalNoteIcon />} />
          ) : (
            <ol aria-label="Up next" className="divide-y divide-line">
              {queue.map((track, index) => (
                <TrackRow key={`${index}-${track.title}`} track={track} />
              ))}
            </ol>
          )}
        </CardBody>
      </Card>
      <Card as="section" aria-labelledby="music-history">
        <CardHeader title={<span id="music-history">Last 5 Played</span>} />
        <CardBody>
          {history.length === 0 ? (
            <EmptyState title="Nothing played yet" description="Recently played songs will show up here." icon={<MusicalNoteIcon />} />
          ) : (
            <ol aria-label="Last played" className="divide-y divide-line">
              {history.map((track, index) => (
                <TrackRow key={`${index}-${track.title}`} track={track} />
              ))}
            </ol>
          )}
        </CardBody>
      </Card>
      <TopList id="music-top-artists" title="Top artists" entries={topArtists} emptyText="No artist stats yet." />
      <TopList id="music-top-songs" title="Top songs" entries={topSongs} emptyText="No song stats yet." />
    </div>
  );
}

/** Fetches and renders one community's music queue; shows not-found on a 404. */
function MusicQueueView({ communityId }: { communityId: number }) {
  const query = useMusicQueue(communityId);
  const status = (query.error as { response?: { status?: number } } | null)?.response?.status;
  if (query.isError && status === 404) {
    return <ErrorState title="Page not found" message="Music is not available for this community." />;
  }
  return (
    <QueryBoundary query={query} loadingLabel="Loading the music queue" errorTitle="We could not load the music queue">
      {(view) => (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <Badge tone={view.aiDjEnabled ? 'green' : 'gray'}>AI DJ: {view.aiDjEnabled ? 'On' : 'Off'}</Badge>
            <Badge tone={view.dmcaSafe ? 'green' : 'gray'}>DMCA-safe mode: {view.dmcaSafe ? 'On' : 'Off'}</Badge>
            <span className="text-sm text-fg-secondary">
              {view.dmcaSafe
                ? 'On: only royalty-free and NCS tracks can be requested.'
                : 'Off: any requested track is allowed.'}
            </span>
          </div>
          <MusicBody
            nowPlaying={view.nowPlaying}
            queue={view.queue}
            history={view.history}
            topArtists={view.topArtists}
            topSongs={view.topSongs}
          />
        </>
      )}
    </QueryBoundary>
  );
}

/** Member-facing music page `/community/:communityId/music`, behind `waddles.music.queue-page` (default OFF). */
export default function MusicQueue() {
  const { communityId } = useParams<{ communityId: string }>();
  const { enabled, isLoading } = useFeatureFlagStatus('waddles.music.queue-page');
  const id = Number(communityId);
  if (isLoading) return <LoadingState label="Loading" />;
  if (!enabled || !Number.isInteger(id) || id <= 0) {
    return <ErrorState title="Page not found" message="This page is not available." />;
  }
  return (
    <div>
      <PageHeader title="Music" description="What is playing, what is next, and what just played." />
      <MusicQueueView communityId={id} />
    </div>
  );
}
