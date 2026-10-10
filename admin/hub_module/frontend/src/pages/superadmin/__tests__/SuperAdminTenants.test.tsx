/**
 * Tests for the super-admin Tenants page: flag gating, paginated + searchable
 * list, create / edit via form modals (payload normalisation), typed-slug
 * deactivation confirmation, and API-error surfacing.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { AxiosError, type AxiosResponse } from 'axios';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useFeatureFlag } from '../../../lib/useFeatureFlag';
import { superTenantApi, type TenantDTO } from '../../../services/superTenantApi';
import SuperAdminTenants from '../SuperAdminTenants';

vi.mock('../../../lib/useFeatureFlag', () => ({ useFeatureFlag: vi.fn() }));
vi.mock('@penguintechinc/react-libs', async () => ({
  FormModalBuilder: (await import('../../../test/formModalStub')).FormModalStub,
}));
vi.mock('../../../services/superTenantApi', () => ({
  superTenantApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), deactivate: vi.fn() },
}));

const flag = vi.mocked(useFeatureFlag);
const api = vi.mocked(superTenantApi);

const TENANTS: TenantDTO[] = [
  {
    id: 0,
    slug: 'default',
    displayName: 'Default Tenant',
    description: null,
    logoUrl: null,
    isGlobal: true,
    isActive: true,
    allowedModuleIds: null,
    seatLimit: null,
    createdAt: null,
    updatedAt: null,
  },
  {
    id: 5,
    slug: 'acme',
    displayName: 'Acme Corp',
    description: 'Roadrunners',
    logoUrl: 'https://img/acme.png',
    isGlobal: false,
    isActive: false,
    allowedModuleIds: null,
    seatLimit: 25,
    createdAt: '2026-02-03T12:00:00Z',
    updatedAt: null,
  },
];

function listResponse(tenants: TenantDTO[], pagination = { page: 1, limit: 25, total: tenants.length, totalPages: 1 }) {
  return { data: { success: true, tenants, pagination } } as AxiosResponse;
}

function axiosFailure(message?: string): AxiosError {
  const config = { headers: {} } as AxiosError['config'];
  const response = {
    status: 400,
    statusText: 'Bad Request',
    headers: {},
    config,
    data: message ? { error: { message } } : {},
  } as AxiosResponse;
  return new AxiosError('failed', AxiosError.ERR_BAD_REQUEST, config, null, response);
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const mount = () => render(<SuperAdminTenants />, { wrapper });
const rowOf = (text: string) => within(screen.getByText(text).closest('tr') as HTMLElement);
const OK = { data: { success: true, message: 'ok' } } as AxiosResponse;

beforeEach(() => {
  vi.clearAllMocks();
  flag.mockReturnValue(true);
  api.list.mockResolvedValue(listResponse(TENANTS));
  api.create.mockResolvedValue(OK);
  api.update.mockResolvedValue(OK);
  api.deactivate.mockResolvedValue(OK);
  vi.spyOn(console, 'debug').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('SuperAdminTenants gating and listing', () => {
  it('shows a notice and fetches nothing while the flag is off', () => {
    flag.mockReturnValue(false);
    mount();
    expect(screen.getByRole('status')).toHaveTextContent('not yet available');
    expect(api.list).not.toHaveBeenCalled();
  });

  it('shows a spinner then renders tenants with type, status, seat limit and created date', async () => {
    mount();
    expect(screen.getByRole('status', { name: 'Loading tenants' })).toBeInTheDocument();
    await screen.findByText('Acme Corp');
    expect(api.list).toHaveBeenCalledWith({ page: 1, limit: 25 });

    const def = rowOf('Default Tenant');
    expect(def.getByText('Default (Shared)')).toBeInTheDocument();
    expect(def.getByText('Unlimited')).toBeInTheDocument();
    expect(def.getByText('—')).toBeInTheDocument();
    expect(def.getByText('Active')).toBeInTheDocument();

    const acme = rowOf('Acme Corp');
    expect(acme.getByText('Customer Org')).toBeInTheDocument();
    expect(acme.getByText('Inactive')).toBeInTheDocument();
    expect(acme.getByText('25')).toBeInTheDocument();
    expect(acme.getByText(new Date('2026-02-03T12:00:00Z').toLocaleDateString())).toBeInTheDocument();
  });

  it('never allows deactivating the shared default tenant', async () => {
    mount();
    await screen.findByText('Acme Corp');
    expect(screen.getByRole('button', { name: 'Deactivate Default Tenant' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Deactivate Acme Corp' })).toBeEnabled();
  });

  it('shows the empty state', async () => {
    api.list.mockResolvedValue(listResponse([]));
    mount();
    expect(await screen.findByText('No tenants found')).toBeInTheDocument();
  });

  it.each([
    ['the API message', axiosFailure('db offline'), 'db offline'],
    ['a generic fallback for an API error without a message', axiosFailure(), 'Failed to load tenants'],
    ['a generic fallback for a non-API error', new Error('x'), 'Failed to load tenants'],
  ])('shows %s when loading fails', async (_label, err, text) => {
    api.list.mockRejectedValue(err);
    mount();
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('searches with a trimmed term and returns to page 1', async () => {
    mount();
    await screen.findByText('Acme Corp');
    fireEvent.change(screen.getByLabelText('Search tenants'), { target: { value: '  acme ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => expect(api.list).toHaveBeenLastCalledWith({ page: 1, limit: 25, search: 'acme' }));
  });

  it('paginates with range text and disables buttons at the ends', async () => {
    api.list.mockImplementation((params) =>
      Promise.resolve(listResponse(TENANTS, { page: params.page, limit: 25, total: 60, totalPages: 3 })),
    );
    mount();
    await screen.findByText('Showing 1 to 25 of 60');
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Showing 26 to 50 of 60');
    expect(api.list).toHaveBeenLastCalledWith({ page: 2, limit: 25 });

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Showing 51 to 60 of 60');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    await screen.findByText('Showing 26 to 50 of 60');
  });
});

describe('SuperAdminTenants create', () => {
  async function openCreate() {
    mount();
    await screen.findByText('Acme Corp');
    fireEvent.click(screen.getByRole('button', { name: 'Create tenant' }));
    return within(screen.getByRole('dialog', { name: 'Create Tenant' }));
  }

  it('normalises the slug and maps blanks to null', async () => {
    const dialog = await openCreate();
    fireEvent.change(dialog.getByLabelText('Slug'), { target: { value: ' My Tenant! ' } });
    fireEvent.change(dialog.getByLabelText('Display Name'), { target: { value: '  My Tenant  ' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Create' }));

    await waitFor(() =>
      expect(api.create).toHaveBeenCalledWith({
        slug: 'mytenant',
        displayName: 'My Tenant',
        description: null,
        seatLimit: null,
      }),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it('sends the description and a parsed seat limit when provided', async () => {
    const dialog = await openCreate();
    fireEvent.change(dialog.getByLabelText('Slug'), { target: { value: 'beta-co' } });
    fireEvent.change(dialog.getByLabelText('Display Name'), { target: { value: 'Beta' } });
    fireEvent.change(dialog.getByLabelText('Description'), { target: { value: 'Second customer' } });
    fireEvent.change(dialog.getByLabelText('Seat Limit'), { target: { value: '15' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Create' }));

    await waitFor(() =>
      expect(api.create).toHaveBeenCalledWith({
        slug: 'beta-co',
        displayName: 'Beta',
        description: 'Second customer',
        seatLimit: 15,
      }),
    );
  });

  it.each([
    ['the API message', axiosFailure('slug taken'), 'slug taken'],
    ['a generic fallback', new Error('x'), 'Failed to create tenant'],
  ])('shows %s when creation fails and keeps the modal open', async (_label, err, text) => {
    api.create.mockRejectedValue(err);
    const dialog = await openCreate();
    fireEvent.change(dialog.getByLabelText('Slug'), { target: { value: 'dup' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Create' }));

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Create Tenant' })).toBeInTheDocument();
  });

  it('closes from Cancel without calling the API', async () => {
    const dialog = await openCreate();
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api.create).not.toHaveBeenCalled();
  });
});

describe('SuperAdminTenants edit', () => {
  async function openEdit() {
    mount();
    await screen.findByText('Acme Corp');
    fireEvent.click(screen.getByRole('button', { name: 'Edit Acme Corp' }));
    return within(screen.getByRole('dialog', { name: 'Edit Tenant — acme' }));
  }

  it('pre-fills from the tenant and saves the edited fields', async () => {
    const dialog = await openEdit();
    expect(dialog.getByLabelText('Display Name')).toHaveValue('Acme Corp');
    expect(dialog.getByLabelText('Description')).toHaveValue('Roadrunners');
    expect(dialog.getByLabelText('Logo URL')).toHaveValue('https://img/acme.png');
    expect(dialog.getByLabelText('Seat Limit')).toHaveValue(25);
    expect(dialog.getByLabelText('Active')).not.toBeChecked();

    fireEvent.change(dialog.getByLabelText('Display Name'), { target: { value: ' Acme Inc ' } });
    fireEvent.change(dialog.getByLabelText('Seat Limit'), { target: { value: '' } });
    fireEvent.change(dialog.getByLabelText('Logo URL'), { target: { value: '' } });
    fireEvent.click(dialog.getByLabelText('Active'));
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() =>
      expect(api.update).toHaveBeenCalledWith(5, {
        displayName: 'Acme Inc',
        description: 'Roadrunners',
        logoUrl: null,
        seatLimit: null,
        isActive: true,
      }),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['the API message', axiosFailure('conflict'), 'conflict'],
    ['a generic fallback', new Error('x'), 'Failed to update tenant'],
  ])('shows %s when the update fails', async (_label, err, text) => {
    api.update.mockRejectedValue(err);
    const dialog = await openEdit();
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });
});

describe('SuperAdminTenants deactivate', () => {
  async function openDeactivate() {
    mount();
    await screen.findByText('Acme Corp');
    fireEvent.click(screen.getByRole('button', { name: 'Deactivate Acme Corp' }));
    return screen.getByRole('heading', { name: 'Deactivate Tenant' }).closest('div.rounded-xl') as HTMLElement;
  }

  it('requires typing the exact slug before the confirm button enables', async () => {
    const box = within(await openDeactivate());
    const confirm = box.getByRole('button', { name: 'Deactivate Tenant' });
    expect(confirm).toBeDisabled();

    fireEvent.change(box.getByLabelText('Confirm tenant slug'), { target: { value: 'acm' } });
    expect(confirm).toBeDisabled();
    fireEvent.change(box.getByLabelText('Confirm tenant slug'), { target: { value: 'acme' } });
    expect(confirm).toBeEnabled();
  });

  it('does nothing when the form is submitted with a wrong slug', async () => {
    const box = within(await openDeactivate());
    fireEvent.change(box.getByLabelText('Confirm tenant slug'), { target: { value: 'nope' } });
    fireEvent.submit(box.getByLabelText('Confirm tenant slug').closest('form') as HTMLFormElement);
    expect(api.deactivate).not.toHaveBeenCalled();
  });

  it('deactivates by id, closes the dialog and refreshes the list', async () => {
    const box = within(await openDeactivate());
    fireEvent.change(box.getByLabelText('Confirm tenant slug'), { target: { value: 'acme' } });
    fireEvent.click(box.getByRole('button', { name: 'Deactivate Tenant' }));

    await waitFor(() => expect(api.deactivate).toHaveBeenCalledWith(5));
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Deactivate Tenant' })).not.toBeInTheDocument());
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it('shows Deactivating... while pending', async () => {
    let release: (value: AxiosResponse) => void = () => undefined;
    api.deactivate.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const box = within(await openDeactivate());
    fireEvent.change(box.getByLabelText('Confirm tenant slug'), { target: { value: 'acme' } });
    fireEvent.click(box.getByRole('button', { name: 'Deactivate Tenant' }));

    expect(await screen.findByRole('button', { name: 'Deactivating...' })).toBeDisabled();
    release(OK);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Deactivating...' })).not.toBeInTheDocument());
  });

  it.each([
    ['the API message', axiosFailure('has members'), 'has members'],
    ['a generic fallback', new Error('x'), 'Failed to deactivate tenant'],
  ])('shows %s when deactivation fails', async (_label, err, text) => {
    api.deactivate.mockRejectedValue(err);
    const box = within(await openDeactivate());
    fireEvent.change(box.getByLabelText('Confirm tenant slug'), { target: { value: 'acme' } });
    fireEvent.click(box.getByRole('button', { name: 'Deactivate Tenant' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('cancel closes the dialog and clears the typed slug', async () => {
    const box = within(await openDeactivate());
    fireEvent.change(box.getByLabelText('Confirm tenant slug'), { target: { value: 'acm' } });
    fireEvent.click(box.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Deactivate Tenant' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Deactivate Acme Corp' }));
    expect(screen.getByLabelText('Confirm tenant slug')).toHaveValue('');
  });
});
