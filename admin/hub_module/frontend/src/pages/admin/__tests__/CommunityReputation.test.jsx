/** Tests for tenant/community reputation settings pages and soft-ban list. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import CommunityReputationPolicy from '../CommunityReputationPolicy';
import CommunityReputationBans from '../CommunityReputationBans';
import TenantReputationSettings from '../../tenant/TenantReputationSettings';
import { adminApi, tenantApi } from '../../../services/api';
import {
  validateCommunityPolicy,
  validateTenantPolicy,
  serverErrorMessage,
} from '../../../lib/reputationPolicy';

vi.mock('../../../services/api', () => ({
  adminApi: {
    getReputationConfig: vi.fn(),
    updateReputationConfig: vi.fn(),
    getReputationSoftBans: vi.fn(),
    getAtRiskUsers: vi.fn(),
    setReputationSoftBan: vi.fn(),
    clearReputationSoftBan: vi.fn(),
  },
  tenantApi: { getReputationConfig: vi.fn(), updateReputationConfig: vi.fn() },
}));

const httpError = (status, message) => ({ response: { status, data: { error: { message } } } });

function mount(ui, path, route) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={route} element={ui} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe('validation rules', () => {
  it('community: hard < soft < at-risk within 300-850', () => {
    expect(validateCommunityPolicy({ hard: 400, soft: 550, atRisk: 600 })).toEqual({});
    expect(validateCommunityPolicy({ hard: 550, soft: 550, atRisk: 600 }).hard).toMatch(/lower than the soft/);
    expect(validateCommunityPolicy({ hard: 400, soft: 600, atRisk: 600 }).soft).toMatch(/lower than the at-risk/);
    expect(validateCommunityPolicy({ hard: 299, soft: 550, atRisk: 851 })).toMatchObject({
      hard: expect.stringMatching(/between 300 and 850/),
      atRisk: expect.stringMatching(/between 300 and 850/),
    });
    expect(validateCommunityPolicy({ hard: '', soft: 550, atRisk: 600 }).hard).toMatch(/whole number/);
  });
  it('tenant: floor < at-risk within range', () => {
    expect(validateTenantPolicy({ floor: 350, atRisk: 450 })).toEqual({});
    expect(validateTenantPolicy({ floor: 450, atRisk: 450 }).floor).toMatch(/lower than/);
    expect(validateTenantPolicy({ floor: 100, atRisk: 450 }).floor).toMatch(/between 300/);
  });
  it('serverErrorMessage reads envelope shapes verbatim', () => {
    expect(serverErrorMessage(httpError(400, 'bad floor'))).toBe('bad floor');
    expect(serverErrorMessage({ response: { data: { error: 'X', message: 'm' } } })).toBe('m');
    expect(serverErrorMessage({}, 'fb')).toBe('fb');
  });
});

describe('TenantReputationSettings', () => {
  const route = '/tenant/:tenantSlug/reputation';
  const load = () =>
    mount(<TenantReputationSettings />, '/tenant/acme/reputation', route);

  it('toggle defaults off and saves with server values', async () => {
    tenantApi.getReputationConfig.mockResolvedValue({
      data: { tenantAutoBanEnabled: false, tenantAutoBanFloor: 350, tenantAtRiskThreshold: 450 },
    });
    tenantApi.updateReputationConfig.mockResolvedValue({ data: { success: true } });
    load();
    const sw = await screen.findByRole('switch', { name: /tenant auto-ban/i });
    expect(sw).not.toBeChecked();
    expect(screen.getByLabelText(/auto-ban floor/i)).toHaveValue(350);
    fireEvent.click(sw);
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() =>
      expect(tenantApi.updateReputationConfig).toHaveBeenCalledWith('acme', {
        tenantAutoBanEnabled: true,
        tenantAutoBanFloor: 350,
        tenantAtRiskThreshold: 450,
      }),
    );
    expect(await screen.findByText(/settings saved/i)).toBeInTheDocument();
  });

  it('blocks invalid save client-side and shows server error verbatim', async () => {
    tenantApi.getReputationConfig.mockResolvedValue({
      data: { tenantAutoBanEnabled: false, tenantAutoBanFloor: 350, tenantAtRiskThreshold: 450 },
    });
    load();
    const floor = await screen.findByLabelText(/auto-ban floor/i);
    fireEvent.change(floor, { target: { value: '500' } });
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    expect((await screen.findAllByText(/lower than the at-risk threshold/)).length).toBeGreaterThan(0);
    expect(tenantApi.updateReputationConfig).not.toHaveBeenCalled();

    fireEvent.change(floor, { target: { value: '360' } });
    tenantApi.updateReputationConfig.mockRejectedValue(httpError(400, 'Server says no'));
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    expect(await screen.findByTestId('rep-save-error')).toHaveTextContent('Server says no');
  });

  it('shows permission message on 403', async () => {
    tenantApi.getReputationConfig.mockRejectedValue(httpError(403, 'Forbidden scope'));
    load();
    expect(await screen.findByTestId('rep-load-error')).toHaveTextContent(/do not have permission/i);
  });
});

describe('CommunityReputationPolicy', () => {
  const load = () => mount(<CommunityReputationPolicy />, '/admin/7/reputation/policy', '/admin/:communityId/reputation/policy');

  it('both bans default off, saves payload', async () => {
    adminApi.getReputationConfig.mockResolvedValue({
      data: { softBanEnabled: false, softBanFloor: 550, hardBanEnabled: false, hardBanFloor: 400, atRiskThreshold: 600 },
    });
    adminApi.updateReputationConfig.mockResolvedValue({ data: { success: true } });
    load();
    const soft = await screen.findByRole('switch', { name: /soft ban/i });
    expect(soft).not.toBeChecked();
    expect(screen.getByRole('switch', { name: /hard ban/i })).not.toBeChecked();
    fireEvent.click(soft);
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() =>
      expect(adminApi.updateReputationConfig).toHaveBeenCalledWith('7', {
        softBanEnabled: true,
        softBanFloor: 550,
        hardBanEnabled: false,
        hardBanFloor: 400,
        atRiskThreshold: 600,
      }),
    );
  });

  it('rejects hard >= soft and surfaces server error', async () => {
    adminApi.getReputationConfig.mockResolvedValue({
      data: { softBanEnabled: false, softBanFloor: 550, hardBanEnabled: false, hardBanFloor: 400, atRiskThreshold: 600 },
    });
    load();
    fireEvent.change(await screen.findByLabelText(/hard-ban floor/i), { target: { value: '560' } });
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    expect((await screen.findAllByText(/lower than the soft-ban floor/)).length).toBeGreaterThan(0);
    expect(adminApi.updateReputationConfig).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/hard-ban floor/i), { target: { value: '450' } });
    adminApi.updateReputationConfig.mockRejectedValue(httpError(403, 'Not a community admin'));
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    expect(await screen.findByTestId('rep-save-error')).toHaveTextContent(/do not have permission.*Not a community admin/i);
  });

  it('shows permission message on 403 load', async () => {
    adminApi.getReputationConfig.mockRejectedValue(httpError(403, 'nope'));
    load();
    expect(await screen.findByTestId('rep-load-error')).toHaveTextContent(/do not have permission/i);
  });
});

describe('CommunityReputationBans', () => {
  const load = () => mount(<CommunityReputationBans />, '/admin/7/reputation/bans', '/admin/:communityId/reputation/bans');
  const ban = { userId: '11', displayName: 'alice', reason: 'floor', setByType: 'system', setAt: '2026-11-01T00:00:00Z', expiresAt: null };
  const risk = { userId: '12', displayName: 'bob', score: 480, tier: 'poor', belowSoftBanFloor: true, belowHardBanFloor: false };

  it('lists bans, clears one and reloads', async () => {
    adminApi.getReputationSoftBans.mockResolvedValueOnce({ data: { softBans: [ban] } }).mockResolvedValue({ data: { softBans: [] } });
    adminApi.getAtRiskUsers.mockResolvedValue({ data: { members: [risk] } });
    adminApi.clearReputationSoftBan.mockResolvedValue({ data: { success: true } });
    load();
    expect(await screen.findByText('alice')).toBeInTheDocument();
    expect(screen.getByText('Score floor')).toBeInTheDocument();
    expect(screen.getByText('Until cleared')).toBeInTheDocument();
    expect(screen.getByText('bob')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /clear soft ban/i }));
    await waitFor(() => expect(adminApi.clearReputationSoftBan).toHaveBeenCalledWith('7', '11'));
    expect(await screen.findByText('No soft-banned members')).toBeInTheDocument();
    expect(screen.getByText('Soft ban cleared.')).toBeInTheDocument();
  });

  it('manual soft-ban validates id and calls API; at-risk soft-ban works', async () => {
    adminApi.getReputationSoftBans.mockResolvedValue({ data: { softBans: [] } });
    adminApi.getAtRiskUsers.mockResolvedValue({ data: { members: [risk] } });
    adminApi.setReputationSoftBan.mockResolvedValue({ data: { success: true } });
    load();
    fireEvent.change(await screen.findByLabelText(/member id/i), { target: { value: 'abc' } });
    fireEvent.click(screen.getByRole('button', { name: /soft-ban member/i }));
    expect(await screen.findByTestId('rep-action-error')).toHaveTextContent(/numeric/);
    expect(adminApi.setReputationSoftBan).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/member id/i), { target: { value: '99' } });
    fireEvent.click(screen.getByRole('button', { name: /soft-ban member/i }));
    await waitFor(() => expect(adminApi.setReputationSoftBan).toHaveBeenCalledWith('7', '99', undefined));

    fireEvent.click(await screen.findByRole('button', { name: /^soft-ban$/i }));
    await waitFor(() => expect(adminApi.setReputationSoftBan).toHaveBeenCalledWith('7', '12', undefined));
  });

  it('shows server error on failed clear and 403 state on load', async () => {
    adminApi.getReputationSoftBans.mockResolvedValue({ data: { softBans: [ban] } });
    adminApi.getAtRiskUsers.mockResolvedValue({ data: { members: [] } });
    adminApi.clearReputationSoftBan.mockRejectedValue(httpError(404, 'Member is not soft-banned'));
    load();
    fireEvent.click(await screen.findByRole('button', { name: /clear soft ban/i }));
    expect(await screen.findByTestId('rep-action-error')).toHaveTextContent('Member is not soft-banned');
  });

  it('403 on load shows permission message', async () => {
    adminApi.getReputationSoftBans.mockRejectedValue(httpError(403, 'denied'));
    adminApi.getAtRiskUsers.mockResolvedValue({ data: { members: [] } });
    load();
    expect(await screen.findByTestId('rep-load-error')).toHaveTextContent(/do not have permission/i);
  });
});
