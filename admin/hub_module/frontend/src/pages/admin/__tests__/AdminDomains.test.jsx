/**
 * Tests for the community Custom Domains page: domain list with verification
 * state + DNS instructions (copy helpers), add / verify / remove flows with
 * success and failure messaging.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminDomains from '../AdminDomains';
import { adminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  adminApi: { getDomains: vi.fn(), addDomain: vi.fn(), verifyDomain: vi.fn(), removeDomain: vi.fn() },
}));

const DOMAINS = [
  { id: 1, domain: 'pending.example.com', isVerified: false, verificationToken: 'tok-abc' },
  { id: 2, domain: 'live.example.com', isVerified: true, verifiedAt: '2026-02-03T12:00:00Z' },
  { id: 3, domain: 'tokenless.example.com', isVerified: false },
];

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/domains']}>
      <Routes>
        <Route path="/admin/:communityId/domains" element={<AdminDomains />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function loaded(domains = DOMAINS) {
  adminApi.getDomains.mockResolvedValue({ data: { domains } });
  mount();
  await screen.findByText('Custom Domains');
}

const card = (domain) => within(screen.getByText(domain).closest('div.rounded-lg'));
const failure = (message) => ({ response: { data: { error: { message } } } });

beforeEach(() => {
  vi.clearAllMocks();
  adminApi.addDomain.mockResolvedValue({ data: { success: true } });
  adminApi.verifyDomain.mockResolvedValue({ data: { success: true } });
  adminApi.removeDomain.mockResolvedValue({ data: { success: true } });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('AdminDomains listing', () => {
  it('shows a spinner, then loads domains for the community', async () => {
    adminApi.getDomains.mockResolvedValue({ data: { domains: DOMAINS } });
    const { container } = mount();
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();
    await screen.findByText('Custom Domains');
    expect(adminApi.getDomains).toHaveBeenCalledWith('7');
  });

  it('shows verification state, verified date and per-state actions', async () => {
    await loaded();
    const pending = card('pending.example.com');
    expect(pending.getByText('Pending Verification')).toBeInTheDocument();
    expect(pending.getByRole('button', { name: /Verify/ })).toBeInTheDocument();

    const live = card('live.example.com');
    expect(live.getByText('Verified')).toBeInTheDocument();
    expect(live.getByText(`Verified ${new Date('2026-02-03T12:00:00Z').toLocaleDateString()}`)).toBeInTheDocument();
    expect(live.queryByRole('button', { name: /Verify/ })).not.toBeInTheDocument();
  });

  it('shows DNS instructions only for unverified domains that have a token', async () => {
    await loaded();
    expect(screen.getAllByText('DNS Verification Required')).toHaveLength(1);
    expect(screen.getByText('_waddlebot.pending.example.com')).toBeInTheDocument();
    expect(screen.getByText('tok-abc')).toBeInTheDocument();
  });

  it('shows the empty state', async () => {
    await loaded([]);
    expect(screen.getByText('No Custom Domains')).toBeInTheDocument();
  });

  it('treats a missing domains array as empty', async () => {
    adminApi.getDomains.mockResolvedValue({ data: {} });
    mount();
    expect(await screen.findByText('No Custom Domains')).toBeInTheDocument();
  });

  it.each([
    ['the server message', failure('forbidden'), 'forbidden'],
    ['a generic fallback', new Error('x'), 'Failed to load domains'],
  ])('shows %s when loading fails, dismissible', async (_label, err, text) => {
    adminApi.getDomains.mockRejectedValue(err);
    mount();
    const banner = (await screen.findByText(text)).closest('div.rounded-lg');
    fireEvent.click(within(banner).getByRole('button'));
    expect(screen.queryByText(text)).not.toBeInTheDocument();
  });
});

describe('AdminDomains add', () => {
  it('keeps Add disabled until a domain is typed, and ignores whitespace', async () => {
    await loaded();
    const add = screen.getByRole('button', { name: /Add Domain/ });
    expect(add).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText('example.com'), { target: { value: '   ' } });
    expect(add).toBeDisabled();
    fireEvent.submit(screen.getByPlaceholderText('example.com').closest('form'));
    expect(adminApi.addDomain).not.toHaveBeenCalled();
  });

  it('adds a trimmed, lower-cased domain, clears the input and reloads', async () => {
    await loaded();
    fireEvent.change(screen.getByPlaceholderText('example.com'), { target: { value: '  New.Example.COM ' } });
    fireEvent.click(screen.getByRole('button', { name: /Add Domain/ }));

    expect(await screen.findByText(/Domain added successfully/)).toBeInTheDocument();
    expect(adminApi.addDomain).toHaveBeenCalledWith('7', 'new.example.com');
    await waitFor(() => expect(adminApi.getDomains).toHaveBeenCalledTimes(2));
    // The reload remounts the form, so re-query the (now cleared) input.
    expect(await screen.findByPlaceholderText('example.com')).toHaveValue('');
  });

  it.each([
    ['the server message', failure('already taken'), 'already taken'],
    ['a generic fallback', new Error('x'), 'Failed to add domain'],
  ])('shows %s when adding fails and keeps the input', async (_label, err, text) => {
    adminApi.addDomain.mockRejectedValue(err);
    await loaded();
    const input = screen.getByPlaceholderText('example.com');
    fireEvent.change(input, { target: { value: 'dup.example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Add Domain/ }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(input).toHaveValue('dup.example.com');
  });

  it('shows Adding... while in flight', async () => {
    let release;
    adminApi.addDomain.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await loaded();
    fireEvent.change(screen.getByPlaceholderText('example.com'), { target: { value: 'x.example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Add Domain/ }));
    expect(await screen.findByRole('button', { name: /Adding/ })).toBeDisabled();
    release({ data: {} });
    await screen.findByText(/Domain added successfully/);
  });

  it('lets the success message be dismissed', async () => {
    await loaded();
    fireEvent.change(screen.getByPlaceholderText('example.com'), { target: { value: 'x.example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Add Domain/ }));
    const banner = (await screen.findByText(/Domain added successfully/)).closest('div.rounded-lg');
    fireEvent.click(within(banner).getByRole('button'));
    expect(screen.queryByText(/Domain added successfully/)).not.toBeInTheDocument();
  });
});

describe('AdminDomains verify', () => {
  it('verifies a domain and reloads', async () => {
    await loaded();
    fireEvent.click(card('pending.example.com').getByRole('button', { name: /Verify/ }));
    expect(await screen.findByText('Domain verified successfully!')).toBeInTheDocument();
    expect(adminApi.verifyDomain).toHaveBeenCalledWith('7', 1);
    await waitFor(() => expect(adminApi.getDomains).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['the API message', { data: { success: false, message: 'TXT record not found' } }, 'TXT record not found'],
    ['a generic DNS hint', { data: { success: false } }, 'Verification failed. Please check your DNS records.'],
  ])('shows %s when verification does not succeed', async (_label, reply, text) => {
    adminApi.verifyDomain.mockResolvedValue(reply);
    await loaded();
    fireEvent.click(card('pending.example.com').getByRole('button', { name: /Verify/ }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it.each([
    ['the server message', failure('rate limited'), 'rate limited'],
    ['a generic fallback', new Error('x'), 'Failed to verify domain'],
  ])('shows %s when the verify request fails', async (_label, err, text) => {
    adminApi.verifyDomain.mockRejectedValue(err);
    await loaded();
    fireEvent.click(card('pending.example.com').getByRole('button', { name: /Verify/ }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('shows Verifying... while in flight', async () => {
    let release;
    adminApi.verifyDomain.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await loaded();
    fireEvent.click(card('pending.example.com').getByRole('button', { name: /Verify/ }));
    expect(await screen.findByRole('button', { name: /Verifying/ })).toBeDisabled();
    release({ data: { success: true } });
    await screen.findByText('Domain verified successfully!');
  });
});

describe('AdminDomains remove', () => {
  const openRemove = (domain = 'live.example.com') => {
    const buttons = card(domain).getAllByRole('button');
    fireEvent.click(buttons[buttons.length - 1]);
    return within(screen.getByRole('heading', { name: 'Remove Domain?' }).closest('div.rounded-lg'));
  };

  it('asks for confirmation naming the domain, and Cancel dismisses it', async () => {
    await loaded();
    const dialog = openRemove();
    expect(dialog.getByText('live.example.com')).toBeInTheDocument();
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Remove Domain?' })).not.toBeInTheDocument();
    expect(adminApi.removeDomain).not.toHaveBeenCalled();
  });

  it('removes the domain, closes the dialog and reloads', async () => {
    await loaded();
    fireEvent.click(openRemove().getByRole('button', { name: 'Remove Domain' }));

    expect(await screen.findByText('Domain removed successfully')).toBeInTheDocument();
    expect(adminApi.removeDomain).toHaveBeenCalledWith('7', 2);
    expect(screen.queryByRole('heading', { name: 'Remove Domain?' })).not.toBeInTheDocument();
    await waitFor(() => expect(adminApi.getDomains).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['the server message', failure('in use'), 'in use'],
    ['a generic fallback', new Error('x'), 'Failed to remove domain'],
  ])('shows %s when removal fails and keeps the dialog', async (_label, err, text) => {
    adminApi.removeDomain.mockRejectedValue(err);
    await loaded();
    fireEvent.click(openRemove().getByRole('button', { name: 'Remove Domain' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Remove Domain?' })).toBeInTheDocument();
  });

  it('shows Removing... while in flight', async () => {
    let release;
    adminApi.removeDomain.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await loaded();
    fireEvent.click(openRemove().getByRole('button', { name: 'Remove Domain' }));
    expect(await screen.findByRole('button', { name: 'Removing...' })).toBeDisabled();
    release({ data: {} });
    await screen.findByText('Domain removed successfully');
  });
});

describe('AdminDomains DNS copy helpers', () => {
  it('copies the record name and the token, flipping the icon for 2 seconds', async () => {
    const writeText = vi.fn();
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    await loaded();

    const nameRow = screen.getByText('_waddlebot.pending.example.com').closest('div.flex');
    const tokenRow = screen.getByText('tok-abc').closest('div.flex');

    vi.useFakeTimers({ toFake: ['setTimeout'] });
    await act(async () => {
      fireEvent.click(within(nameRow).getByRole('button'));
    });
    expect(writeText).toHaveBeenCalledWith('_waddlebot.pending.example.com');
    expect(nameRow.querySelector('.text-green-400')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(within(tokenRow).getByRole('button'));
    });
    expect(writeText).toHaveBeenLastCalledWith('tok-abc');
    expect(tokenRow.querySelector('.text-green-400')).toBeInTheDocument();
    expect(nameRow.querySelector('.text-green-400')).not.toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(tokenRow.querySelector('.text-green-400')).not.toBeInTheDocument();
  });
});
