/**
 * Tests for the tenant KMS (BYOK) page: all four routes, error/403/tier-refusal
 * states, typed confirmation on destructive actions, and no key material in the DOM.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { maskKeyRef, tenantKmsApi } from '../../../services/tenantKmsApi';
import type { KmsConfig, KmsStatus } from '../../../services/tenantKmsApi';
import TenantKms from '../TenantKms';

vi.mock('../../../services/tenantKmsApi', async (orig) => {
  const actual = await orig<typeof import('../../../services/tenantKmsApi')>();
  return { ...actual, tenantKmsApi: { getStatus: vi.fn(), configure: vi.fn(), activate: vi.fn(), remove: vi.fn() } };
});
const api = vi.mocked(tenantKmsApi);

const FAKE_KEY = 'fake-key-ref-0123456789-FAKEKEY';
const cfg = (over: Partial<KmsConfig> = {}): KmsConfig => ({
  provider: 'fake-kms', keyRef: FAKE_KEY, region: 'fake-region-1', principal: null,
  externalId: 'fake-external-id', status: 'pending', lastVerifiedAt: null, lastErrorCode: null, updatedAt: null, ...over,
});
const status = (config: KmsConfig | null): KmsStatus => ({
  success: true, config, keys: [], supportedProviders: ['fake-kms'], platformPrincipals: { 'fake-kms': 'fake-principal' },
});
const rewrap = { targetKind: 'x', targetRef: 'y', total: 3, rewrapped: 2, alreadyCurrent: 1, failedVersions: [], ok: true };
const httpErr = (statusCode: number, code: string, message = 'nope') => ({
  response: { status: statusCode, data: { error: { code, message } } },
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return render(
    <MemoryRouter initialEntries={['/tenant/acme/kms']}>
      <Routes><Route path="/tenant/:tenantSlug/kms" element={<TenantKms />} /></Routes>
    </MemoryRouter>,
    { wrapper },
  );
}
const field = (l: string) => screen.getByLabelText(new RegExp(l, 'i')) as HTMLInputElement;

beforeEach(() => {
  vi.clearAllMocks();
  api.getStatus.mockResolvedValue(status(null));
});

describe('TenantKms', () => {
  it('GET: shows empty state and calls the slug', async () => {
    mount();
    expect(await screen.findByTestId('kms-empty')).toBeTruthy();
    expect(api.getStatus).toHaveBeenCalledWith('acme');
    expect(screen.getByTestId('kms-principal-hint').textContent).toContain('fake-principal');
  });

  it('GET: shows masked key only, never the full reference', async () => {
    api.getStatus.mockResolvedValue(status(cfg()));
    mount();
    expect((await screen.findByTestId('kms-keyref')).textContent).toBe(maskKeyRef(FAKE_KEY));
    expect(document.body.innerHTML).not.toContain(FAKE_KEY);
  });

  it('GET: API error renders an error state', async () => {
    api.getStatus.mockRejectedValue(httpErr(500, 'ENVELOPE_ERROR', 'boom'));
    mount();
    expect(await screen.findByText(/Failed to load encryption settings/i)).toBeTruthy();
  });

  it('GET: 403 renders a permission message', async () => {
    api.getStatus.mockRejectedValue(httpErr(403, 'FORBIDDEN'));
    mount();
    expect(await screen.findByText(/do not have permission/i)).toBeTruthy();
  });

  it('PUT: saves the config, clears inputs, never echoes the key', async () => {
    api.configure.mockResolvedValue({ success: true, config: cfg() });
    mount();
    await screen.findByTestId('kms-empty');
    fireEvent.change(field('Key identifier'), { target: { value: FAKE_KEY } });
    fireEvent.click(screen.getByRole('button', { name: /save key/i }));
    await waitFor(() => expect(api.configure).toHaveBeenCalledWith('acme', {
      provider: 'fake-kms', keyRef: FAKE_KEY, region: null, principal: null,
    }));
    await waitFor(() => expect(field('Key identifier').value).toBe(''));
    expect(document.body.innerHTML).not.toContain(FAKE_KEY);
    expect(document.querySelectorAll('input[type=password]').length).toBe(0);
    expect(await screen.findByTestId('kms-notice')).toBeTruthy();
  });

  it('PUT: tier refusal renders the upgrade prompt, not an error', async () => {
    api.configure.mockRejectedValue(httpErr(403, 'EXTERNAL_KMS_NOT_ENTITLED'));
    mount();
    await screen.findByTestId('kms-empty');
    fireEvent.change(field('Key identifier'), { target: { value: FAKE_KEY } });
    fireEvent.click(screen.getByRole('button', { name: /save key/i }));
    expect(await screen.findByTestId('kms-upgrade')).toBeTruthy();
    expect(screen.queryByTestId('kms-action-error')).toBeNull();
    expect(field('Key identifier').value).toBe('');
  });

  it('PUT: API error and plain 403 surface as errors', async () => {
    api.configure.mockRejectedValueOnce(httpErr(422, 'INVALID_KMS_CONFIG', 'bad key id'));
    mount();
    await screen.findByTestId('kms-empty');
    fireEvent.change(field('Key identifier'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: /save key/i }));
    expect((await screen.findByTestId('kms-action-error')).textContent).toContain('bad key id');
  });

  it('PUT: plain 403 shows permission message', async () => {
    api.configure.mockRejectedValue(httpErr(403, 'FORBIDDEN', 'x'));
    mount();
    await screen.findByTestId('kms-empty');
    fireEvent.change(field('Key identifier'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: /save key/i }));
    expect((await screen.findByTestId('kms-action-error')).textContent).toMatch(/permission/i);
  });

  it('POST activate: pending config can be activated and shows rewrap counts', async () => {
    api.getStatus.mockResolvedValue(status(cfg()));
    api.activate.mockResolvedValue({ success: true, config: cfg({ status: 'active' }), rewrap });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /activate key/i }));
    await waitFor(() => expect(api.activate).toHaveBeenCalledWith('acme'));
    expect((await screen.findByTestId('kms-rewrap')).textContent).toContain('2 of 3');
  });

  it('POST activate: clean rewrap shows success and no warning', async () => {
    api.getStatus.mockResolvedValue(status(cfg()));
    api.activate.mockResolvedValue({ success: true, config: cfg({ status: 'active' }), rewrap });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /activate key/i }));
    expect(await screen.findByTestId('kms-notice')).toBeTruthy();
    expect(screen.queryByTestId('kms-warning')).toBeNull();
  });

  it('POST activate: partial failure warns, no success banner', async () => {
    api.getStatus.mockResolvedValue(status(cfg()));
    api.activate.mockResolvedValue({
      success: true, config: cfg({ status: 'active' }), rewrap: { ...rewrap, rewrapped: 1, failedVersions: [2], ok: false },
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /activate key/i }));
    expect((await screen.findByTestId('kms-warning')).textContent).toMatch(/do not revoke/i);
    expect(screen.queryByTestId('kms-notice')).toBeNull();
    expect(screen.getByTestId('kms-rewrap').textContent).toContain('1 failed');
  });

  it('POST activate: total failure (ok=false, nothing rewrapped) warns', async () => {
    api.getStatus.mockResolvedValue(status(cfg()));
    api.activate.mockResolvedValue({
      success: true, config: cfg(), rewrap: { ...rewrap, rewrapped: 0, alreadyCurrent: 0, failedVersions: [1, 2, 3], ok: false },
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /activate key/i }));
    expect(await screen.findByTestId('kms-warning')).toBeTruthy();
    expect(screen.queryByTestId('kms-notice')).toBeNull();
  });

  it('DELETE: 409 REWRAP_INCOMPLETE warns not to revoke the external key', async () => {
    api.getStatus.mockResolvedValue(status(cfg({ status: 'active' })));
    api.remove.mockRejectedValue({
      response: { status: 409, data: { error: { code: 'REWRAP_INCOMPLETE', message: 'x', details: { failedVersions: [4, 5] } } } },
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /remove external key/i }));
    fireEvent.change(field('Confirmation'), { target: { value: 'REMOVE' } });
    fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }));
    const w = await screen.findByTestId('kms-warning');
    expect(w.textContent).toMatch(/2 data key/);
    expect(w.textContent).toMatch(/do not revoke/i);
    expect(screen.queryByTestId('kms-notice')).toBeNull();
  });

  it('POST activate: error and tier refusal', async () => {
    api.getStatus.mockResolvedValue(status(cfg()));
    api.activate.mockRejectedValueOnce(httpErr(422, 'KMS_ACCESS_DENIED', 'denied by provider'));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /activate key/i }));
    expect((await screen.findByTestId('kms-action-error')).textContent).toContain('denied by provider');
    api.activate.mockRejectedValueOnce(httpErr(403, 'EXTERNAL_KMS_NOT_ENTITLED'));
    fireEvent.click(screen.getByRole('button', { name: /activate key/i }));
    expect(await screen.findByTestId('kms-upgrade')).toBeTruthy();
  });

  it('DELETE: requires typed REMOVE, then calls the API', async () => {
    api.getStatus.mockResolvedValue(status(cfg({ status: 'active' })));
    api.remove.mockResolvedValue({ success: true, rewrap });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /remove external key/i }));
    const confirm = screen.getByRole('button', { name: /^confirm$/i }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(field('Confirmation'), { target: { value: 'nope' } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(field('Confirmation'), { target: { value: 'REMOVE' } });
    fireEvent.click(confirm);
    await waitFor(() => expect(api.remove).toHaveBeenCalledWith('acme'));
    expect(await screen.findByTestId('kms-rewrap')).toBeTruthy();
  });

  it('DELETE: cancel makes no call; 409 error surfaces', async () => {
    api.getStatus.mockResolvedValue(status(cfg({ status: 'active' })));
    api.remove.mockRejectedValue(httpErr(409, 'CONFLICT', 'some data keys could not be re-wrapped'));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /remove external key/i }));
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(api.remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /remove external key/i }));
    fireEvent.change(field('Confirmation'), { target: { value: 'REMOVE' } });
    fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }));
    expect((await screen.findByTestId('kms-action-error')).textContent).toContain('could not be re-wrapped');
  });

  it('rotate: replacing an active key needs typed ROTATE', async () => {
    api.getStatus.mockResolvedValue(status(cfg({ status: 'active' })));
    api.configure.mockResolvedValue({ success: true, config: cfg() });
    mount();
    await screen.findByTestId('kms-config');
    fireEvent.change(field('Key identifier'), { target: { value: FAKE_KEY } });
    fireEvent.click(screen.getByRole('button', { name: /save key/i }));
    expect(api.configure).not.toHaveBeenCalled();
    fireEvent.change(field('Confirmation'), { target: { value: 'ROTATE' } });
    fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }));
    await waitFor(() => expect(api.configure).toHaveBeenCalled());
    await waitFor(() => expect(field('Key identifier').value).toBe(''));
  });
});

describe('maskKeyRef', () => {
  it('masks all but the tail', () => {
    expect(maskKeyRef(FAKE_KEY)).toBe('****FAKEKEY'.slice(0, 4) + FAKE_KEY.slice(-6));
    expect(maskKeyRef('abc')).toBe('****');
    expect(maskKeyRef('')).toBe('');
  });
});
