/**
 * Tenant community tier allocation page: pool meters (limit/unlimited/not licensed/stale/overage),
 * per-community tier select, 402 upgrade panel, 503 and flag-off 404 states, release confirm.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { tenantApi } from '../../../services/api';
import { tenantCommunityTiersApi } from '../../../services/tenantCommunityTiersApi';
import TenantCommunityTiers from '../TenantCommunityTiers';

vi.mock('../../../services/api', () => ({ tenantApi: { getCommunities: vi.fn() } }));
vi.mock('../../../services/tenantCommunityTiersApi', () => ({
  tenantCommunityTiersApi: { getPool: vi.fn(), listAllocations: vi.fn(), setTier: vi.fn() },
}));
const api = vi.mocked(tenantCommunityTiersApi);

const U1 = '11111111-1111-4111-8111-111111111111';
const U2 = '22222222-2222-4222-8222-222222222222';
const COMMUNITIES = [
  { id: 1, publicId: U1, name: 'Penguin Club' },
  { id: 2, publicId: U2, name: 'Seal Society' },
];
const POOL = {
  tiers: [{ tier: 2, limit: 5, used: 3, available: 2, overage: 0 }],
  observedAt: '2026-11-17T10:00:00Z',
  stale: false,
  source: 'license_server',
};
const httpErr = (status, code, details) => ({ response: { status, data: { error: { code, message: 'x', details } } } });

function mount() {
  return render(
    <MemoryRouter initialEntries={['/tenant/acme/community-tiers']}>
      <Routes><Route path="/tenant/:tenantSlug/community-tiers" element={<TenantCommunityTiers />} /></Routes>
    </MemoryRouter>,
  );
}
const select = (name) => screen.getByLabelText(`Tier for ${name}`);

beforeEach(() => {
  vi.clearAllMocks();
  tenantApi.getCommunities.mockResolvedValue({ data: { communities: COMMUNITIES, total: 2 } });
  api.getPool.mockResolvedValue(POOL);
  api.listAllocations.mockResolvedValue({
    allocations: [{ communityId: U1, tier: 2, source: 1, allocatedAt: '2026-11-01T00:00:00Z', stale: false }],
    meta: { total: 1, limit: 200, offset: 0 },
  });
  api.setTier.mockResolvedValue({ communityId: U2, tier: 2, previousTier: 1, changed: true, used: 4, limit: 5, overage: 0 });
});

describe('TenantCommunityTiers', () => {
  it('renders ONE Premium pool meter and the communities with their current tier', async () => {
    mount();
    await screen.findByText('Penguin Club');
    expect(api.getPool).toHaveBeenCalledWith('acme');
    expect(api.listAllocations).toHaveBeenCalledWith('acme', expect.objectContaining({ tier: 2 }));
    const pool = within(screen.getByTestId('pool-tier-2'));
    expect(pool.getByText('Premium communities: 3 of 5 used')).toBeInTheDocument();
    expect(pool.getByText('Available: 2')).toBeInTheDocument();
    expect(pool.getByTestId('pool-source').textContent).toBe('Source: license server');
    expect(screen.queryByTestId('pool-tier-3')).not.toBeInTheDocument();
    expect(screen.queryByText(/Professional|Enterprise/)).not.toBeInTheDocument();
    expect(select('Penguin Club')).toHaveValue('2');
    expect(select('Seal Society')).toHaveValue('1');
    const options = within(select('Seal Society')).getAllByRole('option').map((o) => o.textContent);
    expect(options).toEqual(['Free', 'Premium']);
  });

  it('shows unlimited text for a -1 limit', async () => {
    api.getPool.mockResolvedValue({ ...POOL, tiers: [{ tier: 2, limit: -1, used: 1, available: null, overage: 0 }] });
    mount();
    expect(await screen.findByText('Premium communities: 1 in use, unlimited')).toBeInTheDocument();
    expect(screen.getByText('Available: unlimited')).toBeInTheDocument();
  });

  it('shows Unlimited (operator deployment) for the bypass_default source', async () => {
    api.getPool.mockResolvedValue({ ...POOL, tiers: [{ tier: 2, limit: -1, used: 0, available: null, overage: 0 }], source: 'bypass_default' });
    mount();
    expect(await screen.findByText(/Unlimited \(operator deployment\)/)).toBeInTheDocument();
    expect(screen.getByTestId('pool-source').textContent).toBe('Source: operator deployment');
  });

  it('shows not licensed for a null limit', async () => {
    api.getPool.mockResolvedValue({ ...POOL, tiers: [{ tier: 2, limit: null, used: 0, available: 0, overage: 0 }] });
    mount();
    expect(await screen.findByText(/not licensed/)).toBeInTheDocument();
  });

  it('shows a stale badge with the observed time', async () => {
    api.getPool.mockResolvedValue({ ...POOL, stale: true });
    mount();
    const badge = await screen.findByTestId('pool-stale');
    expect(badge.textContent).toMatch(/Stale: last observed/);
  });

  it('warns in text when the Premium pool is over its limit', async () => {
    api.getPool.mockResolvedValue({ ...POOL, tiers: [{ tier: 2, limit: 2, used: 4, available: 0, overage: 2 }] });
    mount();
    expect(await screen.findByText(/Over limit by 2/)).toBeInTheDocument();
    expect(screen.getByText(/Over limit by 2/)).toHaveClass('text-red-300');
  });

  it('allocates a tier by community UUID, announces the result and reloads usage', async () => {
    mount();
    await screen.findByText('Seal Society');
    const poolCalls = api.getPool.mock.calls.length;
    fireEvent.change(select('Seal Society'), { target: { value: '2' } });
    await waitFor(() => expect(api.setTier).toHaveBeenCalledWith('acme', U2, 2));
    expect(await screen.findByText('Seal Society is now Premium (was Free).')).toBeInTheDocument();
    expect(api.getPool.mock.calls.length).toBeGreaterThan(poolCalls);
    expect(screen.getByTestId('tiers-live')).toHaveAttribute('aria-live', 'polite');
  });

  it('disables the select while saving', async () => {
    let resolve;
    api.setTier.mockReturnValue(new Promise((r) => { resolve = r; }));
    mount();
    await screen.findByText('Seal Society');
    fireEvent.change(select('Seal Society'), { target: { value: '2' } });
    await waitFor(() => expect(select('Seal Society')).toBeDisabled());
    resolve({ communityId: U2, tier: 2, previousTier: 1, changed: true, used: 4, limit: 5, overage: 0 });
    await waitFor(() => expect(select('Seal Society')).toBeEnabled());
  });

  it('shows the upgrade panel on 402 without an error alert toast', async () => {
    api.setTier.mockRejectedValue(httpErr(402, 'TEAM_POOL_EXHAUSTED', { requiredTier: 2, limit: 5, used: 5, upgrade: true }));
    mount();
    await screen.findByText('Seal Society');
    fireEvent.change(select('Seal Society'), { target: { value: '2' } });
    const panel = await screen.findByTestId('pool-exhausted');
    expect(panel.textContent).toContain('Your Premium pool is full (5 of 5).');
    expect(panel.textContent).toContain('Release a slot or upgrade your license.');
    expect(screen.queryByText(/Failed to change tier/)).not.toBeInTheDocument();
  });

  it('is honest on 503: team licensing is not available yet, selects disabled', async () => {
    api.getPool.mockRejectedValue(httpErr(503, 'TEAM_POOL_UNAVAILABLE'));
    mount();
    const box = await screen.findByTestId('pool-unavailable');
    expect(box.textContent).toBe("Team licensing isn't available from the license server yet; contact your platform administrator.");
    expect(select('Seal Society')).toBeDisabled();
    expect(screen.queryByText(/Premium community pool/)).not.toBeInTheDocument();
  });

  it('shows the not-enabled state when the flag is off (404)', async () => {
    api.getPool.mockRejectedValue(httpErr(404, 'NOT_FOUND'));
    api.listAllocations.mockRejectedValue(httpErr(404, 'NOT_FOUND'));
    mount();
    expect(await screen.findByTestId('tiers-not-enabled')).toBeInTheDocument();
  });

  it('shows a 403 state', async () => {
    api.getPool.mockRejectedValue(httpErr(403, 'FORBIDDEN'));
    mount();
    expect(await screen.findByTestId('tiers-forbidden')).toBeInTheDocument();
  });

  it('shows a generic error with the server message', async () => {
    tenantApi.getCommunities.mockRejectedValue({ response: { status: 500, data: { error: 'boom' } } });
    mount();
    expect((await screen.findByTestId('tiers-error')).textContent).toBe('boom');
  });

  it('handles 409 conflict generically by reloading and telling the admin', async () => {
    api.setTier.mockRejectedValue(httpErr(409, 'CONFLICT'));
    mount();
    await screen.findByText('Seal Society');
    fireEvent.change(select('Seal Society'), { target: { value: '2' } });
    expect(await screen.findByText(/changed by someone else/)).toBeInTheDocument();
  });

  it('shows an empty state with no communities', async () => {
    tenantApi.getCommunities.mockResolvedValue({ data: { communities: [], total: 0 } });
    mount();
    expect(await screen.findByText('No communities found in this tenant.')).toBeInTheDocument();
  });

  it('asks to confirm before releasing, and cancel makes no call', async () => {
    mount();
    await screen.findByText('Penguin Club');
    fireEvent.change(select('Penguin Club'), { target: { value: '1' } });
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toMatch(/this community loses Premium features/);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api.setTier).not.toHaveBeenCalled();
  });

  it('releases after confirming', async () => {
    api.setTier.mockResolvedValue({ communityId: U1, tier: 1, previousTier: 2, changed: true, used: 2, limit: 5, overage: 0 });
    mount();
    await screen.findByText('Penguin Club');
    fireEvent.change(select('Penguin Club'), { target: { value: '1' } });
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Release slot' }));
    await waitFor(() => expect(api.setTier).toHaveBeenCalledWith('acme', U1, 1));
  });

  it('never sends numeric community ids', async () => {
    mount();
    await screen.findByText('Seal Society');
    fireEvent.change(select('Seal Society'), { target: { value: '2' } });
    await waitFor(() => expect(api.setTier).toHaveBeenCalled());
    expect(typeof api.setTier.mock.calls[0][1]).toBe('string');
  });
});
