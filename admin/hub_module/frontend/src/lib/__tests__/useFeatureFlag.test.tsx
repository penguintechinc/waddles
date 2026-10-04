import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { apiClient } from '../apiClient';
import { useFeatureFlag } from '../useFeatureFlag';

vi.mock('../apiClient', () => ({
  apiClient: {
    get: vi.fn(),
  },
}));

const mockedGet = vi.mocked(apiClient.get);

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

describe('useFeatureFlag', () => {
  beforeEach(() => {
    mockedGet.mockReset();
  });

  it('defaults to false while loading', () => {
    mockedGet.mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = renderHook(() => useFeatureFlag('waddles.webui.community_bundles'), {
      wrapper,
    });
    expect(result.current).toBe(false);
  });

  it('returns true for a key present and true in the resolved map', async () => {
    mockedGet.mockResolvedValue({
      data: { flags: { 'waddles.webui.community_bundles': true } },
    });
    const { result } = renderHook(() => useFeatureFlag('waddles.webui.community_bundles'), {
      wrapper,
    });
    await waitFor(() => expect(result.current).toBe(true));
  });

  it('returns false for a key absent from the resolved map (unseen = OFF)', async () => {
    mockedGet.mockResolvedValue({
      data: { flags: { 'waddles.webui.community_bundles': true } },
    });
    const { result } = renderHook(() => useFeatureFlag('waddles.webui.super_tenants'), {
      wrapper,
    });
    await waitFor(() => expect(mockedGet).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });

  it('returns false on fetch error, never throws', async () => {
    mockedGet.mockRejectedValue(new Error('network error'));
    const { result } = renderHook(() => useFeatureFlag('waddles.webui.community_bundles'), {
      wrapper,
    });
    await waitFor(() => expect(mockedGet).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });
});
