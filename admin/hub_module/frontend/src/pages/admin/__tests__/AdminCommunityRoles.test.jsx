/**
 * Tests for the community roles admin page: priority-ordered system/custom
 * listing, create / edit / delete flows with exact API payloads, form input
 * normalisation (name slug, priority clamp, scope toggles) and error states.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminCommunityRoles from '../AdminCommunityRoles';
import { rolesApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  rolesApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn() },
}));

const ROLES = () => [
  { id: 1, name: 'member', displayName: 'Member', priority: 1, scopes: ['community:read'], is_system: true },
  {
    id: 2,
    name: 'mod',
    display_name: 'Moderator',
    description: 'Keeps order',
    priority: 20,
    scopes: ['channels:moderate', 'resource:pin'],
    is_system: false,
  },
  { id: 3, name: 'vip', priority: undefined, scopes: undefined, is_system: false },
  { id: 4, name: 'owner', displayName: 'Owner', priority: 49, scopes: [], is_system: true },
];

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/roles']}>
      <Routes>
        <Route path="/admin/:communityId/roles" element={<AdminCommunityRoles />} />
      </Routes>
    </MemoryRouter>,
  );
}

const rowOf = (text) => within(screen.getAllByText(text)[0].closest('div.rounded-xl'));
const failure = (message) => ({ response: { data: { message } } });

beforeEach(() => {
  vi.clearAllMocks();
  rolesApi.list.mockResolvedValue({ data: { roles: ROLES() } });
  rolesApi.create.mockResolvedValue({ data: { success: true } });
  rolesApi.update.mockResolvedValue({ data: { success: true } });
  rolesApi.delete.mockResolvedValue({ data: { success: true } });
});

describe('AdminCommunityRoles listing', () => {
  it('shows a loading note, then splits roles into System and Custom groups', async () => {
    mount();
    expect(screen.getByText('Loading roles…')).toBeInTheDocument();

    expect(await screen.findByText('System Roles')).toBeInTheDocument();
    expect(screen.getByText('Custom Roles')).toBeInTheDocument();
    expect(rolesApi.list).toHaveBeenCalledWith('7');
  });

  it('orders by priority, highest first, within each group', async () => {
    mount();
    await screen.findByText('System Roles');
    const text = document.body.textContent;
    expect(text.indexOf('Owner')).toBeLessThan(text.indexOf('Member'));
    expect(text.indexOf('Moderator')).toBeLessThan(text.indexOf('vip'));
  });

  it('renders display-name fallbacks, scope counts, priority and description', async () => {
    mount();
    await screen.findByText('Moderator');

    const mod = rowOf('Moderator');
    expect(mod.getByText('mod')).toBeInTheDocument();
    expect(mod.getByText('Priority: 20')).toBeInTheDocument();
    expect(mod.getByText('2 scopes')).toBeInTheDocument();
    expect(mod.getByText('Keeps order')).toBeInTheDocument();

    const vip = rowOf('vip');
    expect(vip.getByText('Priority: 0')).toBeInTheDocument();
    expect(vip.getByText('0 scopes')).toBeInTheDocument();

    expect(rowOf('Member').getByText('1 scope')).toBeInTheDocument();
  });

  it('shows edit/delete controls only for custom roles', async () => {
    mount();
    await screen.findByText('Moderator');
    expect(rowOf('Moderator').getByTitle('Edit role')).toBeInTheDocument();
    expect(rowOf('Moderator').getByTitle('Delete role')).toBeInTheDocument();
    expect(rowOf('Member').queryByTitle('Edit role')).not.toBeInTheDocument();
    expect(rowOf('Member').getByText('System')).toBeInTheDocument();
  });

  it('only shows the Custom group when there are no system roles, and vice versa', async () => {
    rolesApi.list.mockResolvedValue({ data: { roles: [ROLES()[1]] } });
    const first = mount();
    await screen.findByText('Custom Roles');
    expect(screen.queryByText('System Roles')).not.toBeInTheDocument();
    first.unmount();

    rolesApi.list.mockResolvedValue({ data: { roles: [ROLES()[0]] } });
    mount();
    await screen.findByText('System Roles');
    expect(screen.queryByText('Custom Roles')).not.toBeInTheDocument();
  });

  it('shows the empty state', async () => {
    rolesApi.list.mockResolvedValue({ data: {} });
    mount();
    expect(await screen.findByText(/No roles yet/)).toBeInTheDocument();
  });

  it.each([
    ['the server message', failure('forbidden'), 'forbidden'],
    ['a generic fallback', new Error('x'), 'Failed to load roles.'],
  ])('shows %s when loading fails', async (_label, err, text) => {
    rolesApi.list.mockRejectedValue(err);
    mount();
    expect(await screen.findByText(text)).toBeInTheDocument();
  });
});

describe('AdminCommunityRoles create', () => {
  async function openCreate() {
    mount();
    await screen.findByText('Moderator');
    fireEvent.click(screen.getByRole('button', { name: /Create Role/ }));
    return screen.getByRole('heading', { name: 'Create Role' });
  }

  it('normalises the role name to a lowercase slug', async () => {
    await openCreate();
    const name = screen.getByPlaceholderText('e.g. moderator');
    fireEvent.change(name, { target: { value: 'Super Mod! #1' } });
    expect(name).toHaveValue('super_mod_1');
  });

  it('clamps priority into 0-49 and treats junk as 0', async () => {
    await openCreate();
    const priority = screen.getByRole('spinbutton');
    fireEvent.change(priority, { target: { value: '99' } });
    expect(priority).toHaveValue(49);
    fireEvent.change(priority, { target: { value: '-5' } });
    expect(priority).toHaveValue(0);
    fireEvent.change(priority, { target: { value: '12' } });
    expect(priority).toHaveValue(12);
  });

  it('submits the full form, closes it and reloads the list', async () => {
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. moderator'), { target: { value: 'helper' } });
    fireEvent.change(screen.getByPlaceholderText('e.g. Moderator'), { target: { value: 'Helper' } });
    fireEvent.change(screen.getByPlaceholderText('Optional description'), { target: { value: 'Helps out' } });
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '7' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'channels:send_chat' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'resource:pin' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'channels:send_chat' }));
    const form = screen.getByRole('heading', { name: 'Create Role' }).closest('form');
    fireEvent.click(within(form).getByRole('button', { name: 'Create Role' }));

    await waitFor(() => expect(rolesApi.create).toHaveBeenCalledTimes(1));
    expect(rolesApi.create).toHaveBeenCalledWith('7', {
      name: 'helper',
      displayName: 'Helper',
      description: 'Helps out',
      priority: 7,
      scopes: ['resource:pin'],
    });
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Create Role' })).not.toBeInTheDocument());
    expect(rolesApi.list).toHaveBeenCalledTimes(2);
  });

  it('keeps the form open and shows the error when create fails', async () => {
    rolesApi.create.mockRejectedValue(failure('name taken'));
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. moderator'), { target: { value: 'dup' } });
    fireEvent.change(screen.getByPlaceholderText('e.g. Moderator'), { target: { value: 'Dup' } });
    fireEvent.submit(screen.getByRole('heading', { name: 'Create Role' }).closest('form'));

    expect(await screen.findByText('name taken')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Create Role' })).toBeInTheDocument();
  });

  it('falls back to a generic create error', async () => {
    rolesApi.create.mockRejectedValue(new Error('x'));
    await openCreate();
    fireEvent.submit(screen.getByRole('heading', { name: 'Create Role' }).closest('form'));
    expect(await screen.findByText('Failed to create role.')).toBeInTheDocument();
  });

  it('shows Saving… and disables submit while the request is in flight', async () => {
    let release;
    rolesApi.create.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await openCreate();
    fireEvent.submit(screen.getByRole('heading', { name: 'Create Role' }).closest('form'));

    expect(await screen.findByRole('button', { name: 'Saving…' })).toBeDisabled();
    release({ data: {} });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Saving…' })).not.toBeInTheDocument());
  });

  it('cancels without calling the API', async () => {
    await openCreate();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Create Role' })).not.toBeInTheDocument();
    expect(rolesApi.create).not.toHaveBeenCalled();
  });
});

describe('AdminCommunityRoles edit', () => {
  async function openEdit(name = 'Moderator') {
    mount();
    await screen.findAllByText(name);
    fireEvent.click(rowOf(name).getByTitle('Edit role'));
    return screen.getByRole('heading', { name: 'Edit Role' }).closest('form');
  }

  it('pre-fills the form from the role, including the snake_case display name', async () => {
    const form = await openEdit();
    expect(within(form).getByPlaceholderText('e.g. moderator')).toHaveValue('mod');
    expect(within(form).getByPlaceholderText('e.g. Moderator')).toHaveValue('Moderator');
    expect(within(form).getByPlaceholderText('Optional description')).toHaveValue('Keeps order');
    expect(within(form).getByRole('spinbutton')).toHaveValue(20);
    expect(within(form).getByRole('checkbox', { name: 'channels:moderate' })).toBeChecked();
    expect(within(form).getByRole('checkbox', { name: 'community:read' })).not.toBeChecked();
  });

  it('defaults missing fields when editing a sparse role', async () => {
    const form = await openEdit('vip');
    expect(within(form).getByPlaceholderText('e.g. Moderator')).toHaveValue('');
    expect(within(form).getByPlaceholderText('Optional description')).toHaveValue('');
    expect(within(form).getByRole('spinbutton')).toHaveValue(0);
  });

  it('saves changes to the role id and reloads', async () => {
    const form = await openEdit();
    fireEvent.change(within(form).getByPlaceholderText('e.g. Moderator'), { target: { value: 'Head Mod' } });
    fireEvent.click(within(form).getByRole('checkbox', { name: 'resource:pin' }));
    fireEvent.click(within(form).getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(rolesApi.update).toHaveBeenCalledTimes(1));
    expect(rolesApi.update).toHaveBeenCalledWith('7', 2, {
      name: 'mod',
      displayName: 'Head Mod',
      description: 'Keeps order',
      priority: 20,
      scopes: ['channels:moderate'],
    });
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Edit Role' })).not.toBeInTheDocument());
    expect(rolesApi.list).toHaveBeenCalledTimes(2);
  });

  it('shows the error when update fails and keeps the form', async () => {
    rolesApi.update.mockRejectedValue(failure('stale role'));
    const form = await openEdit();
    fireEvent.submit(form);
    expect(await screen.findByText('stale role')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Edit Role' })).toBeInTheDocument();
  });

  it('falls back to a generic update error and can be cancelled', async () => {
    rolesApi.update.mockRejectedValue(new Error('x'));
    const form = await openEdit();
    fireEvent.submit(form);
    expect(await screen.findByText('Failed to update role.')).toBeInTheDocument();
    fireEvent.click(within(form).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Edit Role' })).not.toBeInTheDocument();
  });
});

describe('AdminCommunityRoles delete', () => {
  async function askDelete(name = 'Moderator') {
    mount();
    await screen.findAllByText(name);
    fireEvent.click(rowOf(name).getByTitle('Delete role'));
    return screen.getByText(/This cannot be undone/);
  }

  it('asks for confirmation naming the role, and Cancel dismisses it', async () => {
    const prompt = await askDelete();
    expect(prompt.parentElement).toHaveTextContent('Delete role Moderator?');
    fireEvent.click(within(prompt.parentElement).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByText(/This cannot be undone/)).not.toBeInTheDocument();
    expect(rolesApi.delete).not.toHaveBeenCalled();
  });

  it('falls back to the machine name when a role has no display name', async () => {
    const prompt = await askDelete('vip');
    expect(prompt.parentElement).toHaveTextContent('Delete role vip?');
  });

  it('deletes by role id and reloads', async () => {
    const prompt = await askDelete();
    fireEvent.click(within(prompt.parentElement).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(rolesApi.delete).toHaveBeenCalledWith('7', 2));
    await waitFor(() => expect(screen.queryByText(/This cannot be undone/)).not.toBeInTheDocument());
    expect(rolesApi.list).toHaveBeenCalledTimes(2);
  });

  it('shows Deleting… while pending', async () => {
    let release;
    rolesApi.delete.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const prompt = await askDelete();
    fireEvent.click(within(prompt.parentElement).getByRole('button', { name: 'Delete' }));

    expect(await screen.findByRole('button', { name: 'Deleting…' })).toBeDisabled();
    release({ data: {} });
    await waitFor(() => expect(screen.queryByText(/This cannot be undone/)).not.toBeInTheDocument());
  });

  it.each([
    ['server message', failure('role in use'), 'role in use'],
    ['generic fallback', new Error('x'), 'Failed to delete role.'],
  ])('shows the %s when delete fails', async (_label, err, text) => {
    rolesApi.delete.mockRejectedValue(err);
    const prompt = await askDelete();
    fireEvent.click(within(prompt.parentElement).getByRole('button', { name: 'Delete' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });
});

describe('AdminCommunityRoles panel switching', () => {
  it('only ever shows one of create / edit / delete at a time', async () => {
    mount();
    await screen.findByText('Moderator');

    fireEvent.click(screen.getByRole('button', { name: /Create Role/ }));
    expect(screen.getByRole('heading', { name: 'Create Role' })).toBeInTheDocument();

    fireEvent.click(rowOf('Moderator').getByTitle('Edit role'));
    expect(screen.queryByRole('heading', { name: 'Create Role' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Edit Role' })).toBeInTheDocument();

    fireEvent.click(rowOf('Moderator').getByTitle('Delete role'));
    expect(screen.queryByRole('heading', { name: 'Edit Role' })).not.toBeInTheDocument();
    expect(screen.getByText(/This cannot be undone/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Create Role/ }));
    expect(screen.queryByText(/This cannot be undone/)).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Create Role' })).toBeInTheDocument();
  });
});
