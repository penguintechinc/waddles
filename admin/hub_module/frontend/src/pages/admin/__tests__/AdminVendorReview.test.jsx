/** regression: the bare route requested `vendor-submissions/undefined`; no id means no request. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import AdminVendorReview from '../AdminVendorReview';
import { renderWithProviders } from '../../../test/renderWithProviders';

const api = vi.hoisted(() => ({ getVendorSubmission: vi.fn() }));
vi.mock('../../../services/api', () => ({ marketplaceAdminApi: api }));

beforeEach(() => vi.clearAllMocks());

describe('AdminVendorReview route param', () => {
  it('does not fetch and shows a pointer back when no submission id is in the route', async () => {
    renderWithProviders(<AdminVendorReview />, {
      route: '/superadmin/vendor-submissions',
      path: '/superadmin/vendor-submissions',
    });
    expect(await screen.findByTestId('no-submission-selected')).toBeTruthy();
    expect(api.getVendorSubmission).not.toHaveBeenCalled();
  });

  it('fetches the submission once the id exists', async () => {
    api.getVendorSubmission.mockResolvedValue({
      data: { submission: { id: 7, status: 'pending', payment_method: 'stripe', scopes: [], reviews: [] } },
    });
    renderWithProviders(<AdminVendorReview />, {
      route: '/superadmin/vendor-submissions/7',
      path: '/superadmin/vendor-submissions/:submissionId',
    });
    await waitFor(() => expect(api.getVendorSubmission).toHaveBeenCalledWith('7'));
    expect(api.getVendorSubmission).toHaveBeenCalledTimes(1);
  });
});
