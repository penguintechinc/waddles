/**
 * The vendor dashboard route exists in hub-api; a 404 means "no vendor profile yet" and must
 * read that way instead of a bare "Failed to load dashboard".
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import VendorDashboard from '../VendorDashboard';
import { vendorApi } from '../../../services/api';

vi.mock('../../../contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: 1 } }) }));
vi.mock('../../../services/api', () => ({ vendorApi: { getDashboard: vi.fn() } }));

const mount = () => render(<MemoryRouter><VendorDashboard /></MemoryRouter>);

describe('VendorDashboard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('calls the vendor dashboard endpoint', async () => {
    vendorApi.getDashboard.mockResolvedValue({ data: { stats: {}, recentSubmissions: [] } });
    mount();
    await vi.waitFor(() => expect(vendorApi.getDashboard).toHaveBeenCalledTimes(1));
  });

  it('explains a 404 as a missing vendor profile', async () => {
    vendorApi.getDashboard.mockRejectedValue({ response: { status: 404, data: { error: { message: 'Vendor profile not found' } } } });
    mount();
    expect(await screen.findByText(/No vendor profile yet/)).toBeTruthy();
  });

  it('passes through other API errors', async () => {
    vendorApi.getDashboard.mockRejectedValue({ response: { status: 500, data: { error: { message: 'boom' } } } });
    mount();
    expect(await screen.findByText('boom')).toBeTruthy();
  });
});
