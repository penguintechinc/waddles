/** `useCommunityScopes`: scope gating from `/auth/me`, super-admin bypass, loading and error states. */
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { useCommunityScopes } from '../useCommunityScopes';
import { httpError, mockApiGet } from '../../test/api';
import { createTestQueryClient } from '../../test/renderWithProviders';

const auth = vi.hoisted(() => ({ isSuperAdmin: false }));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'tux' }, isAuthenticated: true, isSuperAdmin: auth.isSuperAdmin, refreshUser: vi.fn() }),
}));

const CID = '00000007-0000-4000-8000-000000000000';
const OTHER = '00000008-0000-4000-8000-000000000000';

function serve(scopes: string[]) {
  mockApiGet(() => ({
    success: true,
    user: {
      id: 1,
      communities: [{ id: CID, name: 'acme', displayName: 'Acme', role: 'Warden', roleId: null, scopes }],
    },
  }));
}

function mount(communityId: string) {
  const client = createTestQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return renderHook(() => useCommunityScopes(communityId), { wrapper });
}

describe('useCommunityScopes', () => {
  beforeEach(() => {
    auth.isSuperAdmin = false;
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it('is loading, and grants nothing, until /auth/me answers', async () => {
    serve(['community:admin']);
    const { result } = mount(CID);
    expect(result.current.isLoading).toBe(true);
    expect(result.current.has('community:admin')).toBe(false);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
  });

  it('has/hasAny reflect the scopes the membership carries', async () => {
    serve(['community:admin', 'members:remove']);
    const { result } = mount(CID);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.has('community:admin')).toBe(true);
    expect(result.current.has('members:roles.manage')).toBe(false);
    expect(result.current.hasAny('members:roles.manage', 'members:remove')).toBe(true);
    expect(result.current.hasAny('members:roles.manage', 'stream:config.manage')).toBe(false);
    expect(result.current.hasAny()).toBe(false);
    expect([...result.current.scopes].sort()).toEqual(['community:admin', 'members:remove']);
    expect(result.current.membership?.role).toBe('Warden');
  });

  it('grants nothing for a community the caller is not a member of', async () => {
    serve(['community:admin']);
    const { result } = mount(OTHER);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.membership).toBeUndefined();
    expect(result.current.has('community:admin')).toBe(false);
  });

  it('gives super admins every scope, even for communities they do not belong to', async () => {
    auth.isSuperAdmin = true;
    serve([]);
    const { result } = mount(OTHER);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.has('anything:at-all')).toBe(true);
    expect(result.current.hasAny('x:y')).toBe(true);
  });

  it('exposes a /auth/me failure instead of silently denying', async () => {
    mockApiGet(() => {
      throw httpError(500, { error: 'boom' });
    });
    const { result } = mount(CID);
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.has('community:admin')).toBe(false);
  });
});
