/** Tenant signup email-domain restriction card: load, save, effective policy, 403 + errors. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../services/api', () => ({
  tenantApi: { getSignupDomains: vi.fn(), updateSignupDomains: vi.fn() },
}));
import { tenantApi } from '../../../services/api';
import TenantSignupDomains from '../TenantSignupDomains';

const policy = (domains, platformDomains = [], effectiveDomains = domains) => ({
  data: { success: true, domains, platformDomains, effectiveDomains, restricted: domains.length > 0 },
});

beforeEach(() => {
  vi.clearAllMocks();
  tenantApi.getSignupDomains.mockResolvedValue(policy(['acme.test']));
});

const field = () => screen.findByLabelText('Allowed domains (comma-separated)');

describe('TenantSignupDomains', () => {
  it('loads the tenant list and saves an edited, parsed list', async () => {
    tenantApi.updateSignupDomains.mockResolvedValue(policy(['acme.test', 'corp.test']));
    render(<TenantSignupDomains tenantSlug="acme" />);
    const input = await field();
    await waitFor(() => expect(input.value).toBe('acme.test'));
    expect(tenantApi.getSignupDomains).toHaveBeenCalledWith('acme');
    fireEvent.change(input, { target: { value: 'acme.test,  corp.test ' } });
    fireEvent.click(screen.getByRole('button', { name: /save signup domains/i }));
    await waitFor(() =>
      expect(tenantApi.updateSignupDomains).toHaveBeenCalledWith('acme', ['acme.test', 'corp.test']),
    );
    expect(await screen.findByText('Signup domains saved.')).toBeTruthy();
  });

  it('clearing the field removes the restriction', async () => {
    tenantApi.updateSignupDomains.mockResolvedValue(policy([]));
    render(<TenantSignupDomains tenantSlug="acme" />);
    const input = await field();
    await waitFor(() => expect(input.value).toBe('acme.test'));
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: /save signup domains/i }));
    await waitFor(() => expect(tenantApi.updateSignupDomains).toHaveBeenCalledWith('acme', []));
  });

  it('explains the platform intersection, including deny-all', async () => {
    tenantApi.getSignupDomains.mockResolvedValue(policy(['other.test'], ['acme.test'], []));
    render(<TenantSignupDomains tenantSlug="acme" />);
    const note = await screen.findByTestId('signup-domains-effective');
    expect(note.textContent).toMatch(/acme\.test/);
    expect(note.textContent).toMatch(/no domain can sign up/);
  });

  it('shows a permission message on 403 and a server message on 400', async () => {
    tenantApi.updateSignupDomains.mockRejectedValueOnce({ response: { status: 403 } });
    render(<TenantSignupDomains tenantSlug="acme" />);
    await field();
    await waitFor(() => expect(screen.getByRole('button', { name: /save signup domains/i }).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: /save signup domains/i }));
    expect(await screen.findByText(/do not have permission/)).toBeTruthy();

    tenantApi.updateSignupDomains.mockRejectedValueOnce({
      response: { status: 400, data: { error: { message: 'Each signup domain must be a domain name like example.com' } } },
    });
    fireEvent.click(screen.getByRole('button', { name: /save signup domains/i }));
    expect(await screen.findByText(/must be a domain name/)).toBeTruthy();
  });

  it('surfaces a load failure', async () => {
    tenantApi.getSignupDomains.mockRejectedValue({ response: { status: 500, data: { error: { message: 'boom' } } } });
    render(<TenantSignupDomains tenantSlug="acme" />);
    expect(await screen.findByRole('alert')).toBeTruthy();
  });
});
