/**
 * Tests for the global-admin bundle-version approval queue: list load
 * (`GET /api/v1/admin/bundle-versions`), status-filter refetch, the
 * review-panel permission-summary fetch, and the approve/deny actions.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

import SuperAdminBundleApprovals from '../SuperAdminBundleApprovals';
import { bundleAdminApi, bundleApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  bundleAdminApi: { listPendingVersions: vi.fn() },
  bundleApi: {
    getPermissions: vi.fn(),
    approveVersion: vi.fn(),
    denyVersion: vi.fn(),
  },
}));

const ONE_ROW = {
  versionId: 7,
  appId: 'waddles.integrations.vendor-42.mybundle',
  version: '1.0.0',
  status: 'PUBLISHED',
  requestedBy: 42,
  createdAt: '2026-09-27T00:00:00Z',
  rejectReason: null,
};

function listResponse(versions = [ONE_ROW]) {
  return {
    data: {
      success: true,
      versions,
      pagination: { page: 1, limit: 25, total: versions.length, totalPages: 1 },
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('SuperAdminBundleApprovals', () => {
  it('loads and renders the pending queue', async () => {
    bundleAdminApi.listPendingVersions.mockResolvedValue(listResponse());
    render(<SuperAdminBundleApprovals />);

    expect(await screen.findAllByTestId('bundle-approvals-row')).toHaveLength(1);
    expect(screen.getByText('waddles.integrations.vendor-42.mybundle')).toBeInTheDocument();
    expect(bundleAdminApi.listPendingVersions).toHaveBeenCalledWith({
      status: 'pending',
      page: 1,
      limit: 25,
    });
  });

  it('shows the empty state when there are no matching versions', async () => {
    bundleAdminApi.listPendingVersions.mockResolvedValue(listResponse([]));
    render(<SuperAdminBundleApprovals />);

    expect(await screen.findByText(/No pending bundle versions/)).toBeInTheDocument();
  });

  it('re-fetches with the selected status filter', async () => {
    bundleAdminApi.listPendingVersions.mockResolvedValue(listResponse([]));
    render(<SuperAdminBundleApprovals />);
    await screen.findByTestId('bundle-approvals-status-filter');

    await act(async () => {
      fireEvent.change(screen.getByTestId('bundle-approvals-status-filter'), {
        target: { value: 'rejected' },
      });
    });

    await waitFor(() =>
      expect(bundleAdminApi.listPendingVersions).toHaveBeenLastCalledWith({
        status: 'rejected',
        page: 1,
        limit: 25,
      }),
    );
  });

  it('opens the review panel and fetches the permission summary', async () => {
    bundleAdminApi.listPendingVersions.mockResolvedValue(listResponse());
    bundleApi.getPermissions.mockResolvedValue({
      data: { success: true, summary: { egress: [] }, permissionHash: 'sha256:abc' },
    });
    render(<SuperAdminBundleApprovals />);
    await screen.findAllByTestId('bundle-approvals-row');

    await act(async () => {
      fireEvent.click(screen.getByTestId('bundle-approvals-review-7'));
    });

    expect(bundleApi.getPermissions).toHaveBeenCalledWith(
      'waddles.integrations.vendor-42.mybundle',
      '1.0.0',
    );
    expect(await screen.findByTestId('bundle-approvals-summary')).toHaveTextContent('egress');
  });

  it('approves with the fetched permission hash and an optional community id', async () => {
    bundleAdminApi.listPendingVersions.mockResolvedValue(listResponse());
    bundleApi.getPermissions.mockResolvedValue({
      data: { success: true, summary: { egress: [] }, permissionHash: 'sha256:abc' },
    });
    bundleApi.approveVersion.mockResolvedValue({ data: { success: true, permissionHash: 'sha256:abc' } });
    render(<SuperAdminBundleApprovals />);
    await screen.findAllByTestId('bundle-approvals-row');

    await act(async () => {
      fireEvent.click(screen.getByTestId('bundle-approvals-review-7'));
    });
    await screen.findByTestId('bundle-approvals-summary');

    fireEvent.change(screen.getByTestId('bundle-approvals-community-id'), {
      target: { value: '12' },
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('bundle-approvals-approve-button'));
    });

    expect(bundleApi.approveVersion).toHaveBeenCalledWith(
      'waddles.integrations.vendor-42.mybundle',
      '1.0.0',
      { communityId: 12, permissionHash: 'sha256:abc' },
    );
    await waitFor(() => expect(bundleAdminApi.listPendingVersions).toHaveBeenCalledTimes(2));
  });

  it('denies with the entered reason', async () => {
    bundleAdminApi.listPendingVersions.mockResolvedValue(listResponse());
    bundleApi.getPermissions.mockResolvedValue({
      data: { success: true, summary: { egress: [] }, permissionHash: 'sha256:abc' },
    });
    bundleApi.denyVersion.mockResolvedValue({ data: { success: true, message: 'denied' } });
    render(<SuperAdminBundleApprovals />);
    await screen.findAllByTestId('bundle-approvals-row');

    await act(async () => {
      fireEvent.click(screen.getByTestId('bundle-approvals-review-7'));
    });
    await screen.findByTestId('bundle-approvals-summary');

    await act(async () => {
      fireEvent.click(screen.getByTestId('bundle-approvals-deny-button'));
    });
    expect(screen.getByTestId('bundle-approvals-panel-error')).toHaveTextContent(
      'A denial reason is required',
    );
    expect(bundleApi.denyVersion).not.toHaveBeenCalled();

    fireEvent.change(screen.getByTestId('bundle-approvals-deny-reason'), {
      target: { value: 'manifest declares unreviewed egress host' },
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('bundle-approvals-deny-button'));
    });

    expect(bundleApi.denyVersion).toHaveBeenCalledWith(
      'waddles.integrations.vendor-42.mybundle',
      '1.0.0',
      { reason: 'manifest declares unreviewed egress host' },
    );
    await waitFor(() => expect(bundleAdminApi.listPendingVersions).toHaveBeenCalledTimes(2));
  });

  it('shows the load error state', async () => {
    bundleAdminApi.listPendingVersions.mockRejectedValue({
      response: { status: 403, data: { error: { message: 'Insufficient scope' } } },
    });
    render(<SuperAdminBundleApprovals />);

    expect(await screen.findByTestId('bundle-approvals-error')).toHaveTextContent(
      'Insufficient scope',
    );
  });
});
