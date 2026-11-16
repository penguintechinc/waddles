/**
 * Music queue page: renders the contract shape, empty/error/404/flag-off states, the
 * https-only link rule, the no-requester guarantee, polling, and axe a11y.
 */
import { screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import MusicQueue from '../MusicQueue';
import { parseMusicQueue, safeHttpsUrl } from '../../../services/musicQueueApi';
import { httpError, mockApiGet, unexpectedUrl } from '../../../test/api';
import { axeViolations } from '../../../test/axe';
import { renderWithProviders } from '../../../test/renderWithProviders';

const MUSIC_URL = '/api/v1/communities/5/music';

const track = (title: string, over: Record<string, unknown> = {}) => ({
  title,
  artist: `${title} Artist`,
  source_url: null,
  duration_ms: 185000,
  kind: 'request',
  ...over,
});

function serve(music: unknown | Error, flagOn = true) {
  return mockApiGet((url) => {
    if (url === '/api/v1/flags') return { flags: { 'waddles.music.queue-page': flagOn } };
    if (url === MUSIC_URL) {
      if (music instanceof Error) throw music;
      return music;
    }
    return unexpectedUrl(url);
  });
}

function renderPage(route = '/community/5/music') {
  return renderWithProviders(<MusicQueue />, { route, path: '/community/:communityId/music' });
}

afterEach(() => vi.restoreAllMocks());

describe('MusicQueue page', () => {
  it('renders now playing, queue, history and AI DJ On', async () => {
    serve({
      now_playing: track('Alpha'),
      queue: [track('Bravo'), track('Charlie')],
      history: [track('Delta', { played_at: '2026-11-14T10:00:00Z' })],
      ai_dj: { enabled: true },
      dmca_safe: true,
      top_artists: [{ artist: 'Echo', plays: 4 }],
      top_songs: [{ title: 'Foxtrot', artist: 'Echo', plays: 1 }],
    });
    const { container } = renderPage();
    expect(await screen.findByText('Alpha')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'Music' })).toBeInTheDocument();
    expect(screen.getByText('AI DJ: On')).toBeInTheDocument();
    expect(screen.getByText('DMCA-safe mode: On')).toBeInTheDocument();
    expect(screen.getByText(/only royalty-free and NCS tracks/)).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Top artists' }).textContent).toMatch(/Echo.*4 plays/);
    expect(screen.getByRole('list', { name: 'Top songs' }).textContent).toMatch(/Foxtrot.*Echo.*1 play\b/);
    expect(screen.getByRole('list', { name: 'Up next' }).querySelectorAll('li')).toHaveLength(2);
    expect(screen.getByRole('list', { name: 'Last played' }).querySelectorAll('li')).toHaveLength(1);
    expect(screen.getAllByText(/3:05/).length).toBeGreaterThan(0);
    expect(container.querySelector('[aria-live="polite"]')).not.toBeNull();
    expect(await axeViolations(container)).toEqual([]);
  });

  it('shows empty states and AI DJ Off', async () => {
    serve({ now_playing: null, queue: [], history: [], ai_dj: { enabled: false } });
    renderPage();
    expect(await screen.findByText('The queue is empty')).toBeInTheDocument();
    expect(screen.getByText('Nothing played yet')).toBeInTheDocument();
    expect(screen.getByText('Nothing is playing right now.')).toBeInTheDocument();
    expect(screen.getByText('AI DJ: Off')).toBeInTheDocument();
    expect(screen.getByText('DMCA-safe mode: Off')).toBeInTheDocument();
    expect(screen.getByText('No artist stats yet.')).toBeInTheDocument();
    expect(screen.getByText('No song stats yet.')).toBeInTheDocument();
  });

  it('shows an error state for a server failure', async () => {
    serve(httpError(500, { error: 'boom' }));
    renderPage();
    expect(await screen.findByText('We could not load the music queue', {}, { timeout: 5000 })).toBeInTheDocument();
  });

  it('shows not-found for an API 404', async () => {
    serve(httpError(404));
    renderPage();
    expect(await screen.findByText('Music is not available for this community.')).toBeInTheDocument();
  });

  it('shows not-found and never fetches music when the flag is off', async () => {
    const spy = serve({ now_playing: null, queue: [], history: [], ai_dj: { enabled: false } }, false);
    renderPage();
    expect(await screen.findByText('Page not found')).toBeInTheDocument();
    await waitFor(() => expect(spy.mock.calls.some(([url]) => url === '/api/v1/flags')).toBe(true));
    expect(spy.mock.calls.some(([url]) => url === MUSIC_URL)).toBe(false);
  });

  it('rejects a non-numeric community id', async () => {
    serve({ queue: [] });
    renderPage('/community/abc/music');
    expect(await screen.findByText('Page not found')).toBeInTheDocument();
  });

  it('links only https source urls', async () => {
    serve({
      now_playing: track('Secure', { source_url: 'https://example.com/s' }),
      queue: [
        track('Plain', { source_url: 'http://example.com/p' }),
        track('Evil', { source_url: 'javascript:alert(1)' }),
        track('Data', { source_url: 'data:text/html,x' }),
      ],
      history: [],
      ai_dj: { enabled: false },
    });
    renderPage();
    const link = await screen.findByRole('link', { name: /Secure/ });
    expect(link).toHaveAttribute('href', 'https://example.com/s');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getAllByRole('link')).toHaveLength(1);
    for (const title of ['Plain', 'Evil', 'Data']) expect(screen.getByText(title)).toBeInTheDocument();
  });

  it('never renders a requester even if the API sends one', async () => {
    serve({
      now_playing: track('Alpha', { requester: 'viewer_one', requested_by: 'viewer_two', user_id: 99 }),
      queue: [track('Bravo', { requester: 'viewer_three', requester_id: 7 })],
      history: [track('Delta', { requester: 'viewer_four', played_at: '2026-11-14T10:00:00Z' })],
      ai_dj: { enabled: false },
      requester: 'viewer_five',
    });
    const { container } = renderPage();
    await screen.findByText('Alpha');
    expect(container.textContent).not.toMatch(/viewer_|requester|requested by/i);
  });

  it('renders markup in titles as inert text', async () => {
    serve({ now_playing: track('<img src=x onerror=alert(1)>'), queue: [], history: [], ai_dj: { enabled: false } });
    const { container } = renderPage();
    expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });
});

describe('musicQueueApi parsers', () => {
  it('caps history at 5 and drops unknown fields', () => {
    const view = parseMusicQueue({
      history: Array.from({ length: 8 }, (_, i) => ({ title: `t${i}`, requester: 'x' })),
    });
    expect(view.history).toHaveLength(5);
    expect(Object.keys(view.history[0] ?? {})).not.toContain('requester');
    expect(view.aiDjEnabled).toBe(false);
    expect(view.dmcaSafe).toBe(false);
    expect(view.topArtists).toEqual([]);
  });

  it('caps top lists at 5 and rejects a non-list', () => {
    const rows = Array.from({ length: 7 }, (_, i) => ({ artist: `a${i}`, plays: i, requester: 'x' }));
    expect(parseMusicQueue({ top_artists: rows }).topArtists).toHaveLength(5);
    expect(() => parseMusicQueue({ top_songs: 'x' })).toThrow();
  });

  it('throws on a malformed body', () => {
    expect(() => parseMusicQueue('nope')).toThrow();
    expect(() => parseMusicQueue({ queue: 'x' })).toThrow();
    expect(() => parseMusicQueue({ queue: [{ artist: 'no title' }] })).toThrow();
  });

  it('safeHttpsUrl accepts only https', () => {
    expect(safeHttpsUrl('https://a.example/x')).toBe('https://a.example/x');
    expect(safeHttpsUrl('http://a.example')).toBeNull();
    expect(safeHttpsUrl('javascript:alert(1)')).toBeNull();
    expect(safeHttpsUrl('not a url')).toBeNull();
    expect(safeHttpsUrl(null)).toBeNull();
  });
});
