/** Tenant privacy page, Manual lookup + Bulk tabs: each route, errors, 403, empty state, erase confirm, 402 upgrade. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../services/tenantPrivacyApi', async (orig) => {
  const actual = await orig();
  return {
    ...actual,
    tenantPrivacyApi: { exportUser: vi.fn(), eraseUser: vi.fn(), doNotSell: vi.fn(), bulk: vi.fn() },
  };
});
vi.mock('../../../services/tenantDsarInboxApi', () => ({
  tenantDsarInboxApi: { list: vi.fn(() => Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 })) },
}));
import { tenantPrivacyApi } from '../../../services/tenantPrivacyApi';
import TenantPrivacy from '../TenantPrivacy';

/** Deterministic well-formed user UUID for `n`. */
const U = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const ok = (userId, extra = {}) => ({ userId, status: 'completed', detail: null, data: null, incomplete: null, ...extra });

function mount(tab = 'Manual lookup') {
  render(
    <MemoryRouter initialEntries={['/tenant/acme/privacy']}>
      <Routes>
        <Route path="/tenant/:tenantSlug/privacy" element={<TenantPrivacy />} />
      </Routes>
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole('tab', { name: tab }));
}

function run(type, ids) {
  fireEvent.change(screen.getByLabelText('Request type'), { target: { value: type } });
  fireEvent.change(screen.getByLabelText(/user ids? \(uuids?\)$/i), { target: { value: ids } });
  fireEvent.click(screen.getByRole('button', { name: /run .* request/i }));
}

beforeEach(() => {
  vi.clearAllMocks();
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:x');
  globalThis.URL.revokeObjectURL = vi.fn();
});

describe('TenantPrivacy manual lookup', () => {
  it('shows the empty state before any request', () => {
    mount();
    expect(screen.getByTestId('privacy-empty')).toBeTruthy();
  });

  it('export happy path lists category counts and downloads', async () => {
    tenantPrivacyApi.exportUser.mockResolvedValue(ok(U(7), { data: { messages: [{}, {}] }, incomplete: [] }));
    mount();
    run('export', U(7));
    await screen.findByTestId(`privacy-result-${U(7)}`);
    expect(tenantPrivacyApi.exportUser).toHaveBeenCalledWith('acme', U(7));
    expect(screen.getByText('Completed')).toBeTruthy();
    expect(screen.getByLabelText(/user id \(uuid\)/i)).toBeTruthy();
    expect(screen.getByText('messages: 2')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`download export for user ${U(7)}`, 'i') }));
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it('do-not-sell happy path', async () => {
    tenantPrivacyApi.doNotSell.mockResolvedValue(ok(U(3), { status: 'already_done' }));
    mount();
    run('do_not_sell', U(3));
    await screen.findByText('Already done');
    expect(tenantPrivacyApi.doNotSell).toHaveBeenCalledWith('acme', U(3));
  });

  it('erase requires confirmation; cancel makes no call', async () => {
    mount();
    run('erase', U(5));
    expect(await screen.findByTestId('privacy-erase-confirm')).toBeTruthy();
    expect(tenantPrivacyApi.eraseUser).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(screen.queryByTestId('privacy-erase-confirm')).toBeNull();
    expect(tenantPrivacyApi.eraseUser).not.toHaveBeenCalled();
  });

  it('erase confirmed calls API with confirm and shows conflict status', async () => {
    tenantPrivacyApi.eraseUser.mockResolvedValue(ok(U(5), { status: 'conflict', detail: 'shared account' }));
    mount();
    run('erase', U(5));
    fireEvent.click(await screen.findByRole('button', { name: /erase permanently/i }));
    await screen.findByText('Conflict');
    expect(tenantPrivacyApi.eraseUser).toHaveBeenCalledWith('acme', U(5), true);
    expect(screen.getByText('shared account')).toBeTruthy();
  });

  it('validates input and refuses several ids without calling the API', async () => {
    mount();
    run('export', '');
    expect(await screen.findByText(/at least one user id/i)).toBeTruthy();
    run('export', '7');
    expect(await screen.findByText(/must be UUIDs/i)).toBeTruthy();
    run('export', 'abc');
    expect(await screen.findByText(/must be UUIDs/i)).toBeTruthy();
    run('export', `${U(1)} ${U(2)}`);
    expect(await screen.findByText(/one user at a time/i)).toBeTruthy();
    expect(tenantPrivacyApi.exportUser).not.toHaveBeenCalled();
    expect(tenantPrivacyApi.bulk).not.toHaveBeenCalled();
  });

  it('shows a permission message on 403', async () => {
    tenantPrivacyApi.exportUser.mockRejectedValue({ isAxiosError: true, response: { status: 403, data: { error: { message: 'nope' } } } });
    mount();
    run('export', U(7));
    expect(await screen.findByText(/do not have permission to run privacy requests/i)).toBeTruthy();
  });

  it('shows the API error message on other failures', async () => {
    tenantPrivacyApi.doNotSell.mockRejectedValue({ isAxiosError: true, response: { status: 500, data: { error: { message: 'boom' } } } });
    mount();
    run('do_not_sell', U(7));
    expect(await screen.findByText('boom')).toBeTruthy();
    expect(screen.getByTestId('privacy-empty')).toBeTruthy();
  });
});

describe('TenantPrivacy bulk', () => {
  it('multiple ids use the bulk route with per-user results', async () => {
    tenantPrivacyApi.bulk.mockResolvedValue({
      action: 'do_not_sell', requested: 2, succeeded: 1, failed: 1,
      results: [ok(U(1)), ok(U(2), { status: 'not_found' })],
    });
    mount('Bulk');
    run('do_not_sell', `${U(1)} ${U(2).toUpperCase()}  ${U(1)}`);
    await screen.findByTestId(`privacy-result-${U(2)}`);
    expect(tenantPrivacyApi.bulk).toHaveBeenCalledWith('acme', 'do_not_sell', [U(1), U(2)], false);
    expect(screen.getByText('Not found')).toBeTruthy();
  });

  it('bulk erase confirms then sends confirm=true', async () => {
    tenantPrivacyApi.bulk.mockResolvedValue({ action: 'erase', requested: 2, succeeded: 2, failed: 0, results: [ok(U(1)), ok(U(2))] });
    mount('Bulk');
    run('erase', `${U(1)},${U(2)}`);
    fireEvent.click(await screen.findByRole('button', { name: /erase permanently/i }));
    await waitFor(() => expect(tenantPrivacyApi.bulk).toHaveBeenCalledWith('acme', 'erase', [U(1), U(2)], true));
  });

  it('enforces the per-action cap without calling the API', async () => {
    mount('Bulk');
    run('export', Array.from({ length: 26 }, (_, i) => U(i + 1)).join(' '));
    expect(await screen.findByText(/at most 25/i)).toBeTruthy();
    expect(tenantPrivacyApi.bulk).not.toHaveBeenCalled();
  });

  it('shows an Enterprise upgrade prompt, not an error, on 402', async () => {
    tenantPrivacyApi.bulk.mockRejectedValue({ isAxiosError: true, response: { status: 402, data: { error: { message: 'Enterprise plan required' } } } });
    mount('Bulk');
    run('do_not_sell', `${U(1)} ${U(2)}`);
    const prompt = await screen.findByTestId('bulk-upgrade');
    expect(prompt.textContent).toMatch(/Enterprise/);
    expect(prompt.textContent).toMatch(/automation/i);
    expect(screen.queryByTestId('privacy-request-error')).toBeNull();
    expect(screen.queryByText('Enterprise plan required')).toBeNull();
  });
});
