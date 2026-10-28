/**
 * Tests for the tenant Bundle Catalog page (App Bundle lifecycle, tenant
 * tier): flag/route gating, availability table, role-gated management UI,
 * the make-available form and enable/disable toggles, incl. API error envelope
 * surfacing.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAuth } from '../../../contexts/AuthContext';
import { useFeatureFlag } from '../../../lib/useFeatureFlag';
import { bundleAvailabilityApi, type BundleAvailability } from '../../../services/bundleAvailabilityApi';
import TenantBundleCatalog from '../TenantBundleCatalog';

vi.mock('../../../lib/useFeatureFlag', () => ({ useFeatureFlag: vi.fn() }));
vi.mock('../../../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../../../services/bundleAvailabilityApi', () => ({
  bundleAvailabilityApi: { list: vi.fn(), enable: vi.fn(), disable: vi.fn() },
}));

const flag = vi.mocked(useFeatureFlag);
const auth = vi.mocked(useAuth);
const api = vi.mocked(bundleAvailabilityApi);

const ROWS: BundleAvailability[] = [
  { appId: 'waddles.core.ping', tenantId: 1, available: true, pinnedVersionId: 12, updatedAt: '2026-03-04T10:30:00Z' },
  { appId: 'waddles.core.pong', tenantId: 1, available: false, pinnedVersionId: null, updatedAt: null },
];

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function mount(path = '/tenant/acme/bundles') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/tenant/:tenantSlug/bundles" element={<TenantBundleCatalog />} />
        <Route path="/tenant/bundles" element={<TenantBundleCatalog />} />
      </Routes>
    </MemoryRouter>,
    { wrapper },
  );
}

function setAuth(opts: { tenantAdmin?: boolean; isAdmin?: boolean; isSuperAdmin?: boolean }): void {
  auth.mockReturnValue({
    hasRole: (role: string) => role === 'tenant-admin' && Boolean(opts.tenantAdmin),
    isAdmin: Boolean(opts.isAdmin),
    isSuperAdmin: Boolean(opts.isSuperAdmin),
  } as unknown as ReturnType<typeof useAuth>);
}

const envelope = (message: string) => ({ response: { data: { success: false, error: { message } } } });

beforeEach(() => {
  vi.clearAllMocks();
  flag.mockReturnValue(true);
  setAuth({ tenantAdmin: true });
  api.list.mockResolvedValue(ROWS);
  api.enable.mockResolvedValue({ success: true, message: 'ok' });
  api.disable.mockResolvedValue({ success: true, message: 'ok' });
  vi.spyOn(console, 'debug').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('TenantBundleCatalog gating', () => {
  it('shows a not-available notice and fetches nothing while the flag is off', () => {
    flag.mockReturnValue(false);
    mount();
    expect(screen.getByRole('status')).toHaveTextContent('not yet available');
    expect(api.list).not.toHaveBeenCalled();
  });

  it('reports a missing tenant slug in the route', () => {
    mount('/tenant/bundles');
    expect(screen.getByRole('alert')).toHaveTextContent('Missing tenant in route.');
    expect(api.list).not.toHaveBeenCalled();
  });
});

describe('TenantBundleCatalog listing', () => {
  it('loads availability for the tenant slug and renders the table', async () => {
    mount();
    expect(screen.getByText('Loading bundles...')).toBeInTheDocument();

    const table = await screen.findByTestId('bundle-availability-table');
    expect(api.list).toHaveBeenCalledWith('acme');
    const ping = within(within(table).getByText('waddles.core.ping').closest('tr') as HTMLElement);
    expect(ping.getByText('Available')).toBeInTheDocument();
    expect(ping.getByText('12')).toBeInTheDocument();
    expect(ping.getByText(new Date('2026-03-04T10:30:00Z').toLocaleString())).toBeInTheDocument();

    const pong = within(within(table).getByText('waddles.core.pong').closest('tr') as HTMLElement);
    expect(pong.getByText('Unavailable')).toBeInTheDocument();
    expect(pong.getAllByText('—')).toHaveLength(2);
  });

  it('shows the empty state', async () => {
    api.list.mockResolvedValue([]);
    mount();
    expect(await screen.findByText(/No bundles have an availability record/)).toBeInTheDocument();
  });

  it.each([
    ['the API error message', envelope('tenant suspended'), 'tenant suspended'],
    ['a generic fallback', new Error('x'), 'Failed to load bundle availability.'],
  ])('shows %s when the list fails', async (_label, err, text) => {
    api.list.mockRejectedValue(err);
    mount();
    expect(await screen.findByText(text)).toBeInTheDocument();
  });
});

describe('TenantBundleCatalog permissions', () => {
  it.each([
    ['a tenant admin', { tenantAdmin: true }],
    ['an admin', { isAdmin: true }],
    ['a super admin', { isSuperAdmin: true }],
  ])('lets %s manage availability', async (_label, opts) => {
    setAuth(opts);
    mount();
    await screen.findByTestId('bundle-availability-table');
    expect(screen.getByRole('form', { name: 'Add bundle availability' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Actions' })).toBeInTheDocument();
  });

  it('is read-only for everyone else', async () => {
    setAuth({});
    mount();
    await screen.findByTestId('bundle-availability-table');
    expect(screen.queryByRole('form', { name: 'Add bundle availability' })).not.toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Actions' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Disable|Enable/ })).not.toBeInTheDocument();
  });
});

describe('TenantBundleCatalog make-available form', () => {
  it('requires an app id', async () => {
    mount();
    await screen.findByTestId('bundle-availability-table');
    fireEvent.click(screen.getByRole('button', { name: 'Make bundle available' }));
    expect(await screen.findByText('App ID is required.')).toBeInTheDocument();
    expect(api.enable).not.toHaveBeenCalled();
  });

  it('enables the trimmed app id, clears the field and refetches', async () => {
    mount();
    await screen.findByTestId('bundle-availability-table');
    const input = screen.getByLabelText('App ID');
    fireEvent.change(input, { target: { value: '  waddles.core.new ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Make bundle available' }));

    await waitFor(() => expect(api.enable).toHaveBeenCalledWith('acme', { appId: 'waddles.core.new' }));
    await waitFor(() => expect(input).toHaveValue(''));
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it('shows Adding... while the request is in flight', async () => {
    let release: (value: { success: boolean; message: string }) => void = () => undefined;
    api.enable.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    mount();
    await screen.findByTestId('bundle-availability-table');
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'x.y' } });
    fireEvent.click(screen.getByRole('button', { name: 'Make bundle available' }));

    expect(await screen.findByRole('button', { name: 'Make bundle available' })).toHaveTextContent('Adding...');
    expect(screen.getByRole('button', { name: 'Make bundle available' })).toBeDisabled();
    release({ success: true, message: 'ok' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Make bundle available' })).toHaveTextContent('Make Available'));
  });

  it.each([
    ['the API error message', envelope('no platform install'), 'no platform install'],
    ['a generic fallback', new Error('x'), 'Failed to enable bundle.'],
  ])('surfaces %s when enabling fails, keeping the input', async (_label, err, text) => {
    api.enable.mockRejectedValue(err);
    mount();
    await screen.findByTestId('bundle-availability-table');
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'x.y' } });
    fireEvent.click(screen.getByRole('button', { name: 'Make bundle available' }));

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByLabelText('App ID')).toHaveValue('x.y');
  });
});

describe('TenantBundleCatalog toggles', () => {
  it('disables an available bundle', async () => {
    mount();
    await screen.findByTestId('bundle-availability-table');
    fireEvent.click(screen.getByRole('button', { name: 'Disable waddles.core.ping' }));

    await waitFor(() => expect(api.disable).toHaveBeenCalledWith('acme', 'waddles.core.ping'));
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
    expect(api.enable).not.toHaveBeenCalled();
  });

  it('enables an unavailable bundle', async () => {
    mount();
    await screen.findByTestId('bundle-availability-table');
    fireEvent.click(screen.getByRole('button', { name: 'Enable waddles.core.pong' }));

    await waitFor(() => expect(api.enable).toHaveBeenCalledWith('acme', { appId: 'waddles.core.pong' }));
    expect(api.disable).not.toHaveBeenCalled();
  });

  it('locks every toggle while a mutation is in flight', async () => {
    let release: (value: { success: boolean; message: string }) => void = () => undefined;
    api.disable.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    mount();
    await screen.findByTestId('bundle-availability-table');
    fireEvent.click(screen.getByRole('button', { name: 'Disable waddles.core.ping' }));

    await waitFor(() => expect(screen.getByRole('button', { name: 'Enable waddles.core.pong' })).toBeDisabled());
    release({ success: true, message: 'ok' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enable waddles.core.pong' })).toBeEnabled());
  });
});
