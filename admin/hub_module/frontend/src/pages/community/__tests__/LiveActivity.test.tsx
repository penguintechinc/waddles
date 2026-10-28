/**
 * Live activity: hub-api answers `{ success, events: [...] }` (snake_case rows). The page used
 * to read `response.data.data`, so the backfill was silently empty even though the route was
 * served. These tests pin the real wire shape, the SSE tail, and the unhappy paths.
 */
import { act, fireEvent, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import LiveActivity from '../LiveActivity';
import { mergeEvents } from '../../../components/community/liveActivityMerge';
import type { LiveActivityEvent } from '../../../services/communityActivityApi';
import { httpError, mockApiGet, unexpectedUrl } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: MessageEvent<string>) => void) | null = null;
  closed = false;
  constructor(
    public readonly url: string,
    public readonly init?: ConstructorParameters<typeof EventSource>[1],
  ) {
    FakeEventSource.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
  emit(data: string): void {
    this.onmessage?.(new MessageEvent<string>('message', { data }));
  }
}

const wire = (id: number, over: Record<string, unknown> = {}) => ({
  id,
  community_id: 5,
  platform: 'discord',
  actor: `user${id}`,
  message_in: `hello ${id}`,
  reply_out: null,
  occurred_at: '2026-10-21T12:00:00Z',
  ...over,
});

function renderPage(route = '/admin/5/live-activity') {
  return renderWithProviders(<LiveActivity />, { route, path: '/admin/:communityId/live-activity' });
}

describe('LiveActivity', () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders the served `events` backfill, newest first, with replies', async () => {
    const get = mockApiGet((url) =>
      url === '/api/v1/community/5/live-activity'
        ? { success: true, events: [wire(3, { reply_out: 'pong' }), wire(2, { actor: null })] }
        : unexpectedUrl(url),
    );
    renderPage();
    const rows = await screen.findAllByTestId('live-activity-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText('user3')).toBeInTheDocument();
    expect(within(rows[0]!).getByText('hello 3')).toBeInTheDocument();
    expect(within(rows[0]!).getByText(/Waddles: pong/)).toBeInTheDocument();
    expect(within(rows[1]!).getByText('Unknown user')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/community/5/live-activity', { params: { limit: 50 } });
  });

  it('opens the SSE stream for this community with credentials and closes it on unmount', async () => {
    mockApiGet(() => ({ success: true, events: [] }));
    const { unmount } = renderPage();
    await screen.findByText('No Activity Yet');
    const source = FakeEventSource.instances[0]!;
    expect(source.url).toBe('/api/v1/community/5/live-activity/stream');
    expect(source.init).toEqual({ withCredentials: true });
    unmount();
    expect(source.closed).toBe(true);
  });

  it('appends streamed events, de-duplicating ids already backfilled', async () => {
    mockApiGet(() => ({ success: true, events: [wire(2)] }));
    renderPage();
    await screen.findAllByTestId('live-activity-row');
    const source = FakeEventSource.instances[0]!;
    act(() => {
      source.emit(JSON.stringify(wire(2))); // duplicate of the backfill
      source.emit(JSON.stringify(wire(3, { message_in: 'fresh one' })));
    });
    const rows = screen.getAllByTestId('live-activity-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText('fresh one')).toBeInTheDocument();
  });

  it('shows streamed events even when the backfill was empty', async () => {
    mockApiGet(() => ({ success: true, events: [] }));
    renderPage();
    await screen.findByText('No Activity Yet');
    act(() => FakeEventSource.instances[0]!.emit(JSON.stringify(wire(9, { message_in: 'first ever' }))));
    expect(screen.getByText('first ever')).toBeInTheDocument();
    expect(screen.queryByText('No Activity Yet')).not.toBeInTheDocument();
  });

  it('reports connection state transitions', async () => {
    mockApiGet(() => ({ success: true, events: [] }));
    renderPage();
    await screen.findByText('No Activity Yet');
    const pill = screen.getByTestId('live-connection');
    expect(pill).toHaveTextContent('Connecting');
    act(() => FakeEventSource.instances[0]!.onopen?.());
    expect(pill).toHaveTextContent('Live');
    act(() => FakeEventSource.instances[0]!.onerror?.());
    expect(pill).toHaveTextContent('Reconnecting');
  });

  it('surfaces an unreadable stream frame instead of dropping it silently', async () => {
    mockApiGet(() => ({ success: true, events: [wire(1)] }));
    renderPage();
    await screen.findAllByTestId('live-activity-row');
    act(() => FakeEventSource.instances[0]!.emit('{not json'));
    expect(await screen.findByText('Some live events could not be read')).toBeInTheDocument();
    expect(console.error).toHaveBeenCalled();
    expect(screen.getAllByTestId('live-activity-row')).toHaveLength(1);
  });

  it('shows a real error (not an empty feed) when the backfill fails, and retries', async () => {
    let calls = 0;
    mockApiGet(() => {
      calls += 1;
      if (calls === 1) throw httpError(403, { success: false, error: { code: 'FORBIDDEN', message: 'Missing scope community.live_activity:read' } });
      return { success: true, events: [wire(4)] };
    });
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('We could not load recent activity')).toBeInTheDocument();
    expect(within(alert).getByText('Missing scope community.live_activity:read')).toBeInTheDocument();
    expect(screen.queryByText('No Activity Yet')).not.toBeInTheDocument();
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('hello 4')).toBeInTheDocument();
  });

  it('flags contract drift (a list that is not `events`)', async () => {
    mockApiGet(() => ({ success: true, data: [wire(1)] }));
    renderPage();
    expect(await screen.findByText(/Unexpected response from the server \(live-activity: "events" is not a list\)/)).toBeInTheDocument();
  });

  it('rejects an invalid community id without calling the server', () => {
    const get = mockApiGet(() => ({}));
    renderPage('/admin/abc/live-activity');
    expect(screen.getByText('This live activity link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
    expect(FakeEventSource.instances).toHaveLength(0);
  });
});

describe('mergeEvents', () => {
  const ev = (id: number): LiveActivityEvent => ({
    id,
    platform: 'discord',
    actor: null,
    messageIn: null,
    replyOut: null,
    occurredAt: '2026-10-21T00:00:00Z',
  });

  it('de-duplicates by id, sorts newest first and caps at 100', () => {
    expect(mergeEvents([ev(3), ev(1)], [ev(3), ev(2)]).map((e) => e.id)).toEqual([3, 2, 1]);
    const many = Array.from({ length: 150 }, (_, i) => ev(i + 1));
    const merged = mergeEvents([], many);
    expect(merged).toHaveLength(100);
    expect(merged[0]!.id).toBe(150);
  });
});
