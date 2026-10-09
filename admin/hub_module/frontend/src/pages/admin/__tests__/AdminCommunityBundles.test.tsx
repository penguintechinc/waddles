/**
 * Tests for the community Bundles admin page (App Bundle lifecycle, community
 * tier): feature-flag + route gating, activation list with the read-only
 * command-flag badge, admin-only controls, and the activate / deactivate
 * mutations including failure surfacing.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAuth } from '../../../contexts/AuthContext';
import { useFeatureFlag } from '../../../lib/useFeatureFlag';
import { bundleActivationApi, type BundleActivation } from '../../../services/bundleActivationApi';
import AdminCommunityBundles from '../AdminCommunityBundles';

vi.mock('../../../lib/useFeatureFlag', () => ({ useFeatureFlag: vi.fn() }));
vi.mock('../../../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('@penguintechinc/react-libs', async () => ({
  FormModalBuilder: (await import('../../../test/formModalStub')).FormModalStub,
}));
vi.mock('../../../services/bundleActivationApi', () => ({
  bundleActivationApi: { list: vi.fn(), activate: vi.fn(), deactivate: vi.fn() },
}));

const flag = vi.mocked(useFeatureFlag);
const auth = vi.mocked(useAuth);
const api = vi.mocked(bundleActivationApi);

const ROWS: BundleActivation[] = [
  { appId: 'waddles.core.ping', communityId: 7, tenantId: 1, versionId: 3, activatedAt: '2026-03-04T10:30:00Z' },
  { appId: 'waddles.core.pong', communityId: 7, tenantId: 1, versionId: 4, activatedAt: null },
];

let flags: Record<string, boolean>;

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function mount(communityId = '7') {
  return render(
    <MemoryRouter initialEntries={[`/admin/${communityId}/bundles`]}>
      <Routes>
        <Route path="/admin/:communityId/bundles" element={<AdminCommunityBundles />} />
      </Routes>
    </MemoryRouter>,
    { wrapper },
  );
}

function setAdmin(isAdmin: boolean): void {
  auth.mockReturnValue({ isCommunityAdmin: () => isAdmin } as ReturnType<typeof useAuth>);
}

beforeEach(() => {
  vi.clearAllMocks();
  flags = { 'waddles.webui.community_bundles': true };
  flag.mockImplementation((key) => flags[key] ?? false);
  setAdmin(true);
  api.list.mockResolvedValue(ROWS);
  api.activate.mockResolvedValue({ success: true, message: 'ok' });
  api.deactivate.mockResolvedValue({ success: true, message: 'ok' });
  vi.spyOn(console, 'debug').mockImplementation(() => undefined);
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AdminCommunityBundles gating', () => {
  it('shows a not-available notice and fetches nothing while the flag is off', () => {
    flags = {};
    mount();
    expect(screen.getByRole('status')).toHaveTextContent('not yet available');
    expect(api.list).not.toHaveBeenCalled();
  });

  it('rejects a non-numeric community id without fetching', () => {
    mount('not-a-number');
    expect(screen.getByRole('alert')).toHaveTextContent('Invalid community.');
    expect(api.list).not.toHaveBeenCalled();
  });
});

describe('AdminCommunityBundles listing', () => {
  it('loads activations for the numeric community id', async () => {
    mount();
    expect(screen.getByRole('status')).toHaveTextContent('Loading bundles');
    expect(await screen.findByTestId('bundle-row-waddles.core.ping')).toBeInTheDocument();
    expect(api.list).toHaveBeenCalledWith(7);
  });

  it('renders each activation with its date, or "unknown" when none', async () => {
    mount();
    const ping = within(await screen.findByTestId('bundle-row-waddles.core.ping'));
    expect(ping.getByText('waddles.core.ping')).toBeInTheDocument();
    expect(ping.getByText(new Date('2026-03-04T10:30:00Z').toLocaleString(), { exact: false })).toBeInTheDocument();
    expect(within(screen.getByTestId('bundle-row-waddles.core.pong')).getByText(/unknown/)).toBeInTheDocument();
  });

  it('shows the read-only PostHog command flag state per bundle', async () => {
    flags['waddles.command-waddles.core.ping'] = true;
    mount();
    expect(within(await screen.findByTestId('bundle-row-waddles.core.ping')).getByText('command flag: on')).toBeInTheDocument();
    expect(within(screen.getByTestId('bundle-row-waddles.core.pong')).getByText('command flag: off')).toBeInTheDocument();
  });

  it('shows the empty state', async () => {
    api.list.mockResolvedValue([]);
    mount();
    expect(await screen.findByText('No bundles activated for this community yet.')).toBeInTheDocument();
  });

  it('shows a failure alert when the list cannot be loaded', async () => {
    api.list.mockRejectedValue(new Error('boom'));
    mount();
    expect(await screen.findByText('Failed to load bundles for this community.')).toBeInTheDocument();
    expect(screen.queryByText(/No bundles activated/)).not.toBeInTheDocument();
  });

  it('hides management controls from non-admins', async () => {
    setAdmin(false);
    mount();
    await screen.findByTestId('bundle-row-waddles.core.ping');
    expect(screen.queryByRole('button', { name: /Activate a new bundle/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Deactivate waddles/ })).not.toBeInTheDocument();
  });
});

describe('AdminCommunityBundles activate', () => {
  async function openActivate() {
    mount();
    await screen.findByTestId('bundle-row-waddles.core.ping');
    fireEvent.click(screen.getByRole('button', { name: 'Activate a new bundle for this community' }));
    return screen.getByRole('dialog', { name: 'Activate Bundle' });
  }

  it('activates the trimmed app id, closes the modal and refetches the list', async () => {
    const dialog = within(await openActivate());
    fireEvent.change(dialog.getByLabelText('App ID'), { target: { value: '  waddles.core.new  ' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Activate' }));

    await waitFor(() => expect(api.activate).toHaveBeenCalledWith(7, 'waddles.core.new'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Activate Bundle' })).not.toBeInTheDocument());
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it('ignores a blank app id', async () => {
    const dialog = within(await openActivate());
    fireEvent.change(dialog.getByLabelText('App ID'), { target: { value: '   ' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Activate' }));

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(api.activate).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'Activate Bundle' })).toBeInTheDocument();
  });

  it('shows a failure alert and keeps the modal open when activation fails', async () => {
    api.activate.mockRejectedValue(new Error('conflict'));
    const dialog = within(await openActivate());
    fireEvent.change(dialog.getByLabelText('App ID'), { target: { value: 'waddles.core.new' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Activate' }));

    expect(await screen.findByText('Failed to activate bundle. Please try again.')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Activate Bundle' })).toBeInTheDocument();
    expect(console.error).toHaveBeenCalledWith(
      '[CommunityBundles] ActivateFailed',
      expect.objectContaining({ communityId: 7, appId: 'waddles.core.new' }),
    );
  });

  it('closes the modal from Cancel', async () => {
    const dialog = within(await openActivate());
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog', { name: 'Activate Bundle' })).not.toBeInTheDocument();
  });
});

describe('AdminCommunityBundles deactivate', () => {
  async function askDeactivate() {
    mount();
    await screen.findByTestId('bundle-row-waddles.core.ping');
    fireEvent.click(screen.getByRole('button', { name: 'Deactivate waddles.core.ping' }));
    return screen.getByRole('dialog', { name: 'Deactivate waddles.core.ping?' });
  }

  it('confirms by app id, deactivates, closes and refetches', async () => {
    const dialog = within(await askDeactivate());
    fireEvent.click(dialog.getByRole('button', { name: 'Deactivate' }));

    await waitFor(() => expect(api.deactivate).toHaveBeenCalledWith(7, 'waddles.core.ping'));
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Deactivate waddles.core.ping?' })).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it('shows a failure alert when deactivation fails', async () => {
    api.deactivate.mockRejectedValue(new Error('nope'));
    const dialog = within(await askDeactivate());
    fireEvent.click(dialog.getByRole('button', { name: 'Deactivate' }));

    expect(await screen.findByText('Failed to deactivate bundle. Please try again.')).toBeInTheDocument();
    expect(console.error).toHaveBeenCalledWith(
      '[CommunityBundles] DeactivateFailed',
      expect.objectContaining({ appId: 'waddles.core.ping' }),
    );
  });

  it('dismisses the confirmation without calling the API', async () => {
    const dialog = within(await askDeactivate());
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(api.deactivate).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
