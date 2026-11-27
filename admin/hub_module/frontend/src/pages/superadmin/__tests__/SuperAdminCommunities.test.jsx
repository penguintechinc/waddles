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
import { fetchCommunityMembers } from '../../../services/communityMembersApi';

vi.mock('../../../services/api', () => ({
  superAdminApi: {
    getCommunities: vi.fn(),
    updateCommunity: vi.fn(),
    reassignOwner: vi.fn(),
    deleteCommunity: vi.fn(),
  },
}));

vi.mock('../../../services/communityMembersApi', () => ({
  fetchCommunityMembers: vi.fn(),
}));

const MEMBERS = [
  { userId: '11111111-1111-4111-8111-111111111111', username: 'alice', roleName: 'community-owner' },
  { userId: '22222222-2222-4222-8222-222222222222', username: 'bob', roleName: 'member' },
];

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
    licenseTier: 2,
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
    licenseTier: 1,
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
  fetchCommunityMembers.mockResolvedValue({ members: MEMBERS, page: 1, limit: 100, total: 2, totalPages: 1 });
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
    expect(penguins.getByText('Premium')).toBeInTheDocument();
    expect(penguins.getByText('Active')).toBeInTheDocument();
    expect(penguins.getByText(new Date('2026-02-03T12:00:00Z').toLocaleDateString())).toBeInTheDocument();

    const seals = rowOf('Seal Society');
    expect(seals.getByText('Unassigned')).toBeInTheDocument();
    expect(seals.getByText('Free')).toBeInTheDocument();
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
    expect(m.getByLabelText('License tier')).toHaveValue('2');
    expect(m.queryByLabelText(/Premium Community/)).not.toBeInTheDocument();
    expect(m.getByPlaceholderText('Leave blank for unlimited')).toHaveValue('50');
  });

  it('defaults optional fields for a sparse community', async () => {
    const m = await openEdit('Seal Society');
    expect(m.getByLabelText('Active')).not.toBeChecked();
    expect(m.getByLabelText('License tier')).toHaveValue('1');
    expect(m.getByPlaceholderText('Leave blank for unlimited')).toHaveValue('');
  });

  it('saves the edited form, closes and reloads', async () => {
    const m = await openEdit();
    const inputs = m.getAllByRole('textbox');
    fireEvent.change(inputs[0], { target: { value: 'Emperor Club' } });
    fireEvent.change(inputs[1], { target: { value: 'Bigger' } });
    fireEvent.click(m.getByLabelText('Public'));
    fireEvent.change(m.getByPlaceholderText('Leave blank for unlimited'), { target: { value: '10' } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() =>
      expect(superAdminApi.updateCommunity).toHaveBeenCalledWith(1, {
        displayName: 'Emperor Club',
        description: 'Bigger',
        isActive: true,
        isPublic: false,
        seatLimit: 10,
      }),
    );
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Edit Community' })).not.toBeInTheDocument());
    await waitFor(() => expect(superAdminApi.getCommunities).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['the server message', failure('conflict'), 'conflict'],
    ['a generic fallback', new Error('x'), 'Failed to update community'],
  ])('shows %s inline when the update fails and keeps the modal', async (_label, err, text) => {
    superAdminApi.updateCommunity.mockRejectedValue(err);
    const m = await openEdit();
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    expect(await m.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Edit Community' })).toBeInTheDocument();
  });

  // regression: a blank seat limit was sent as '' (then 0/omitted); now cleared = explicit null.
  it('sends explicit seatLimit null when a previously set limit is cleared', async () => {
    const m = await openEdit();
    fireEvent.change(m.getByPlaceholderText('Leave blank for unlimited'), { target: { value: '' } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(superAdminApi.updateCommunity).toHaveBeenCalled());
    const [, payload] = superAdminApi.updateCommunity.mock.calls[0];
    expect(payload).toHaveProperty('seatLimit', null);
  });

  it('omits seatLimit when unchanged (absent vs null is meaningful)', async () => {
    const m = await openEdit();
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(superAdminApi.updateCommunity).toHaveBeenCalled());
    expect(superAdminApi.updateCommunity.mock.calls[0][1]).not.toHaveProperty('seatLimit');
    expect(superAdminApi.updateCommunity.mock.calls[0][1]).not.toHaveProperty('licenseTier');
  });

  it('omits seatLimit for a blank field that was never set', async () => {
    const m = await openEdit('Seal Society');
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(superAdminApi.updateCommunity).toHaveBeenCalled());
    expect(superAdminApi.updateCommunity.mock.calls[0][1]).not.toHaveProperty('seatLimit');
  });

  it('sends description null when cleared', async () => {
    const m = await openEdit();
    fireEvent.change(m.getByLabelText('Description'), { target: { value: '' } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(superAdminApi.updateCommunity).toHaveBeenCalled());
    expect(superAdminApi.updateCommunity.mock.calls[0][1]).toHaveProperty('description', null);
  });

  it('trims the seat limit and sends it as an integer', async () => {
    const m = await openEdit('Seal Society');
    fireEvent.change(m.getByPlaceholderText('Leave blank for unlimited'), { target: { value: ' 25 ' } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(superAdminApi.updateCommunity).toHaveBeenCalled());
    expect(superAdminApi.updateCommunity.mock.calls[0][1].seatLimit).toBe(25);
  });

  it.each(['-5', '1.5', '0', '1e3', 'abc'])('rejects seat limit %s inline without calling the API', async (bad) => {
    const m = await openEdit('Seal Society');
    fireEvent.change(m.getByPlaceholderText('Leave blank for unlimited'), { target: { value: bad } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    expect(await m.findByText(/Seat limit must be a whole number/)).toBeInTheDocument();
    expect(superAdminApi.updateCommunity).not.toHaveBeenCalled();
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
    const m = modal('Reassign Owner');
    await m.findByRole('option', { name: 'bob (member)' });
    return m;
  }

  it('shows the community and current owner (display only)', async () => {
    const m = await openReassign();
    expect(m.getByText('Penguin Club')).toBeInTheDocument();
    expect(m.getByText('Current owner: Alice')).toBeInTheDocument();
  });

  it('shows Unassigned for an ownerless community', async () => {
    const m = await openReassign('Seal Society');
    expect(m.getByText('Current owner: Unassigned')).toBeInTheDocument();
  });

  it('offers no free-text owner name or id inputs', async () => {
    const m = await openReassign();
    expect(m.queryByPlaceholderText("Enter new owner's name")).not.toBeInTheDocument();
    expect(m.queryByPlaceholderText('Platform user ID')).not.toBeInTheDocument();
  });

  it("loads the community's members by its id and re-queries on search", async () => {
    const m = await openReassign();
    expect(fetchCommunityMembers).toHaveBeenCalledWith(1, { page: 1, limit: 100, search: '', roleId: '' });
    fireEvent.change(m.getByPlaceholderText('Search members'), { target: { value: 'bo' } });
    await waitFor(() =>
      expect(fetchCommunityMembers).toHaveBeenCalledWith(1, { page: 1, limit: 100, search: 'bo', roleId: '' }),
    );
  });

  it('requires a member to be selected', async () => {
    const m = await openReassign();
    fireEvent.submit(m.getByLabelText('New Owner *').closest('form'));
    await waitFor(() => expect(alert).toHaveBeenCalledWith('Select a member to become the owner'));
    expect(superAdminApi.reassignOwner).not.toHaveBeenCalled();
  });

  it('transfers by user UUID only, closes and reloads', async () => {
    const m = await openReassign();
    fireEvent.change(m.getByLabelText('New Owner *'), { target: { value: MEMBERS[1].userId } });
    fireEvent.click(m.getByRole('button', { name: 'Reassign Owner' }));

    await waitFor(() =>
      expect(superAdminApi.reassignOwner).toHaveBeenCalledWith(1, { newOwnerUserId: MEMBERS[1].userId }),
    );
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Reassign Owner' })).not.toBeInTheDocument());
    await waitFor(() => expect(superAdminApi.getCommunities).toHaveBeenCalledTimes(2));
  });

  it('says so when the member list cannot be loaded', async () => {
    fetchCommunityMembers.mockRejectedValue(new Error('x'));
    await loaded();
    fireEvent.click(rowOf('Penguin Club').getByRole('button', { name: 'Reassign' }));
    expect(await screen.findByText('Failed to load community members')).toBeInTheDocument();
  });

  it.each([
    ['the server message', failure('not a member'), 'not a member'],
    ['a generic fallback', new Error('x'), 'Failed to reassign owner'],
  ])('alerts with %s on failure', async (_label, err, text) => {
    superAdminApi.reassignOwner.mockRejectedValue(err);
    const m = await openReassign();
    fireEvent.change(m.getByLabelText('New Owner *'), { target: { value: MEMBERS[1].userId } });
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

describe('SuperAdminCommunities license tier', () => {
  async function openEdit(name = 'Seal Society') {
    await loaded();
    fireEvent.click(rowOf(name).getByRole('button', { name: 'Edit' }));
    return modal('Edit Community');
  }

  it('offers only Free / Premium with operator-override helper text', async () => {
    const m = await openEdit();
    const options = within(m.getByLabelText('License tier')).getAllByRole('option').map((o) => o.textContent);
    expect(options).toEqual(['Free', 'Premium']);
    expect(m.getByText(/Operator override: not blocked by the tenant's pool; counts against it and is audited\./)).toBeInTheDocument();
  });

  it('does not ask for a reason until the tier changes', async () => {
    const m = await openEdit();
    expect(m.queryByLabelText('Reason for tier change')).not.toBeInTheDocument();
    fireEvent.change(m.getByLabelText('License tier'), { target: { value: '2' } });
    expect(m.getByLabelText('Reason for tier change')).toBeInTheDocument();
    fireEvent.change(m.getByLabelText('License tier'), { target: { value: '1' } });
    expect(m.queryByLabelText('Reason for tier change')).not.toBeInTheDocument();
  });

  it('offers the seven reason codes with human labels', async () => {
    const m = await openEdit();
    fireEvent.change(m.getByLabelText('License tier'), { target: { value: '2' } });
    const values = within(m.getByLabelText('Reason for tier change')).getAllByRole('option').map((o) => o.getAttribute('value'));
    expect(values).toEqual([
      '', 'support_request', 'sales_agreement', 'trial_extension', 'billing_correction',
      'partner_program', 'data_correction', 'other',
    ]);
    expect(m.getByRole('option', { name: 'Sales agreement' })).toBeInTheDocument();
  });

  it('blocks saving a tier change without a reason', async () => {
    const m = await openEdit();
    fireEvent.change(m.getByLabelText('License tier'), { target: { value: '2' } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    expect(await m.findByText('Select a reason for the tier change.')).toBeInTheDocument();
    expect(superAdminApi.updateCommunity).not.toHaveBeenCalled();
  });

  it('sends licenseTier + reasonCode and never isPremium', async () => {
    const m = await openEdit();
    fireEvent.change(m.getByLabelText('License tier'), { target: { value: '2' } });
    fireEvent.change(m.getByLabelText('Reason for tier change'), { target: { value: 'sales_agreement' } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(superAdminApi.updateCommunity).toHaveBeenCalled());
    const [id, payload] = superAdminApi.updateCommunity.mock.calls[0];
    expect(id).toBe(2);
    expect(payload.licenseTier).toBe(2);
    expect(payload.reasonCode).toBe('sales_agreement');
    expect(payload).not.toHaveProperty('isPremium');
  });

  it('shows the global-admin message on 403 and keeps the dialog open', async () => {
    superAdminApi.updateCommunity.mockRejectedValue({ response: { status: 403, data: {} } });
    const m = await openEdit();
    fireEvent.change(m.getByLabelText('License tier'), { target: { value: '2' } });
    fireEvent.change(m.getByLabelText('Reason for tier change'), { target: { value: 'other' } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    const alertBox = await m.findByText(/requires global admin access/);
    expect(alertBox.closest('[role="alert"]')).not.toBeNull();
    expect(screen.getByRole('heading', { name: 'Edit Community' })).toBeInTheDocument();
  });

  it('shows the 402 TENANT_TIER_REQUIRED message with the operator-pool hint in the dialog', async () => {
    superAdminApi.updateCommunity.mockRejectedValue({
      response: {
        status: 402,
        data: { error: { code: 'TENANT_TIER_REQUIRED', message: 'Premium communities require a Professional or Enterprise license' } },
      },
    });
    const m = await openEdit();
    fireEvent.change(m.getByLabelText('License tier'), { target: { value: '2' } });
    fireEvent.change(m.getByLabelText('Reason for tier change'), { target: { value: 'other' } });
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    const box = await m.findByText(/Premium communities require a Professional or Enterprise license/);
    expect(box.closest('[role="alert"]')).not.toBeNull();
    expect(box.textContent).toContain("On operator deployments set the tenant's pool first (License pools)");
    expect(screen.getByRole('heading', { name: 'Edit Community' })).toBeInTheDocument();
  });

  it('surfaces a 400 backend message in the dialog', async () => {
    superAdminApi.updateCommunity.mockRejectedValue({ response: { status: 400, data: { error: { message: 'reasonCode is invalid' } } } });
    const m = await openEdit();
    fireEvent.click(m.getByRole('button', { name: 'Save Changes' }));
    expect(await m.findByText('reasonCode is invalid')).toBeInTheDocument();
  });

  it('falls back to the legacy premium flag when licenseTier is absent', async () => {
    superAdminApi.getCommunities.mockResolvedValue(
      reply([{ ...COMMUNITIES[0], licenseTier: undefined }, COMMUNITIES[1]]),
    );
    await loaded();
    expect(rowOf('Penguin Club').getByText('Premium')).toBeInTheDocument();
  });

  it('maps an unknown tier code (3) to a Free badge in the list', async () => {
    superAdminApi.getCommunities.mockResolvedValue(reply([{ ...COMMUNITIES[0], licenseTier: 3 }]));
    await loaded();
    expect(rowOf('Penguin Club').getByText('Free')).toBeInTheDocument();
  });
});
