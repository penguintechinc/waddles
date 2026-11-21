/** Tenant reputation settings: the ban-lift re-arm drop loads, validates and saves. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../services/api', () => ({
  tenantApi: { getReputationConfig: vi.fn(), updateReputationConfig: vi.fn() },
}));
import { tenantApi } from '../../../services/api';
import TenantReputationSettings from '../TenantReputationSettings';

function mount() {
  return render(
    <MemoryRouter initialEntries={['/tenant/acme/reputation']}>
      <Routes>
        <Route path="/tenant/:tenantSlug/reputation" element={<TenantReputationSettings />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  tenantApi.getReputationConfig.mockResolvedValue({
    data: {
      success: true,
      tenantAutoBanEnabled: true,
      tenantAutoBanFloor: 350,
      tenantAtRiskThreshold: 450,
      liftRearmDrop: 5,
    },
  });
  tenantApi.updateReputationConfig.mockResolvedValue({ data: { success: true } });
});

describe('TenantReputationSettings re-arm drop', () => {
  it('shows the stored re-arm drop and saves an edited value', async () => {
    mount();
    const input = await screen.findByLabelText(/re-arm drop after a ban lift/i);
    expect(input.value).toBe('5');
    fireEvent.change(input, { target: { value: '12' } });
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() =>
      expect(tenantApi.updateReputationConfig).toHaveBeenCalledWith('acme', {
        tenantAutoBanEnabled: true,
        tenantAutoBanFloor: 350,
        tenantAtRiskThreshold: 450,
        liftRearmDrop: 12,
      }),
    );
  });

  it.each(['0', '101', '', '2.5'])('rejects %s without calling the API', async (value) => {
    mount();
    const input = await screen.findByLabelText(/re-arm drop after a ban lift/i);
    fireEvent.change(input, { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    expect(await screen.findByText(/re-arm drop must be a whole number between 1 and 100/i)).toBeTruthy();
    expect(tenantApi.updateReputationConfig).not.toHaveBeenCalled();
  });

  it('falls back to the default 5 when the server omits it', async () => {
    tenantApi.getReputationConfig.mockResolvedValue({
      data: { success: true, tenantAutoBanEnabled: false, tenantAutoBanFloor: 350, tenantAtRiskThreshold: 450 },
    });
    mount();
    const input = await screen.findByLabelText(/re-arm drop after a ban lift/i);
    expect(input.value).toBe('5');
  });
});
