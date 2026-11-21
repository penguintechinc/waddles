/**
 * Tests for the operator license-pool dialog: load, set each tier, -1 / null mapping, validation,
 * the 409 operator-only state, bypassDomain=false lock, and 403 / error states.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { operatorLicensePoolsApi } from '../../../services/tenantLicenseApi';
import type { OperatorPoolsResponse } from '../../../services/tenantLicenseApi';
import TenantLicensePools from '../TenantLicensePools';
import { NOT_ALLOWED_MESSAGE, draftToLimit } from '../licensePoolDraft';

vi.mock('../../../services/tenantLicenseApi', async (orig) => {
  const actual = await orig<typeof import('../../../services/tenantLicenseApi')>();
  return { ...actual, operatorLicensePoolsApi: { get: vi.fn(), set: vi.fn() } };
});
const api = vi.mocked(operatorLicensePoolsApi);

const resp = (over: Partial<OperatorPoolsResponse> = {}): OperatorPoolsResponse => ({
  success: true,
  tenantId: 'aaaaaaaa-0000-4000-8000-000000000005',
  bypassDomain: true,
  pools: [
    { tierCode: 2, limit: 5, source: 'operator_set', observedAt: '2026-11-01T00:00:00Z' },
  ],
  ...over,
});
const httpErr = (status: number, code?: string, message?: string) => ({
  response: { status, data: { error: { code, message } } },
});

const mount = () => render(<TenantLicensePools tenantId="aaaaaaaa-0000-4000-8000-000000000005" tenantSlug="acme" onClose={vi.fn()} />);
const input = (_tier: number) => screen.getByLabelText(/Premium pool size/) as HTMLInputElement;
const save = (_tier: number) => fireEvent.click(screen.getByRole('button', { name: 'Save Premium' }));

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue(resp());
});

describe('draftToLimit', () => {
  it.each([
    [{ text: '', cleared: true }, { limit: null }],
    [{ text: '-1', cleared: false }, { limit: -1 }],
    [{ text: '0', cleared: false }, { limit: 0 }],
    [{ text: ' 12 ', cleared: false }, { limit: 12 }],
  ])('maps %j', (d, out) => expect(draftToLimit(d)).toEqual(out));
  it.each(['', 'abc', '1.5', '-2'])('rejects %j', (text) => expect(draftToLimit({ text, cleared: false })).toHaveProperty('error'));
});

describe('TenantLicensePools', () => {
  it('loads and shows ONE Premium pool (no Professional/Enterprise rows)', async () => {
    mount();
    expect(screen.getByRole('status', { name: 'Loading license pools' })).toBeTruthy();
    expect((await screen.findByTestId('pool-current-2')).textContent).toContain('Current: 5');
    expect(screen.queryByTestId('pool-current-3')).toBeNull();
    expect(screen.getAllByRole('form')).toHaveLength(1);
    expect(screen.queryByText(/Professional|Enterprise/)).toBeNull();
    expect(api.get).toHaveBeenCalledWith('aaaaaaaa-0000-4000-8000-000000000005');
    expect(input(2).value).toBe('5');
    expect(screen.queryByTestId('pools-locked')).toBeNull();
  });

  it('shows not licensed wording for a null limit', async () => {
    api.get.mockResolvedValue(resp({ pools: [{ tierCode: 2, limit: null, source: 'operator_set', observedAt: 'x' }] }));
    mount();
    expect((await screen.findByTestId('pool-current-2')).textContent).toContain('not licensed');
    expect(input(2).placeholder).toBe('Not licensed');
  });

  it('sets the Premium pool to a number', async () => {
    api.set.mockResolvedValue(resp({ pools: [{ tierCode: 2, limit: 9, source: 'operator_set', observedAt: 'x' }] }));
    mount();
    await screen.findByTestId('pool-current-2');
    fireEvent.change(input(2), { target: { value: '9' } });
    save(2);
    await waitFor(() => expect(api.set).toHaveBeenCalledWith('aaaaaaaa-0000-4000-8000-000000000005', 2, 9));
    expect(await screen.findByText('Premium pool saved.')).toBeTruthy();
  });

  it('Unlimited shortcut sends -1 for the Premium pool', async () => {
    api.set.mockResolvedValue(resp());
    mount();
    await screen.findByTestId('pool-current-2');
    const form = screen.getByRole('form', { name: 'Premium pool' });
    fireEvent.click(form.querySelector('button[type=button]') as HTMLButtonElement);
    expect(input(2).value).toBe('-1');
    save(2);
    await waitFor(() => expect(api.set).toHaveBeenCalledWith('aaaaaaaa-0000-4000-8000-000000000005', 2, -1));
  });

  it('Not licensed sends null', async () => {
    api.set.mockResolvedValue(resp());
    mount();
    await screen.findByTestId('pool-current-2');
    fireEvent.click(screen.getAllByRole('button', { name: 'Not licensed' })[0] as HTMLElement);
    save(2);
    await waitFor(() => expect(api.set).toHaveBeenCalledWith('aaaaaaaa-0000-4000-8000-000000000005', 2, null));
  });

  it('validates input client-side and does not call the API', async () => {
    mount();
    await screen.findByTestId('pool-current-2');
    fireEvent.change(input(2), { target: { value: '-5' } });
    save(2);
    expect((await screen.findByRole('alert')).textContent).toContain('-1 (unlimited)');
    expect(api.set).not.toHaveBeenCalled();
  });

  it('409 POOL_OPERATOR_SET_NOT_ALLOWED shows the message and disables editing', async () => {
    api.set.mockRejectedValue(httpErr(409, 'POOL_OPERATOR_SET_NOT_ALLOWED'));
    mount();
    await screen.findByTestId('pool-current-2');
    fireEvent.change(input(2), { target: { value: '3' } });
    save(2);
    expect((await screen.findByTestId('pools-locked')).textContent).toBe(NOT_ALLOWED_MESSAGE);
    expect(input(2).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Save Premium' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('bypassDomain=false from the GET starts locked', async () => {
    api.get.mockResolvedValue(resp({ bypassDomain: false }));
    mount();
    expect((await screen.findByTestId('pools-locked')).textContent).toBe(NOT_ALLOWED_MESSAGE);
    expect(input(2).disabled).toBe(true);
  });

  it('403 on load shows forbidden', async () => {
    api.get.mockRejectedValue(httpErr(403));
    mount();
    expect(await screen.findByTestId('pools-forbidden')).toBeTruthy();
  });

  it('load error shows message and Retry recovers', async () => {
    api.get.mockRejectedValueOnce(httpErr(500, 'X', 'db down'));
    mount();
    expect((await screen.findByTestId('pools-error')).textContent).toContain('db down');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTestId('pool-current-2')).toBeTruthy();
  });

  it('generic save failure and 403 on save show a per-tier error', async () => {
    api.set.mockRejectedValueOnce(httpErr(500, 'X', 'boom')).mockRejectedValueOnce(httpErr(403));
    mount();
    await screen.findByTestId('pool-current-2');
    save(2);
    expect((await screen.findByRole('alert')).textContent).toContain('boom');
    save(2);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('permission'));
  });
});
