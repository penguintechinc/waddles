/**
 * Tests for the tenant license key page: states (loading / not configured / configured / 403 /
 * error), set + replace + remove flows, and the write-only guarantee: the key never appears in the
 * DOM, the field is cleared on success AND error, and nothing is logged.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { tenantLicenseApi } from '../../../services/tenantLicenseApi';
import TenantLicense from '../TenantLicense';

vi.mock('../../../services/tenantLicenseApi', async (orig) => {
  const actual = await orig<typeof import('../../../services/tenantLicenseApi')>();
  return { ...actual, tenantLicenseApi: { get: vi.fn(), set: vi.fn(), remove: vi.fn() } };
});
const api = vi.mocked(tenantLicenseApi);

const SECRET = 'FAKE-LICENSE-KEY-0123456789-ABCDEF';
const NOT_CONFIGURED = { configured: false, fingerprint: null, setAt: null };
const CONFIGURED = { configured: true, fingerprint: 'ab12cd34', setAt: '2026-11-01T12:00:00Z' };
const httpErr = (status: number, message = 'nope') => ({ response: { status, data: { error: { message } } } });

function mount() {
  return render(
    <MemoryRouter initialEntries={['/tenant/acme/license']}>
      <Routes>
        <Route path="/tenant/:tenantSlug/license" element={<TenantLicense />} />
      </Routes>
    </MemoryRouter>,
  );
}
const keyField = () => document.getElementById('license-key') as HTMLInputElement;
const noSecret = () => expect(document.body.innerHTML).not.toContain(SECRET);

let spies: ReturnType<typeof vi.spyOn>[] = [];
beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue(NOT_CONFIGURED);
  spies = (['log', 'debug', 'info', 'warn', 'error'] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
});
afterEach(() => vi.restoreAllMocks());

function noKeyLogged() {
  for (const s of spies) expect(JSON.stringify(s.mock.calls)).not.toContain(SECRET);
}

describe('TenantLicense', () => {
  it('shows a spinner then the not-configured state with the honest pool note', async () => {
    mount();
    expect(screen.getByRole('status', { name: 'Loading license' })).toBeTruthy();
    expect((await screen.findByTestId('license-not-configured')).textContent).toContain('Free tier limits apply; team pools unavailable');
    expect(api.get).toHaveBeenCalledWith('acme');
    expect(screen.getByTestId('license-pending-note').textContent).toMatch(/pending/i);
  });

  it('configured: shows fingerprint and set time, never a key; offers Replace and Remove', async () => {
    api.get.mockResolvedValue(CONFIGURED);
    mount();
    expect((await screen.findByTestId('license-fingerprint')).textContent).toBe('ab12cd34');
    expect(screen.getByRole('button', { name: 'Replace key' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Remove license key' })).toBeTruthy();
    noSecret();
  });

  it('the key field is a password input with autocomplete off and no spellcheck', async () => {
    mount();
    await screen.findByTestId('license-not-configured');
    const f = keyField();
    expect(f.type).toBe('password');
    expect(f.getAttribute('autocomplete')).toBe('off');
    expect(f.getAttribute('spellcheck')).toBe('false');
  });

  it('set success: trims, sends the key once, clears the field, updates status, no key anywhere', async () => {
    api.set.mockResolvedValue(CONFIGURED);
    mount();
    await screen.findByTestId('license-not-configured');
    fireEvent.change(keyField(), { target: { value: `  ${SECRET}\n` } });
    fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    await waitFor(() => expect(api.set).toHaveBeenCalledWith('acme', SECRET));
    expect((await screen.findByTestId('license-fingerprint')).textContent).toBe('ab12cd34');
    expect(keyField().value).toBe('');
    expect(screen.getByTestId('license-notice').textContent).toContain('saved');
    noSecret();
    noKeyLogged();
  });

  it('set error: shows the message and still clears the field', async () => {
    api.set.mockRejectedValue(httpErr(400, 'licenseKey must be valid'));
    mount();
    await screen.findByTestId('license-not-configured');
    fireEvent.change(keyField(), { target: { value: SECRET } });
    fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    expect((await screen.findByTestId('license-notice')).textContent).toContain('licenseKey must be valid');
    expect(keyField().value).toBe('');
    noSecret();
    noKeyLogged();
  });

  it('403 on set shows a permission message and clears the field', async () => {
    api.set.mockRejectedValue(httpErr(403));
    mount();
    await screen.findByTestId('license-not-configured');
    fireEvent.change(keyField(), { target: { value: SECRET } });
    fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    expect((await screen.findByTestId('license-notice')).textContent).toContain('permission');
    expect(keyField().value).toBe('');
  });

  it('client validation rejects a malformed key without calling the API and clears the field', async () => {
    mount();
    await screen.findByTestId('license-not-configured');
    fireEvent.change(keyField(), { target: { value: 'short key!' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    expect((await screen.findByRole('alert')).textContent).toContain('16-256 characters');
    expect(api.set).not.toHaveBeenCalled();
    expect(keyField().value).toBe('');
  });

  it('remove requires typing REMOVE, then clears and shows not configured', async () => {
    api.get.mockResolvedValue(CONFIGURED);
    api.remove.mockResolvedValue(undefined);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Remove license key' }));
    const confirm = screen.getByRole('button', { name: 'Confirm removal' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Type REMOVE to confirm'), { target: { value: 'remove' } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Type REMOVE to confirm'), { target: { value: 'REMOVE' } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => expect(api.remove).toHaveBeenCalledWith('acme'));
    expect(await screen.findByTestId('license-not-configured')).toBeTruthy();
  });

  it('remove failure keeps the key configured and shows the error', async () => {
    api.get.mockResolvedValue(CONFIGURED);
    api.remove.mockRejectedValue(httpErr(500, 'boom'));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Remove license key' }));
    fireEvent.change(screen.getByLabelText('Type REMOVE to confirm'), { target: { value: 'REMOVE' } });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm removal' }));
    expect((await screen.findByTestId('license-notice')).textContent).toContain('boom');
    expect(screen.getByTestId('license-fingerprint')).toBeTruthy();
  });

  it('403 on load shows the forbidden state and no form', async () => {
    api.get.mockRejectedValue(httpErr(403));
    mount();
    expect(await screen.findByTestId('license-forbidden')).toBeTruthy();
    expect(screen.queryByLabelText(/license key/i)).toBeNull();
  });

  it('load error shows the message with Retry', async () => {
    api.get.mockRejectedValueOnce(httpErr(500, 'db down'));
    mount();
    expect((await screen.findByTestId('license-error')).textContent).toContain('db down');
    api.get.mockResolvedValue(CONFIGURED);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTestId('license-fingerprint')).toBeTruthy();
  });
});
