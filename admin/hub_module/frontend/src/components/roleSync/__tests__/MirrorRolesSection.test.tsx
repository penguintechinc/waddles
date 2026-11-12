/** Tests for the "Mirror roles between Discord servers" section: render, create, delete, errors. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import MirrorRolesSection from '../MirrorRolesSection';
import { roleSyncApi, type GuildPairing, type RoleMirrorMapping } from '../../../services/roleSyncApi';

const pairing = (id: number, guild: string): GuildPairing => ({
  id,
  community_id: 7,
  discord_guild_id: guild,
  direction: 'bidirectional',
  sync_enabled: true,
  event_sync_enabled: false,
  role_name_prefix: '[T]',
  created_by_user_id: null,
  created_at: null,
  updated_at: null,
});

const PAIRINGS = [pairing(1, '111'), pairing(2, '222')];

const mapping = (over: Partial<RoleMirrorMapping> = {}): RoleMirrorMapping => ({
  id: 5,
  community_id: 7,
  source_guild_id: '111',
  source_role_id: '10',
  target_guild_id: '222',
  target_role_id: '20',
  last_synced_at: '2026-11-11T10:00:00Z',
  last_roles_added: 3,
  last_roles_removed: 1,
  last_skipped_not_member: 2,
  last_error: null,
  created_at: null,
  ...over,
});

function renderSection(canManage = true, pairings = PAIRINGS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MirrorRolesSection communityId={7} pairings={pairings} canManage={canManage} />
    </QueryClientProvider>,
  );
}

function stubRoles() {
  vi.spyOn(roleSyncApi, 'listGuildRoles').mockImplementation(async (_c, guildId) =>
    guildId === '111'
      ? [{ id: '10', name: 'Crew-A', color: 0, position: 2, permissions: 0, managed: false, privileged: false }]
      : [
          { id: '20', name: 'Crew-B', color: 0, position: 2, permissions: 0, managed: false, privileged: false },
          { id: '21', name: 'Admins', color: 0, position: 3, permissions: 8, managed: false, privileged: true },
        ],
  );
}

describe('MirrorRolesSection', () => {
  afterEach(() => vi.restoreAllMocks());

  it('lists mappings with role names and last sync counters', async () => {
    stubRoles();
    vi.spyOn(roleSyncApi, 'listMirrors').mockResolvedValue([mapping()]);
    renderSection();
    const result = await screen.findByTestId('mirror-result-5');
    expect(result.textContent).toContain('+3 added');
    expect(result.textContent).toContain('−1 removed');
    expect(result.textContent).toContain('2 skipped');
    await waitFor(() => expect(screen.getByTestId('mirror-row-5').textContent).toContain('Crew-A'));
    expect(screen.getByTestId('mirror-row-5').textContent).toContain('Crew-B');
  });

  it('shows "Not synced yet" and a last error', async () => {
    stubRoles();
    vi.spyOn(roleSyncApi, 'listMirrors').mockResolvedValue([
      mapping({ id: 1, last_synced_at: null }),
      mapping({ id: 2, last_error: 'DiscordSyncError' }),
    ]);
    renderSection();
    expect((await screen.findByTestId('mirror-result-1')).textContent).toBe('Not synced yet');
    expect(screen.getByTestId('mirror-result-2').textContent).toContain('DiscordSyncError');
  });

  it('asks for two servers when fewer than two are paired', async () => {
    vi.spyOn(roleSyncApi, 'listMirrors').mockResolvedValue([]);
    vi.spyOn(roleSyncApi, 'listGuildRoles').mockResolvedValue([]);
    renderSection(true, [pairing(1, '111')]);
    expect(await screen.findByTestId('mirror-needs-two-guilds')).toBeTruthy();
    expect(screen.queryByLabelText('Source server')).toBeNull();
  });

  it('creates a mapping from the dropdown selections', async () => {
    stubRoles();
    vi.spyOn(roleSyncApi, 'listMirrors').mockResolvedValue([]);
    const create = vi.spyOn(roleSyncApi, 'createMirror').mockResolvedValue(mapping());
    renderSection();
    const submit = await screen.findByRole('button', { name: 'Add mirror' });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Source server'), { target: { value: '111' } });
    await screen.findByRole('option', { name: 'Crew-A' });
    fireEvent.change(screen.getByLabelText('Source role'), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText('Target server'), { target: { value: '222' } });
    await screen.findByRole('option', { name: 'Crew-B' });
    fireEvent.change(screen.getByLabelText('Target role'), { target: { value: '20' } });
    expect((submit as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(submit);
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(7, {
        source_guild_id: '111',
        source_role_id: '10',
        target_guild_id: '222',
        target_role_id: '20',
      }),
    );
  });

  it('relays the server error message on a rejected create (e.g. cycle)', async () => {
    stubRoles();
    vi.spyOn(roleSyncApi, 'listMirrors').mockResolvedValue([]);
    vi.spyOn(roleSyncApi, 'createMirror').mockRejectedValue({
      response: { data: { error: { message: 'That mapping would create a mirror cycle' } } },
    });
    renderSection();
    await screen.findByRole('button', { name: 'Add mirror' });
    fireEvent.change(screen.getByLabelText('Source server'), { target: { value: '111' } });
    await screen.findByRole('option', { name: 'Crew-A' });
    fireEvent.change(screen.getByLabelText('Source role'), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText('Target server'), { target: { value: '222' } });
    await screen.findByRole('option', { name: 'Crew-B' });
    fireEvent.change(screen.getByLabelText('Target role'), { target: { value: '20' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add mirror' }));
    expect((await screen.findByRole('alert')).textContent).toContain('mirror cycle');
  });

  it('disables privileged roles in the target dropdown with a tooltip', async () => {
    stubRoles();
    vi.spyOn(roleSyncApi, 'listMirrors').mockResolvedValue([]);
    renderSection();
    await screen.findByRole('button', { name: 'Add mirror' });
    fireEvent.change(screen.getByLabelText('Target server'), { target: { value: '222' } });
    const opt = (await screen.findByRole('option', { name: 'Admins (privileged)' })) as HTMLOptionElement;
    expect(opt.disabled).toBe(true);
    expect(opt.title).toContain('cannot be a mirror target');
    expect((screen.getByRole('option', { name: 'Crew-B' }) as HTMLOptionElement).disabled).toBe(false);
  });

  it('deletes a mapping', async () => {
    stubRoles();
    vi.spyOn(roleSyncApi, 'listMirrors').mockResolvedValue([mapping()]);
    const del = vi.spyOn(roleSyncApi, 'deleteMirror').mockResolvedValue({} as never);
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete mirror 5' }));
    await waitFor(() => expect(del).toHaveBeenCalledWith(7, 5));
  });

  it('hides create form and delete buttons for non-admins', async () => {
    stubRoles();
    vi.spyOn(roleSyncApi, 'listMirrors').mockResolvedValue([mapping()]);
    renderSection(false);
    await screen.findByTestId('mirror-row-5');
    expect(screen.queryByRole('button', { name: 'Add mirror' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete mirror 5' })).toBeNull();
  });
});
