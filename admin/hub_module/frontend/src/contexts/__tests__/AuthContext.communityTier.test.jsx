/** communityTier(communityId): reads /auth/me communities[].licenseTier; missing/unknown = Free (1). */
import { describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';

import api from '../../services/api';
import { AuthProvider, useAuth } from '../AuthContext';

vi.mock('../../services/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }));

async function tierFn(communities) {
  api.get.mockResolvedValue({ data: { success: true, user: { id: 1, communities } } });
  const captured = {};
  function Probe() {
    Object.assign(captured, useAuth());
    return null;
  }
  render(<AuthProvider><Probe /></AuthProvider>);
  await waitFor(() => expect(captured.user).toBeTruthy());
  return captured.communityTier;
}

describe('AuthContext.communityTier', () => {
  it('returns the licenseTier of the matching community (string route param ok)', async () => {
    const tier = await tierFn([{ id: '00000007-0000-4000-8000-000000000000', licenseTier: 2 }, { id: '00000008-0000-4000-8000-000000000000', licenseTier: 1 }]);
    expect(tier('00000007-0000-4000-8000-000000000000')).toBe(2);
    expect(tier('00000008-0000-4000-8000-000000000000')).toBe(1);
  });

  it('treats a missing, unknown or out-of-range value as Free', async () => {
    const tier = await tierFn([{ id: '00000007-0000-4000-8000-000000000000' }, { id: '00000008-0000-4000-8000-000000000000', licenseTier: 9 }, { id: '0000000a-0000-4000-8000-000000000000', licenseTier: 3 }, { id: '00000009-0000-4000-8000-000000000000', licenseTier: 'x' }]);
    expect(tier('00000007-0000-4000-8000-000000000000')).toBe(1);
    expect(tier('00000008-0000-4000-8000-000000000000')).toBe(1);
    expect(tier('00000009-0000-4000-8000-000000000000')).toBe(1);
    expect(tier('0000000a-0000-4000-8000-000000000000')).toBe(1);
  });

  it('is Free for a community the caller does not belong to', async () => {
    const tier = await tierFn([{ id: '00000007-0000-4000-8000-000000000000', licenseTier: 2 }]);
    expect(tier('00000063-0000-4000-8000-000000000000')).toBe(1);
  });
});
