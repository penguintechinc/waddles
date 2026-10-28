/**
 * Tests for the super-admin Manage Communities page: filterable/paginated
 * table, and the edit, reassign-owner and delete (typed-name) modals with
 * their exact API payloads, validation alerts and failure alerts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import SuperAdminCommunities from '../SuperAdminCommunities';
import { superAdminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  superAdminApi: {
    getCommunities: vi.fn(),
    updateCommunity: vi.fn(),
    reassignOwner: vi.fn(),
    deleteCommunity: vi.fn(),
  },
}));

const COMMUNITIES = [
  {
    id: 1,
    name: 'penguins',
    displayName: 'Penguin Club',
    description: 'Waddle',
    platform: 'discord',
    ownerName: 'Alice',
    memberCount: 42,
    isPremium: true,
    isActive: true,
    isPublic: true,
    seatLimit: 50,
    createdAt: '2026-02-03T12:00:00Z',
  },
  {
    id: 2,
    name: 'seals',
    displayName: 'Seal Society',
    platform: 'twitch',
    ownerName: null,
    memberCount: 7,
    isPremium: false,
    isActive: false,
    isPublic: false,
    createdAt: '2026-03-04T12:00:00Z',
  },
];

function reply(communities = COMMUNITIES, pagination = { total: communities.length, totalPages: 1 }) {
  return { data: { success: true, communities, pagination } };
}

const failure = (message) => ({ response: { data: { error: { message } } } });
const rowOf = (name) => within(screen.getByText(name).closest('tr'));
const modal = (title) => within(screen.getByRole('heading', { name: title }).closest('div.rounded-xl'));

async function loaded() {
  render(
    <MemoryRouter>
      <SuperAdminCommunities />
    </MemoryRouter>,
  );
  await screen.findByText('Penguin Club');
}

beforeEach(() => {
  vi.clearAllMocks();
  superAdminApi.getCommunities.mockResolvedValue(reply());
  for (const fn of ['updateCommunity', 'reassignOwner', 'deleteCommunity']) {
    superAdminApi[fn].mockResolvedValue({ data: { success: true } });
  }
  vi.stubGlobal('alert', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SuperAdminCommunities listing', () => {
  it('requests the first page without filters and links to community creation', async () => {
    await loaded();
    expect(superAdminApi.getCommunities).toHaveBeenCalledWith({ page: 1, limit: 25 });
    expect(screen.getByRole('link', { name: '+ Create Community' })).toHaveAttribute('href', '/communities/create');
  });

  it('renders platform, owner, members, plan, status and created date', async () => {
    await loaded();
    const penguins = rowOf('Penguin Club');
    expect(penguins.getByText('penguins')).toBeInTheDocument();
    expect(penguins.getByText(/Discord/)).toBeInTheDocument();
    expect(penguins.getByText('Alice')).toBeInTheDocument();
    expect(penguins.getByText('42')).toBeInTheDocument();
    expect(penguins.getByText(/Premium/)).toBeInTheDocument();
    expect(penguins.getByText('Active')).toBeInTheDocument();
    expect(penguins.getByText(new Date('2026-02-03T12:00:00Z').toLocaleDateString())).toBeInTheDocument();

    const seals = rowOf('Seal Society');
    expect(seals.getByText('Unassigned')).toBeInTheDocument();
    expect(seals.getByText('Standard')).toBeInTheDocument();
    expect(seals.getByText('Inactive')).toBeInTheDocument();
  });

  it('shows the empty state', async () => {
    superAdminApi.getCommunities.mockResolvedValue(reply([]));
    render(<MemoryRouter><SuperAdminCommunities /></MemoryRouter>);
    expect(await screen.findByText('No communities found')).toBeInTheDocument();
  });

  it('leaves the list empty when the API reports success:false', async () => {
    superAdminApi.getCommunities.mockResolvedValue({ data: { success: false } });
    render(<MemoryRouter><SuperAdminCommunities /></MemoryRouter>);
    expect(await screen.findByText('No communities found')).toBeInTheDocument();
  });

  it.each([
    ['the server message', failure('platform down'), 'platform down'],
    ['a generic fallback', new Error('x'), 'Failed to load communities'],
  ])('shows %s when loading fails', async (_label, err, text) => {
    superAdminApi.getCommunities.mockRejectedValue(err);
    render(<MemoryRouter><SuperAdminCommunities /></MemoryRouter>);
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('applies search, platform and status filters', async () => {
    await loaded();
    fireEvent.change(screen.getByPlaceholderText('Search communities...'), { target: { value: 'peng' } });
    await waitFor(() =>
      expect(superAdminApi.getCommunities).toHaveBeenLastCalledWith({ page: 1, limit: 25, search: 'peng' }),
    );

    const [platformSelect, statusSelect] = screen.getAllByRole('combobox');
    expect(within(platformSelect).queryByText(/Hub Chat/)).not.toBeInTheDocument();
    fireEvent.change(platformSelect, { target: { value: 'twitch' } });
    await waitFor(() =>
      expect(superAdminApi.getCommunities).toHaveBeenLastCalledWith({ page: 1, limit: 25, search: 'peng', platform: 'twitch' }),
    );
    fireEvent.change(statusSelect, { target: { value: 'false' } });
    await waitFor(() =>
      expect(superAdminApi.getCommunities).toHaveBeenLastCalledWith({
        page: 1,
        limit: 25,
        search: 'peng',
        platform: 'twitch',
        isActive: 'false',
      }),
    );
  });

  it('re-runs the search from the Search button', async () => {
    await loaded();
    const before = superAdminApi.getCommunities.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(superAdminApi.getCommunities.mock.calls.length).toBeGreaterThan(before));
  });

  it('paginates with range text and disables Previous/Next at the ends', async () => {
    superAdminApi.getCommunities.mockImplementation((params) =>
      Promise.resolve(reply(COMMUNITIES, { total: 60, totalPages: 3, page: params.page })),
    );
    await loaded();
    expect(screen.getByText('Showing 1 to 25 of 60')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Showing 26 to 50 of 60');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Showing 51 to 60 of 60');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    await screen.findByText('Showing 26 to 50 of 60');
  });
});

describe('SuperAdminCommunities edit', () => {
  async function openEdit(name = 'Penguin Club') {
    await loaded();
    fireEvent.click(rowOf(name).getByRole('button', { name: 'Edit' }));
    return modal('Edit Community');
  }

  it('pre-fills from the community', async () => {
    const m = await openEdit();
    expect(m.getByDisplayValue('Penguin Club')).toBeInTheDocument();
    expect(m.getByDisplayValue('Waddle')).toBeInTheDocument();
    expect(m.getByLabelText('Active')).toBeChecked();
    expect(m.getByLabelText('Public')).toBeChecked();
    expect(m.getByLabelText(/Premium Community/)).toBeChecked();
    expect(m.getByPlaceholderText('Leave blank for unlimited')).toHaveValue(50);
  });

  it('defaults optional fields for a sparse community', async () => {
    const m = await openEdit('Seal Society');
    expect(m.getByLabelText('Active')).not.toBeChecked();
    expect(m.getByLabelText(/Premium Community/)).not.toBeChecked();
    expect(m.getByPlaceholderText('Leave blank for unlimited')).toHaveValue(null);
  });

  it('saves the edited form, closes and reloads', async () => {
    const m = await openEdit();
    const inputs = m.getAllByRole('textbox');
    fireEvent.change(inputs[0], { target: { value: 'Emperor Club' } });
    fireEvent.change(inputs[1], { target: { value: 'Bigger' } });
    fireEvent.click(m.getByLabelText('Public'));
    fireEvent.click(m.getByLabelText(/Premium Community/));
    fireEvent.change(m.getByPlaceholderText('Leave blank for unlimited'), { target: { value: '10' } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() =>
      expect(superAdminApi.updateCommunity).toHaveBeenCalledWith(1, {
        displayName: 'Emperor Club',
        description: 'Bigger',
        isActive: true,
        isPublic: false,
        isPremium: false,
        seatLimit: '10',
      }),
    );
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Edit Community' })).not.toBeInTheDocument());
    await waitFor(() => expect(superAdminApi.getCommunities).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['the server message', failure('conflict'), 'conflict'],
    ['a generic fallback', new Error('x'), 'Failed to update community'],
  ])('alerts with %s when the update fails and keeps the modal', async (_label, err, text) => {
    superAdminApi.updateCommunity.mockRejectedValue(err);
    const m = await openEdit();
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(alert).toHaveBeenCalledWith(text));
    expect(screen.getByRole('heading', { name: 'Edit Community' })).toBeInTheDocument();
  });

  it('keeps the modal open when the API reports success:false, and Cancel closes it', async () => {
    superAdminApi.updateCommunity.mockResolvedValue({ data: { success: false } });
    const m = await openEdit();
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(superAdminApi.updateCommunity).toHaveBeenCalled());
    expect(screen.getByRole('heading', { name: 'Edit Community' })).toBeInTheDocument();
    fireEvent.click(m.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Edit Community' })).not.toBeInTheDocument();
  });
});

describe('SuperAdminCommunities reassign owner', () => {
  async function openReassign(name = 'Penguin Club') {
    await loaded();
    fireEvent.click(rowOf(name).getByRole('button', { name: 'Reassign' }));
    return modal('Reassign Owner');
  }

  it('shows the community and current owner', async () => {
    const m = await openReassign();
    expect(m.getByText('Penguin Club')).toBeInTheDocument();
    expect(m.getByText('Current owner: Alice')).toBeInTheDocument();
  });

  it('shows Unassigned for an ownerless community', async () => {
    const m = await openReassign('Seal Society');
    expect(m.getByText('Current owner: Unassigned')).toBeInTheDocument();
  });

  it('requires a new owner name', async () => {
    const m = await openReassign();
    fireEvent.change(m.getByPlaceholderText("Enter new owner's name"), { target: { value: '   ' } });
    fireEvent.submit(m.getByPlaceholderText("Enter new owner's name").closest('form'));
    await waitFor(() => expect(alert).toHaveBeenCalledWith('Owner name is required'));
    expect(superAdminApi.reassignOwner).not.toHaveBeenCalled();
  });

  it('reassigns with name and id, closes and reloads', async () => {
    const m = await openReassign();
    fireEvent.change(m.getByPlaceholderText("Enter new owner's name"), { target: { value: 'Bob' } });
    fireEvent.change(m.getByPlaceholderText('Platform user ID'), { target: { value: 'u-9' } });
    fireEvent.click(m.getByRole('button', { name: 'Reassign Owner' }));

    await waitFor(() =>
      expect(superAdminApi.reassignOwner).toHaveBeenCalledWith(1, { newOwnerId: 'u-9', newOwnerName: 'Bob' }),
    );
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Reassign Owner' })).not.toBeInTheDocument());
    await waitFor(() => expect(superAdminApi.getCommunities).toHaveBeenCalledTimes(2));
  });

  it('sends a null id when none is given', async () => {
    const m = await openReassign();
    fireEvent.change(m.getByPlaceholderText("Enter new owner's name"), { target: { value: 'Bob' } });
    fireEvent.click(m.getByRole('button', { name: 'Reassign Owner' }));
    await waitFor(() =>
      expect(superAdminApi.reassignOwner).toHaveBeenCalledWith(1, { newOwnerId: null, newOwnerName: 'Bob' }),
    );
  });

  it.each([
    ['the server message', failure('not a member'), 'not a member'],
    ['a generic fallback', new Error('x'), 'Failed to reassign owner'],
  ])('alerts with %s on failure', async (_label, err, text) => {
    superAdminApi.reassignOwner.mockRejectedValue(err);
    const m = await openReassign();
    fireEvent.change(m.getByPlaceholderText("Enter new owner's name"), { target: { value: 'Bob' } });
    fireEvent.click(m.getByRole('button', { name: 'Reassign Owner' }));
    await waitFor(() => expect(alert).toHaveBeenCalledWith(text));
  });

  it('closes from Cancel', async () => {
    const m = await openReassign();
    fireEvent.click(m.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Reassign Owner' })).not.toBeInTheDocument();
  });
});

describe('SuperAdminCommunities delete', () => {
  async function openDelete() {
    await loaded();
    fireEvent.click(rowOf('Penguin Club').getByRole('button', { name: 'Delete' }));
    return modal('Delete Community');
  }

  it('requires the exact community name to enable the confirm button', async () => {
    const m = await openDelete();
    const confirm = m.getByRole('button', { name: 'Delete Community' });
    expect(confirm).toBeDisabled();
    fireEvent.change(m.getByPlaceholderText('Type community name to confirm'), { target: { value: 'pengu' } });
    expect(confirm).toBeDisabled();
    fireEvent.change(m.getByPlaceholderText('Type community name to confirm'), { target: { value: 'penguins' } });
    expect(confirm).toBeEnabled();
  });

  it('alerts and does nothing when a wrong name is submitted', async () => {
    const m = await openDelete();
    const input = m.getByPlaceholderText('Type community name to confirm');
    fireEvent.change(input, { target: { value: 'wrong' } });
    fireEvent.submit(input.closest('form'));
    await waitFor(() => expect(alert).toHaveBeenCalledWith('Community name does not match'));
    expect(superAdminApi.deleteCommunity).not.toHaveBeenCalled();
  });

  it('deletes by id, closes and reloads', async () => {
    const m = await openDelete();
    fireEvent.change(m.getByPlaceholderText('Type community name to confirm'), { target: { value: 'penguins' } });
    fireEvent.click(m.getByRole('button', { name: 'Delete Community' }));

    await waitFor(() => expect(superAdminApi.deleteCommunity).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Delete Community' })).not.toBeInTheDocument());
    await waitFor(() => expect(superAdminApi.getCommunities).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['the server message', failure('has members'), 'has members'],
    ['a generic fallback', new Error('x'), 'Failed to delete community'],
  ])('alerts with %s on failure', async (_label, err, text) => {
    superAdminApi.deleteCommunity.mockRejectedValue(err);
    const m = await openDelete();
    fireEvent.change(m.getByPlaceholderText('Type community name to confirm'), { target: { value: 'penguins' } });
    fireEvent.click(m.getByRole('button', { name: 'Delete Community' }));
    await waitFor(() => expect(alert).toHaveBeenCalledWith(text));
  });

  it('closes from Cancel without deleting', async () => {
    const m = await openDelete();
    fireEvent.click(m.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Delete Community' })).not.toBeInTheDocument();
    expect(superAdminApi.deleteCommunity).not.toHaveBeenCalled();
  });
});
