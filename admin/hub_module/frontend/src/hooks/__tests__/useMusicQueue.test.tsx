/** Polling behaviour of `useMusicQueue`: 15s cadence, paused while the tab is hidden. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MUSIC_POLL_MS, useMusicQueue } from '../useMusicQueue';
import { mockApiGet } from '../../test/api';

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0 } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
}

afterEach(() => {
  setVisibility('visible');
  vi.restoreAllMocks();
});

describe('useMusicQueue', () => {
  it('polls every 15s while visible and pauses while hidden', async () => {
    mockApiGet(() => ({ queue: [] }));
    const { result } = renderHook(() => useMusicQueue(5), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(MUSIC_POLL_MS).toBe(15_000);
    const options = result.current;
    expect(options.data?.queue).toEqual([]);

    // Inspect the interval function through the query cache observer options.
    const client = new QueryClient();
    const probe = renderHook(() => useMusicQueue(6), {
      wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
    });
    const observerOptions = client.getQueryCache().find({ queryKey: ['community', 6, 'music'] })?.observers[0]?.options;
    const interval = observerOptions?.refetchInterval as () => number | false;
    setVisibility('visible');
    expect(interval()).toBe(15_000);
    setVisibility('hidden');
    expect(interval()).toBe(false);
    probe.unmount();
  });
});
