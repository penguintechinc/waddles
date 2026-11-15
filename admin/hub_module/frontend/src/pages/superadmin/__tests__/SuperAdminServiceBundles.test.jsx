/**
 * Tests for the super-admin service-bundles page: list, per-row Disable/Enable,
 * and the has_dependents (409) confirm -> forced disable flow.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import SuperAdminServiceBundles from '../SuperAdminServiceBundles';
import { bundleAdminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  bundleAdminApi: { listInstalled: vi.fn(), disableService: vi.fn(), enableService: vi.fn() },
}));

const SVC = 'waddles.integrations.tools.svc';
const row = (status) => ({
  appId: SVC,
  name: 'Tools',
  kind: 'service',
  version: '1.0.0',
  digestShort: 'sha256:abababababab',
  installedAt: '2026-11-01T00:00:00Z',
  installSource: 'human',
  status,
});
const list = (...bundles) => ({ data: { success: true, bundles } });

beforeEach(() => {
  vi.clearAllMocks();
});

describe('SuperAdminServiceBundles', () => {
  it('lists service bundles with version and status', async () => {
    bundleAdminApi.listInstalled.mockResolvedValue(list(row('active')));
    render(<SuperAdminServiceBundles />);
    const r = await screen.findByTestId('service-bundle-row');
    expect(r).toHaveTextContent('1.0.0');
    expect(screen.getByTestId(`service-status-${SVC}`)).toHaveTextContent('active');
    expect(bundleAdminApi.listInstalled).toHaveBeenCalledWith({ kind: 'service' });
  });

  it('disables without dependents and reloads', async () => {
    bundleAdminApi.listInstalled
      .mockResolvedValueOnce(list(row('active')))
      .mockResolvedValueOnce(list(row('disabled')));
    bundleAdminApi.disableService.mockResolvedValue({ data: { success: true } });
    render(<SuperAdminServiceBundles />);
    fireEvent.click(await screen.findByTestId(`service-disable-${SVC}`));
    await waitFor(() => expect(bundleAdminApi.disableService).toHaveBeenCalledWith(SVC, false));
    expect(await screen.findByTestId(`service-enable-${SVC}`)).toBeInTheDocument();
  });

  it('enables a disabled bundle', async () => {
    bundleAdminApi.listInstalled
      .mockResolvedValueOnce(list(row('disabled')))
      .mockResolvedValueOnce(list(row('active')));
    bundleAdminApi.enableService.mockResolvedValue({ data: { success: true } });
    render(<SuperAdminServiceBundles />);
    fireEvent.click(await screen.findByTestId(`service-enable-${SVC}`));
    await waitFor(() => expect(bundleAdminApi.enableService).toHaveBeenCalledWith(SVC));
    expect(await screen.findByTestId(`service-disable-${SVC}`)).toBeInTheDocument();
  });

  it('shows dependents on 409 and force-disables after confirm', async () => {
    bundleAdminApi.listInstalled.mockResolvedValue(list(row('active')));
    bundleAdminApi.disableService
      .mockRejectedValueOnce({
        response: {
          status: 409,
          data: {
            error: {
              code: 'has_dependents',
              message: 'requested',
              details: { dependents: [{ appId: 'waddles.socials.music.default', kind: 'app', version: '2.0.0' }] },
            },
          },
        },
      })
      .mockResolvedValueOnce({ data: { success: true } });
    render(<SuperAdminServiceBundles />);
    fireEvent.click(await screen.findByTestId(`service-disable-${SVC}`));
    expect(await screen.findByTestId('service-dependent')).toHaveTextContent(
      'waddles.socials.music.default',
    );
    fireEvent.click(screen.getByTestId('service-disable-force'));
    await waitFor(() => expect(bundleAdminApi.disableService).toHaveBeenLastCalledWith(SVC, true));
    await waitFor(() => expect(screen.queryByTestId('service-disable-confirm')).toBeNull());
  });

  it('shows a load error', async () => {
    bundleAdminApi.listInstalled.mockRejectedValue({ response: { status: 500, data: {} } });
    render(<SuperAdminServiceBundles />);
    expect(await screen.findByTestId('service-bundles-error')).toBeInTheDocument();
  });
});
