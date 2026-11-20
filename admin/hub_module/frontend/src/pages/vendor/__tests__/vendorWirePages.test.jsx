/**
 * regression: `.map is not a function` on /vendor/analytics and /vendor/discount-codes. Bodies here use
 * the shapes hub-api really sends: `{ success, data: { rows, total } }` for communities, `{ codes }` for
 * discount performance, `{ discountCodes, total }` for the code list, and `{ success:false, error }` errors.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const api = vi.hoisted(() => ({
  getAnalyticsSales: vi.fn(),
  getAnalyticsInstalls: vi.fn(),
  getAnalyticsDiscountCodes: vi.fn(),
  getAnalyticsCommunities: vi.fn(),
  exportAnalytics: vi.fn(),
  getDiscountCodes: vi.fn(),
  getModules: vi.fn(),
}));
vi.mock('../../../services/api', () => ({ vendorApi: api }));

import VendorAnalytics from '../VendorAnalytics';
import VendorDiscountCodes from '../VendorDiscountCodes';
import {
  asArray,
  normalizeCommunities,
  normalizeDiscountCodePage,
  normalizeDiscountPerformance,
  normalizeInstalls,
  normalizeModules,
  normalizeSales,
  unwrapEnvelope,
} from '../vendorWire';

const env = (data) => ({ data: { success: true, data } });
const httpError = (status, body) => ({ response: { status, data: body } });

const sales = { period: '30d', installations: { total: 12, active: 9, uninstalls: 3, churnRate: 25 }, revenue: { totalCents: 90000, mtdCents: 12345, ytdCents: 90000 } };
const installs = [{ date: '2026-11-01T00:00:00+00:00', installs: 2, uninstalls: 1 }];
const perf = { codes: [{ id: 1, code: 'SAVE10', isActive: true, validUntil: null, maxUses: 10, currentUses: 4, totalRedemptions: 4, totalDiscountCents: 800 }], summary: { active: 1, expired: 0 } };
const comms = { rows: [{ installationId: 5, communityId: 'uuid-1', moduleId: 2, moduleName: 'Poll', status: 'active', installedAt: '2026-11-01T00:00:00+00:00', uninstalledAt: null, lastActiveAt: null }], total: 1, page: 1, limit: 20 };

const mountAnalytics = () => render(<MemoryRouter><VendorAnalytics /></MemoryRouter>);

beforeEach(() => {
  vi.clearAllMocks();
  api.getAnalyticsSales.mockResolvedValue(env(sales));
  api.getAnalyticsInstalls.mockResolvedValue(env(installs));
  api.getAnalyticsDiscountCodes.mockResolvedValue(env(perf));
  api.getAnalyticsCommunities.mockResolvedValue(env(comms));
  api.getModules.mockResolvedValue({ data: { success: true, modules: [{ id: 2, name: 'Poll' }] } });
});

describe('VendorAnalytics against the real API shapes', () => {
  it('renders the community rows wrapper, discount rows and sales cards without crashing', async () => {
    mountAnalytics();
    expect(await screen.findByText('uuid-1')).toBeTruthy();
    expect(screen.getByText('Poll')).toBeTruthy();
    expect(screen.getByText('SAVE10')).toBeTruthy();
    expect(screen.getByText('$123.45')).toBeTruthy();
    expect(screen.getByText('12')).toBeTruthy();
  });

  it('only sends an allow-listed sortBy and the supported granularity', async () => {
    mountAnalytics();
    await screen.findByText('uuid-1');
    expect(api.getAnalyticsCommunities.mock.calls[0][0]).toEqual({ page: 1, limit: 20, sortBy: 'installed_at' });
    expect(api.getAnalyticsInstalls.mock.calls[0][0].granularity).toBe('day');
  });

  it('shows empty states for empty rows and codes', async () => {
    api.getAnalyticsCommunities.mockResolvedValue(env({ rows: [], total: 0, page: 1, limit: 20 }));
    api.getAnalyticsDiscountCodes.mockResolvedValue(env({ codes: [], summary: { active: 0, expired: 0 } }));
    mountAnalytics();
    expect(await screen.findByText('No community installations yet')).toBeTruthy();
    expect(screen.getByText(/No discount codes yet/)).toBeTruthy();
  });

  it('treats an error body as an error state, not data', async () => {
    api.getAnalyticsCommunities.mockResolvedValue({ data: { success: false, error: 'Vendor profile not found' } });
    api.getAnalyticsDiscountCodes.mockRejectedValue(httpError(500, { success: false, error: 'boom' }));
    mountAnalytics();
    expect(await screen.findByText('We could not load Community installations')).toBeTruthy();
    expect(await screen.findByText('We could not load Discount code performance')).toBeTruthy();
  });

  it('retries a failed panel', async () => {
    api.getAnalyticsDiscountCodes.mockRejectedValueOnce(httpError(500, { success: false, error: 'boom' }));
    mountAnalytics();
    await screen.findByText('We could not load Discount code performance');
    fireEvent.click(screen.getAllByText('Try again')[0]);
    expect(await screen.findByText('SAVE10')).toBeTruthy();
  });
});

describe('VendorDiscountCodes against the real API shapes', () => {
  const mountCodes = () => render(<MemoryRouter><VendorDiscountCodes /></MemoryRouter>);
  const page = { discountCodes: [{ id: 3, code: 'WELCOME', moduleId: 2, discountType: 'percentage', discountValue: 15, maxUses: 5, currentUses: 1, validFrom: null, validUntil: null, isActive: true, description: null, createdAt: '2026-11-01T00:00:00Z' }], page: 1, limit: 20, total: 1 };

  it('lists codes from { data: { discountCodes } }', async () => {
    api.getDiscountCodes.mockResolvedValue(env(page));
    mountCodes();
    expect(await screen.findByText('WELCOME')).toBeTruthy();
    expect(screen.getByText('Poll')).toBeTruthy();
  });

  it('shows the empty state for an empty page', async () => {
    api.getDiscountCodes.mockResolvedValue(env({ discountCodes: [], page: 1, limit: 20, total: 0 }));
    mountCodes();
    expect(await screen.findByText('No discount codes yet')).toBeTruthy();
  });

  it('shows an error panel (no crash, no empty state) for a non-envelope body', async () => {
    api.getDiscountCodes.mockResolvedValue({ data: '<html>spa fallback</html>' });
    mountCodes();
    expect(await screen.findByText('We could not load Discount codes')).toBeTruthy();
    expect(screen.queryByText('No discount codes yet')).toBeNull();
  });

  it('survives a modules body that is not a list', async () => {
    api.getDiscountCodes.mockResolvedValue(env(page));
    api.getModules.mockResolvedValue({ data: { modules: { 0: 'x' } } });
    mountCodes();
    await waitFor(() => expect(screen.getByText('WELCOME')).toBeTruthy());
  });
});

describe('vendorWire helpers', () => {
  it('coerces non-arrays and bad payloads safely', () => {
    expect(asArray({})).toEqual([]);
    expect(normalizeSales(null)).toBeNull();
    expect(normalizeSales({})).toEqual({ totalInstallations: 0, activeInstallations: 0, churnRate: 0, revenueMtd: 0 });
    expect(normalizeInstalls({})).toEqual([]);
    expect(normalizeInstalls([{}])[0]).toEqual({ installs: 0, uninstalls: 0, label: '' });
    expect(normalizeCommunities([])).toEqual({ rows: [], total: 0 });
    expect(normalizeCommunities({ rows: 'x', total: 'y' })).toEqual({ rows: [], total: 0 });
    expect(normalizeDiscountCodePage(undefined)).toEqual({ codes: [], total: 0 });
    expect(normalizeModules(null)).toEqual([]);
    expect(normalizeDiscountPerformance(null)).toEqual([]);
  });

  it('derives discount status and remaining uses', () => {
    const [a, b, c, d] = normalizeDiscountPerformance({
      codes: [
        { id: 1, code: 'A', isActive: false },
        { id: 2, code: 'B', isActive: true, validUntil: '2000-01-01T00:00:00Z' },
        { id: 3, code: 'C', isActive: true, maxUses: 2, currentUses: 2 },
        { id: 4, code: 'D', isActive: true },
      ],
    });
    expect([a.status, b.status, c.status, d.status]).toEqual(['inactive', 'expired', 'maxed', 'active']);
    expect([c.remainingUses, d.remainingUses]).toEqual([0, -1]);
  });

  it('unwrapEnvelope throws on non-envelopes and returns data otherwise', () => {
    expect(unwrapEnvelope(env([1]))).toEqual([1]);
    expect(() => unwrapEnvelope({ data: { success: false, error: 'nope' } })).toThrow('nope');
    expect(() => unwrapEnvelope({ data: 'html' })).toThrow('Unexpected response');
    expect(() => unwrapEnvelope(undefined)).toThrow();
  });
});
