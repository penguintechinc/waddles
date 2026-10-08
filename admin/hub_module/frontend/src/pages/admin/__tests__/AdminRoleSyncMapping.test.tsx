/**
 * Tests for the Discord role sync mapping admin page (role-sync PR #639
 * follow-up) -- flag-gated render, pairing selection, binding list, create
 * (both sync_scope directions), delete, and fail-loud error surfacing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';

import AdminRoleSyncMapping from '../AdminRoleSyncMapping';
import { useFeatureFlag } from '../../../lib/useFeatureFlag';
import { useAuth } from '../../../contexts/AuthContext';
import { roleSyncApi } from '../../../services/roleSyncApi';
import type { GuildPairing, RoleSyncBinding } from '../../../services/roleSyncApi';

vi.mock('../../../lib/useFeatureFlag', () => ({
  useFeatureFlag: vi.fn(),
}));

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

// `@penguintechinc/react-libs`'s built dist is a directory-import ESM
// package vitest/node can't resolve directly -- same workaround as
// `src/pages/admin/__tests__/LoyaltyLeaderboard.test.jsx` /
// `src/layouts/__tests__/DashboardLayout.test.jsx`. Stub FormModalBuilder as
// a controlled form that honors `showWhen`/`defaultValue`, close enough to
// the real component's contract for these interaction tests.
interface StubField {
  name: string;
  label: string;
  type: string;
  defaultValue?: string | number | boolean;
  options?: Array<{ value: string | number; label: string }>;
  showWhen?: (values: Record<string, unknown>) => boolean;
}

vi.mock('@penguintechinc/react-libs', () => ({
  FormModalBuilder: ({
    isOpen,
    title,
    fields,
    onSubmit,
    onClose,
    submitButtonText,
  }: {
    isOpen: boolean;
    title: string;
    fields: StubField[];
    onSubmit: (data: Record<string, unknown>) => Promise<void>;
    onClose: () => void;
    submitButtonText: string;
  }) => {
    const [values, setValues] = useState<Record<string, unknown>>(() => {
      const initial: Record<string, unknown> = {};
      fields.forEach((f) => {
        initial[f.name] = f.defaultValue ?? '';
      });
      return initial;
    });
    if (!isOpen) return null;
    const visibleFields = fields.filter((f) => !f.showWhen || f.showWhen(values));
    return (
      <div data-testid="form-modal">
        <h2>{title}</h2>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await onSubmit(values);
            } catch {
              // component under test owns error display; nothing to do here
            }
          }}
        >
          {visibleFields.map((f) => (
            <div key={f.name}>
              <label htmlFor={f.name}>{f.label}</label>
              {f.type === 'select' ? (
                <select
                  id={f.name}
                  value={String(values[f.name] ?? '')}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                >
                  {f.options?.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={f.name}
                  type={f.type}
                  value={String(values[f.name] ?? '')}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                />
              )}
            </div>
          ))}
          <button type="submit">{submitButtonText}</button>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
        </form>
      </div>
    );
  },
}));

vi.mock('../../../services/roleSyncApi', async () => {
  const actual = await vi.importActual<typeof import('../../../services/roleSyncApi')>(
    '../../../services/roleSyncApi',
  );
  return {
    ...actual,
    roleSyncApi: {
      listPairings: vi.fn(),
      listBindings: vi.fn(),
      createBinding: vi.fn(),
      deleteBinding: vi.fn(),
    },
  };
});

const mockedUseFeatureFlag = vi.mocked(useFeatureFlag);
const mockedUseAuth = vi.mocked(useAuth);
const mockedRoleSyncApi = vi.mocked(roleSyncApi);

const PAIRING: GuildPairing = {
  id: 7,
  community_id: 42,
  discord_guild_id: '555000111',
  direction: 'bidirectional',
  sync_enabled: true,
  role_name_prefix: 'wb-',
  created_by_user_id: 1,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

const SUBSCRIBER_BINDING: RoleSyncBinding = {
  id: 1,
  pairing_id: 7,
  sync_scope: 'subscriber_tier',
  subscriber_tier: 2,
  community_role: null,
  discord_role_id: '901',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

const COMMUNITY_ROLE_BINDING: RoleSyncBinding = {
  id: 2,
  pairing_id: 7,
  sync_scope: 'community_role',
  subscriber_tier: null,
  community_role: 'moderator',
  discord_role_id: '902',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

function mount(communityId = '42') {
  return render(
    <MemoryRouter initialEntries={[`/admin/${communityId}/role-sync`]}>
      <Routes>
        <Route path="/admin/:communityId/role-sync" element={<AdminRoleSyncMapping />} />
      </Routes>
    </MemoryRouter>,
    { wrapper },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedUseFeatureFlag.mockReturnValue(true);
  mockedUseAuth.mockReturnValue({ isCommunityAdmin: () => true } as ReturnType<typeof useAuth>);
  mockedRoleSyncApi.listPairings.mockResolvedValue([PAIRING]);
  mockedRoleSyncApi.listBindings.mockResolvedValue([SUBSCRIBER_BINDING, COMMUNITY_ROLE_BINDING]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AdminRoleSyncMapping', () => {
  it('shows a disabled notice when the feature flag is off', () => {
    mockedUseFeatureFlag.mockReturnValue(false);
    mount();
    expect(screen.getByRole('status')).toHaveTextContent('not yet available');
    expect(mockedRoleSyncApi.listPairings).not.toHaveBeenCalled();
  });

  it('shows an empty state when no guild is paired', async () => {
    mockedRoleSyncApi.listPairings.mockResolvedValue([]);
    mount();
    expect(await screen.findByText(/No Discord guild is paired/)).toBeInTheDocument();
    expect(mockedRoleSyncApi.listBindings).not.toHaveBeenCalled();
  });

  it('lists bindings for the selected pairing with direction summaries', async () => {
    mount();

    expect(await screen.findByTestId('binding-row-1')).toHaveTextContent(
      'Tier 2 subscribers → Discord role 901',
    );
    expect(screen.getByTestId('binding-row-2')).toHaveTextContent(
      'Discord role 902 → moderator',
    );
    expect(mockedRoleSyncApi.listBindings).toHaveBeenCalledWith(42, 7);
  });

  it('surfaces a load error instead of swallowing it', async () => {
    mockedRoleSyncApi.listBindings.mockRejectedValue({
      response: { data: { error: { message: 'tenant mismatch' } } },
    });
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('tenant mismatch');
  });

  it('creates a community_role binding and refreshes the list', async () => {
    mockedRoleSyncApi.createBinding.mockResolvedValue({
      ...COMMUNITY_ROLE_BINDING,
      id: 3,
      discord_role_id: '903',
      community_role: 'vip',
    });

    mount();
    await screen.findByTestId('binding-row-1');

    await act(async () => {
      fireEvent.click(screen.getByLabelText('Create a new role sync binding'));
    });

    fireEvent.change(await screen.findByLabelText(/Sync direction/), {
      target: { value: 'community_role' },
    });
    fireEvent.change(screen.getByLabelText(/Discord role ID/), {
      target: { value: '903' },
    });
    fireEvent.change(await screen.findByLabelText(/Community role/), {
      target: { value: 'vip' },
    });

    await act(async () => {
      fireEvent.click(screen.getByText('Add Mapping', { selector: 'button[type="submit"]' }));
    });

    await waitFor(() =>
      expect(mockedRoleSyncApi.createBinding).toHaveBeenCalledWith(42, 7, {
        sync_scope: 'community_role',
        discord_role_id: '903',
        community_role: 'vip',
      }),
    );
  });

  it('deletes a binding after confirm', async () => {
    mockedRoleSyncApi.deleteBinding.mockResolvedValue(
      undefined as unknown as Awaited<ReturnType<typeof roleSyncApi.deleteBinding>>,
    );

    mount();
    const deleteBtn = await screen.findByLabelText('Delete binding for Discord role 901');

    await act(async () => {
      fireEvent.click(deleteBtn);
    });

    await act(async () => {
      fireEvent.click(screen.getByText('Delete', { selector: 'button[type="submit"]' }));
    });

    await waitFor(() => expect(mockedRoleSyncApi.deleteBinding).toHaveBeenCalledWith(42, 7, 1));
  });

  it('surfaces a create error instead of swallowing it', async () => {
    mockedRoleSyncApi.createBinding.mockRejectedValue({
      response: { data: { error: { message: 'discord_role_id already bound' } } },
    });

    mount();
    await screen.findByTestId('binding-row-1');

    await act(async () => {
      fireEvent.click(screen.getByLabelText('Create a new role sync binding'));
    });
    fireEvent.change(await screen.findByLabelText(/Discord role ID/), {
      target: { value: '904' },
    });
    fireEvent.change(await screen.findByLabelText(/Subscriber tier/), {
      target: { value: '1' },
    });

    await act(async () => {
      fireEvent.click(screen.getByText('Add Mapping', { selector: 'button[type="submit"]' }));
    });

    expect(await screen.findByText('discord_role_id already bound')).toBeInTheDocument();
  });

  it('hides management controls for a non-admin viewer', async () => {
    mockedUseAuth.mockReturnValue({ isCommunityAdmin: () => false } as ReturnType<typeof useAuth>);
    mount();
    await screen.findByTestId('binding-row-1');
    expect(screen.queryByLabelText('Create a new role sync binding')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Delete binding for Discord role 901')).not.toBeInTheDocument();
  });
});
