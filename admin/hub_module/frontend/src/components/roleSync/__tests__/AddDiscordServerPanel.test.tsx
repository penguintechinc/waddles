/** Tests for AddDiscordServerPanel: install redirect, pair-by-ID, error mapping, permission gating. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import AddDiscordServerPanel from '../AddDiscordServerPanel';
import { roleSyncApi, roleSyncKeys } from '../../../services/roleSyncApi';
import type { GuildPairing } from '../../../services/roleSyncApi';

vi.mock('../../../services/roleSyncApi', async () => {
  const actual = await vi.importActual<typeof import('../../../services/roleSyncApi')>(
    '../../../services/roleSyncApi',
  );
  return {
    ...actual,
    roleSyncApi: { startServerInstall: vi.fn(), createPairing: vi.fn() },
  };
});

const api = vi.mocked(roleSyncApi);

const PAIRING = {
  id: 9,
  community_id: 42,
  discord_guild_id: '777888999',
  direction: 'discord_to_twitch',
  sync_enabled: true,
} as GuildPairing;

function apiError(status: number, code: string, message = 'server said no') {
  return { response: { status, data: { success: false, error: { message, code, timestamp: 't' } } } };
}

function mount(props: { canManage?: boolean; onPaired?: (p: GuildPairing) => void } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
  render(
    <QueryClientProvider client={queryClient}>
      <AddDiscordServerPanel communityId={42} canManage={props.canManage ?? true} onPaired={props.onPaired} />
    </QueryClientProvider>,
  );
  return { invalidate };
}

function pair(id = '777888999') {
  fireEvent.change(screen.getByLabelText('Discord server ID'), { target: { value: id } });
  fireEvent.click(screen.getByRole('button', { name: 'Pair server by ID' }));
}

const assign = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...window.location, assign },
  });
});

afterEach(() => vi.restoreAllMocks());

describe('AddDiscordServerPanel', () => {
  it('renders nothing when the user cannot manage', () => {
    mount({ canManage: false });
    expect(screen.queryByRole('button', { name: 'Add Discord server' })).toBeNull();
    expect(screen.queryByLabelText('Discord server ID')).toBeNull();
  });

  it('install: calls the API with the sync flag and redirects to the authorize URL', async () => {
    api.startServerInstall.mockResolvedValue('https://discord.com/oauth2/authorize?x=1');
    mount();
    fireEvent.click(screen.getAllByLabelText('Enable role sync for this server')[0]!);
    fireEvent.click(screen.getByRole('button', { name: 'Add Discord server' }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://discord.com/oauth2/authorize?x=1'));
    expect(api.startServerInstall).toHaveBeenCalledWith(42, { sync_enabled: false });
  });

  it('install: 503 DISCORD_NOT_CONFIGURED shows the not-configured message and does not redirect', async () => {
    api.startServerInstall.mockRejectedValue(apiError(503, 'DISCORD_NOT_CONFIGURED'));
    mount();
    fireEvent.click(screen.getByRole('button', { name: 'Add Discord server' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Discord bot is not configured for this tenant');
    expect(assign).not.toHaveBeenCalled();
  });

  it('pair: success invalidates the pairings query, clears the form and calls onPaired', async () => {
    api.createPairing.mockResolvedValue(PAIRING);
    const onPaired = vi.fn();
    const { invalidate } = mount({ onPaired });
    pair();
    expect(await screen.findByRole('status')).toHaveTextContent('777888999');
    expect(api.createPairing).toHaveBeenCalledWith(42, {
      discord_guild_id: '777888999',
      sync_enabled: true,
      direction: 'discord_to_twitch',
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: roleSyncKeys.pairings(42) });
    expect(screen.getByLabelText('Discord server ID')).toHaveValue('');
    expect(onPaired).toHaveBeenCalledWith(PAIRING);
  });

  it.each([
    [409, 'DISCORD_BOT_NOT_IN_GUILD', 'The Waddles bot is not in that server. Use Add Discord server to invite it first.'],
    [409, 'CONFLICT', 'That server is already paired with this community.'],
    [403, 'FORBIDDEN', "You don't have permission to pair servers for this community."],
    [400, 'VALIDATION_ERROR', 'server said no'],
    [500, 'INTERNAL', 'server said no'],
  ])('pair: %i %s maps to friendly text', async (status, code, text) => {
    api.createPairing.mockRejectedValue(apiError(status, code));
    mount();
    pair();
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
  });
});
