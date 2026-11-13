/**
 * Tests for the pluggable app-bundle settings page: card grid, schema-driven
 * form render, save, 409 conflict + reload, 422 field errors, 404 no-config,
 * 403, and role-aware (moderator read-only / admin edit) behaviour.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useAuth } from '../../../contexts/AuthContext';
import { useFeatureFlag } from '../../../lib/useFeatureFlag';
import { bundleActivationApi } from '../../../services/bundleActivationApi';
import { BundleAdminError, bundleAdminApi, type BundleConfig } from '../../../services/bundleAdminApi';
import AdminBundleSettings from '../AdminBundleSettings';
import { bundleName, toErrorSchema } from '../../../components/bundles/settingsHelpers';

vi.mock('../../../lib/useFeatureFlag', () => ({ useFeatureFlag: vi.fn() }));
vi.mock('../../../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../../../services/bundleActivationApi', () => ({ bundleActivationApi: { list: vi.fn() } }));
vi.mock('../../../services/bundleAdminApi', async (orig) => {
  const real = await orig<typeof import('../../../services/bundleAdminApi')>();
  return {
    ...real,
    bundleAdminApi: { descriptor: vi.fn(), getConfig: vi.fn(), saveConfig: vi.fn(), history: vi.fn() },
  };
});

const flag = vi.mocked(useFeatureFlag);
const auth = vi.mocked(useAuth);
const activation = vi.mocked(bundleActivationApi);
const api = vi.mocked(bundleAdminApi);

const GREET = 'waddles.core.social.greeter';
const PLAIN = 'waddles.core.util.ping';
const SCHEMA = {
  type: 'object',
  description: 'Welcome new members.',
  properties: {
    message: { type: 'string', title: 'Welcome message', default: 'hi' },
    enabled: { type: 'boolean', title: 'Enabled' },
  },
};
const CONFIG: BundleConfig = {
  success: true, appId: GREET, version: 2, config: { message: 'hello', enabled: true },
  updatedBy: 1, createdAt: '2026-11-10T12:00:00Z',
};

function setRole(role: string) {
  auth.mockReturnValue({ user: { communities: [{ id: 7, role }] } } as unknown as ReturnType<typeof useAuth>);
}

function mount(path = '/admin/7/settings') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/admin/:communityId/settings" element={<AdminBundleSettings />} />
        <Route path="/admin/:communityId/settings/:appId" element={<AdminBundleSettings />} />
      </Routes>
    </MemoryRouter>,
    { wrapper },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'debug').mockImplementation(() => undefined);
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  flag.mockReturnValue(true);
  setRole('community-admin');
  activation.list.mockResolvedValue([
    { appId: GREET, communityId: 7, tenantId: 1, versionId: 1, version: '1.2.0', activatedAt: null },
    { appId: PLAIN, communityId: 7, tenantId: 1, versionId: 2, version: '0.1.0', activatedAt: null },
  ]);
  api.descriptor.mockImplementation(async (_c, appId) => ({
    success: true, appId, version: '1', tools: [], configSchema: appId === GREET ? SCHEMA : null,
  }));
  api.getConfig.mockResolvedValue(CONFIG);
  api.history.mockResolvedValue([{ version: 2, updatedBy: 1, createdAt: CONFIG.createdAt }, { version: 1, updatedBy: 1, createdAt: null }]);
});

describe('AdminBundleSettings', () => {
  it('renders nothing when the feature flag is off', () => {
    flag.mockReturnValue(false);
    const { container } = mount();
    expect(container).toBeEmptyDOMElement();
  });

  it('renders one card per activated bundle with status and summary', async () => {
    mount();
    expect(await screen.findByText('Greeter')).toBeInTheDocument();
    expect(screen.getByText('Ping')).toBeInTheDocument();
    expect(await screen.findByText('Welcome new members.')).toBeInTheDocument();
    expect(await screen.findByText('Configurable')).toBeInTheDocument();
    expect(screen.getByText('No settings')).toBeInTheDocument();
  });

  it('opens the schema form with version, last-changed and history', async () => {
    mount();
    fireEvent.click(await screen.findByTestId(`bundle-card-${GREET}`));
    expect(await screen.findByLabelText(/Welcome message/)).toHaveValue('hello');
    expect(screen.getByTestId('config-meta')).toHaveTextContent('Version 2');
    expect(await screen.findByTestId('config-history')).toHaveTextContent('v1');
  });

  it('saves with the loaded version as expectedVersion', async () => {
    api.saveConfig.mockResolvedValue({ ...CONFIG, version: 3, config: { message: 'yo', enabled: true } });
    mount(`/admin/7/settings/${GREET}`);
    const input = await screen.findByLabelText(/Welcome message/);
    fireEvent.change(input, { target: { value: 'yo' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.saveConfig).toHaveBeenCalledWith(7, GREET, { message: 'yo', enabled: true }, 2));
    expect(await screen.findByText('Settings saved.')).toBeInTheDocument();
  });

  it('reset restores the last saved values', async () => {
    mount(`/admin/7/settings/${GREET}`);
    const input = await screen.findByLabelText(/Welcome message/);
    fireEvent.change(input, { target: { value: 'changed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }));
    await waitFor(() => expect(screen.getByLabelText(/Welcome message/)).toHaveValue('hello'));
  });

  it('offers a reload on a 409 version conflict', async () => {
    api.saveConfig.mockRejectedValue(new BundleAdminError(409, 'config_version_conflict', 'stale'));
    mount(`/admin/7/settings/${GREET}`);
    await screen.findByLabelText(/Welcome message/);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    const reload = await screen.findByRole('button', { name: 'Reload latest' });
    fireEvent.click(reload);
    await waitFor(() => expect(api.getConfig).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole('button', { name: 'Reload latest' })).not.toBeInTheDocument();
  });

  it('shows hub-api 422 field errors beside the field', async () => {
    api.saveConfig.mockRejectedValue(
      new BundleAdminError(422, 'config_invalid', 'bad', { errors: [{ path: '$.message', rule: 'maxLength' }] }),
    );
    mount(`/admin/7/settings/${GREET}`);
    await screen.findByLabelText(/Welcome message/);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/Rejected by server: maxLength/)).toBeInTheDocument();
  });

  it('renders "no settings" (not an error) for a schema-less bundle', async () => {
    mount(`/admin/7/settings/${PLAIN}`);
    expect(await screen.findByTestId('no-settings')).toBeInTheDocument();
    expect(api.getConfig).not.toHaveBeenCalled();
  });

  it('treats a 404 bundle_has_no_config from /config as no settings', async () => {
    api.getConfig.mockRejectedValue(new BundleAdminError(404, 'bundle_has_no_config', 'none'));
    mount(`/admin/7/settings/${GREET}`);
    expect(await screen.findByTestId('no-settings')).toBeInTheDocument();
  });

  it('shows a permission message on 403', async () => {
    api.descriptor.mockRejectedValue(new BundleAdminError(403, 'FORBIDDEN', 'no'));
    mount(`/admin/7/settings/${GREET}`);
    expect(await screen.findByText(/do not have permission/)).toBeInTheDocument();
  });

  it('gives moderators a read-only form with no save/reset', async () => {
    setRole('moderator');
    mount(`/admin/7/settings/${GREET}`);
    expect(await screen.findByLabelText(/Welcome message/)).toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    expect(screen.getByText(/Read-only/)).toBeInTheDocument();
  });

  it('blocks members who are not moderators or admins', () => {
    setRole('member');
    mount();
    expect(screen.getByRole('alert')).toHaveTextContent(/moderator or admin/);
    expect(activation.list).not.toHaveBeenCalled();
  });
  it('close returns to the grid and generic save errors surface', async () => {
    api.saveConfig.mockRejectedValue(new BundleAdminError(500, 'ERROR_500', 'Server exploded'));
    mount(`/admin/7/settings/${GREET}`);
    await screen.findByLabelText(/Welcome message/);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Server exploded')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByTestId('bundle-settings-panel')).not.toBeInTheDocument());
  });

  it('explains an inactive bundle and a network failure', async () => {
    api.descriptor.mockRejectedValue(new BundleAdminError(404, 'bundle_not_active', 'x'));
    activation.list.mockRejectedValue(new Error('offline'));
    mount(`/admin/7/settings/${GREET}`);
    expect(await screen.findByText(/no longer active/)).toBeInTheDocument();
    expect(await screen.findByText(/Something went wrong/)).toBeInTheDocument();
  });

  it('shows an empty state when nothing is activated', async () => {
    activation.list.mockResolvedValue([]);
    mount();
    expect(await screen.findByText(/No apps are activated yet/)).toBeInTheDocument();
  });
});

describe('helpers', () => {
  it('bundleName humanises the last app-id segment', () => {
    expect(bundleName('waddles.core.x.role_sync-map')).toBe('Role Sync Map');
  });

  it('toErrorSchema nests JSON paths and keeps root errors', () => {
    expect(toErrorSchema([{ path: '$.a.b', rule: 'type' }, { path: '$', rule: 'required' }, { path: '$.l[0]', rule: 'enum' }])).toEqual({
      __errors: ['Rejected by server: required'],
      a: { b: { __errors: ['Rejected by server: type'] } },
      l: { 0: { __errors: ['Rejected by server: enum'] } },
    });
  });
});
