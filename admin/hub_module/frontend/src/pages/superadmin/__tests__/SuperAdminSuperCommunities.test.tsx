/**
 * Tests for the super-admin cross-tenant Communities page: flag gating,
 * searchable/status-filtered/paginated list, edit modal payload, and the
 * typed-name deactivation confirmation, incl. failure handling.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useFeatureFlag } from '../../../lib/useFeatureFlag';
import {
  platformCommunitiesApi,
  type ListPlatformCommunitiesResponse,
  type PlatformCommunityDTO,
} from '../../../services/platformCommunitiesApi';
import SuperAdminSuperCommunities from '../SuperAdminSuperCommunities';

vi.mock('../../../lib/useFeatureFlag', () => ({ useFeatureFlag: vi.fn() }));
vi.mock('../../../services/platformCommunitiesApi', () => ({
  platformCommunitiesApi: { list: vi.fn(), get: vi.fn(), update: vi.fn(), deactivate: vi.fn() },
}));

const flag = vi.mocked(useFeatureFlag);
const api = vi.mocked(platformCommunitiesApi);

const COMMUNITIES: PlatformCommunityDTO[] = [
  {
    id: 1,
    name: 'penguins',
    displayName: 'Penguin Club',
    description: 'Waddle waddle',
    primaryPlatform: 'discord',
    memberCount: 42,
    isPublic: true,
    isActive: true,
    createdAt: '2026-02-03T12:00:00Z',
  },
  {
    id: 2,
    name: null,
    displayName: null,
    description: null,
    primaryPlatform: null,
    memberCount: 3,
    isPublic: false,
    isActive: false,
    createdAt: null,
  },
];

function page(
  communities: PlatformCommunityDTO[],
  pagination = { page: 1, limit: 25, total: communities.length, totalPages: 1 },
): ListPlatformCommunitiesResponse {
  return { success: true, communities, pagination };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const mount = () => render(<SuperAdminSuperCommunities />, { wrapper });
const rowOf = (text: string) => within(screen.getByText(text).closest('tr') as HTMLElement);
const OK = { success: true, message: 'ok' };

beforeEach(() => {
  vi.clearAllMocks();
  flag.mockReturnValue(true);
  api.list.mockResolvedValue(page(COMMUNITIES));
  api.update.mockResolvedValue(OK);
  api.deactivate.mockResolvedValue(OK);
  vi.spyOn(console, 'debug').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('SuperAdminSuperCommunities listing', () => {
  it('shows a notice and fetches nothing while the flag is off', () => {
    flag.mockReturnValue(false);
    mount();
    expect(screen.getByRole('status')).toHaveTextContent('not yet available');
    expect(api.list).not.toHaveBeenCalled();
  });

  it('loads active communities first, then renders rows', async () => {
    mount();
    expect(screen.getByRole('status', { name: 'Loading communities' })).toBeInTheDocument();
    await screen.findByText('Penguin Club');
    expect(api.list).toHaveBeenCalledWith({ page: 1, limit: 25, search: '', isActive: true });

    const club = rowOf('Penguin Club');
    expect(club.getByText('penguins')).toBeInTheDocument();
    expect(club.getByText('discord')).toBeInTheDocument();
    expect(club.getByText('42')).toBeInTheDocument();
    expect(club.getByText('Public')).toBeInTheDocument();
    expect(club.getByText('Active')).toBeInTheDocument();
    expect(club.getByText(new Date('2026-02-03T12:00:00Z').toLocaleDateString())).toBeInTheDocument();
  });

  it('falls back to #id, dashes and Private/Inactive for sparse rows', async () => {
    mount();
    await screen.findByText('Penguin Club');
    const sparse = rowOf('#2');
    expect(sparse.getAllByText('—')).toHaveLength(2);
    expect(sparse.getByText('Private')).toBeInTheDocument();
    expect(sparse.getByText('Inactive')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit 2' })).toBeInTheDocument();
  });

  it('only offers Deactivate for active communities', async () => {
    mount();
    await screen.findByText('Penguin Club');
    expect(screen.getByRole('button', { name: 'Deactivate Penguin Club' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Deactivate 2' })).not.toBeInTheDocument();
  });

  it('shows the empty state', async () => {
    api.list.mockResolvedValue(page([]));
    mount();
    expect(await screen.findByText('No communities found')).toBeInTheDocument();
  });

  it.each([
    ['the error message', new Error('platform down'), 'platform down'],
    ['a generic fallback for a non-Error rejection', 'weird', 'Failed to load communities'],
  ])('shows %s when loading fails', async (_label, err, text) => {
    api.list.mockRejectedValue(err);
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
  });

  it('searches by the typed term and resets to page 1', async () => {
    mount();
    await screen.findByText('Penguin Club');
    fireEvent.change(screen.getByLabelText('Search communities'), { target: { value: 'peng' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() =>
      expect(api.list).toHaveBeenLastCalledWith({ page: 1, limit: 25, search: 'peng', isActive: true }),
    );
  });

  it('switches the status filter to inactive', async () => {
    mount();
    await screen.findByText('Penguin Club');
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'false' } });
    await waitFor(() =>
      expect(api.list).toHaveBeenLastCalledWith({ page: 1, limit: 25, search: '', isActive: false }),
    );
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'true' } });
    await waitFor(() => expect(api.list).toHaveBeenLastCalledWith(expect.objectContaining({ isActive: true })));
  });

  it('paginates with range text and disables Previous/Next at the ends', async () => {
    api.list.mockImplementation((params) =>
      Promise.resolve(page(COMMUNITIES, { page: params?.page ?? 1, limit: 25, total: 55, totalPages: 3 })),
    );
    mount();
    await screen.findByText('Showing 1 to 25 of 55');
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Showing 26 to 50 of 55');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Showing 51 to 55 of 55');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    await screen.findByText('Showing 26 to 50 of 55');
    expect(api.list).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }));
  });
});

describe('SuperAdminSuperCommunities edit', () => {
  async function openEdit(name = 'Penguin Club') {
    mount();
    await screen.findByText('Penguin Club');
    fireEvent.click(screen.getByRole('button', { name: `Edit ${name}` }));
    return within(screen.getByRole('dialog'));
  }

  it('pre-fills the modal from the community', async () => {
    const dialog = await openEdit();
    expect(dialog.getByRole('heading', { name: 'Edit Community' })).toBeInTheDocument();
    expect(dialog.getByLabelText('Display Name')).toHaveValue('Penguin Club');
    expect(dialog.getByLabelText('Description')).toHaveValue('Waddle waddle');
    expect(dialog.getByLabelText('Active')).toBeChecked();
    expect(dialog.getByLabelText('Public')).toBeChecked();
  });

  it('defaults blank fields for a sparse community', async () => {
    const dialog = await openEdit('2');
    expect(dialog.getByLabelText('Display Name')).toHaveValue('');
    expect(dialog.getByLabelText('Description')).toHaveValue('');
    expect(dialog.getByLabelText('Active')).not.toBeChecked();
  });

  it('saves the edited fields, closes and refreshes the list', async () => {
    const dialog = await openEdit();
    fireEvent.change(dialog.getByLabelText('Display Name'), { target: { value: 'Emperor Club' } });
    fireEvent.change(dialog.getByLabelText('Description'), { target: { value: 'Bigger' } });
    fireEvent.click(dialog.getByLabelText('Public'));
    fireEvent.click(dialog.getByLabelText('Active'));
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() =>
      expect(api.update).toHaveBeenCalledWith(1, {
        displayName: 'Emperor Club',
        description: 'Bigger',
        isPublic: false,
        isActive: false,
      }),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it('shows Saving... while pending', async () => {
    let release: (value: typeof OK) => void = () => undefined;
    api.update.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const dialog = await openEdit();
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));
    expect(await screen.findByRole('button', { name: 'Saving...' })).toBeDisabled();
    release(OK);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('keeps the modal open when the update fails, and can be cancelled', async () => {
    api.update.mockRejectedValue(new Error('conflict'));
    const dialog = await openEdit();
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(api.update).toHaveBeenCalled());
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('SuperAdminSuperCommunities deactivate', () => {
  async function openDeactivate() {
    mount();
    await screen.findByText('Penguin Club');
    fireEvent.click(screen.getByRole('button', { name: 'Deactivate Penguin Club' }));
    return within(screen.getByRole('dialog'));
  }

  it('requires the exact community name to enable the confirm button', async () => {
    const dialog = await openDeactivate();
    const confirm = dialog.getByRole('button', { name: 'Deactivate Community' });
    expect(confirm).toBeDisabled();
    fireEvent.change(dialog.getByLabelText('Confirm community name'), { target: { value: 'pengu' } });
    expect(confirm).toBeDisabled();
    fireEvent.change(dialog.getByLabelText('Confirm community name'), { target: { value: 'penguins' } });
    expect(confirm).toBeEnabled();
  });

  it('ignores a submit with the wrong name', async () => {
    const dialog = await openDeactivate();
    fireEvent.change(dialog.getByLabelText('Confirm community name'), { target: { value: 'nope' } });
    fireEvent.submit(dialog.getByLabelText('Confirm community name').closest('form') as HTMLFormElement);
    expect(api.deactivate).not.toHaveBeenCalled();
  });

  it('deactivates with the optional reason, closes and refreshes', async () => {
    const dialog = await openDeactivate();
    fireEvent.change(dialog.getByLabelText('Confirm community name'), { target: { value: 'penguins' } });
    fireEvent.change(dialog.getByLabelText('Reason (optional)'), { target: { value: 'abuse' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Deactivate Community' }));

    await waitFor(() => expect(api.deactivate).toHaveBeenCalledWith(1, 'abuse'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it('omits the reason when left blank', async () => {
    const dialog = await openDeactivate();
    fireEvent.change(dialog.getByLabelText('Confirm community name'), { target: { value: 'penguins' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Deactivate Community' }));
    await waitFor(() => expect(api.deactivate).toHaveBeenCalledWith(1, undefined));
  });

  it('shows Deactivating... while pending and stays open on failure', async () => {
    let fail: (reason: Error) => void = () => undefined;
    api.deactivate.mockReturnValue(new Promise((_resolve, reject) => { fail = reject; }));
    const dialog = await openDeactivate();
    fireEvent.change(dialog.getByLabelText('Confirm community name'), { target: { value: 'penguins' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Deactivate Community' }));

    expect(await screen.findByRole('button', { name: 'Deactivating...' })).toBeDisabled();
    fail(new Error('nope'));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Deactivating...' })).not.toBeInTheDocument());
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('cancel closes the dialog without calling the API', async () => {
    const dialog = await openDeactivate();
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api.deactivate).not.toHaveBeenCalled();
  });
});
