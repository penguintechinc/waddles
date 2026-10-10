/**
 * Tests for the Personal Access Token page: empty vs active-token card,
 * create flow (name validation, optional scope ceiling, one-time reveal,
 * copy), revoke, and error handling.
 *
 * `tokenApi` is mocked with axios-shaped responses (`{ data: { pat } }`,
 * `{ data: { scopes } }`, `{ data: { token } }`); see
 * PersonalAccessToken.wire.test.jsx for the contract against the real
 * HTTP-backed client.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import PersonalAccessToken from '../PersonalAccessToken';
import { tokenApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  tokenApi: { getPAT: vi.fn(), getPATScopes: vi.fn(), createPAT: vi.fn(), revokePAT: vi.fn() },
}));

const SCOPES = [
  { scope_key: 'profile:read', description: 'Read profile', category: 'Profile' },
  { scope_key: 'profile:write', category: 'Profile' },
  { scope_key: 'misc:ping' },
];

const TOKEN = {
  name: 'Deploy script',
  created_at: '2026-01-02T12:00:00Z',
  last_used_at: '2026-03-01T12:00:00Z',
  scope_ceiling: ['profile:read', 'misc:ping'],
};

async function loadedEmpty() {
  tokenApi.getPAT.mockResolvedValue({ data: { pat: null } });
  render(<PersonalAccessToken />);
  await screen.findByText('No active token');
}

async function loadedWithToken(token = TOKEN) {
  tokenApi.getPAT.mockResolvedValue({ data: { pat: token } });
  render(<PersonalAccessToken />);
  await screen.findByText(token.name);
}

const form = () => within(screen.getByRole('heading', { name: 'Create Personal Access Token' }).parentElement.querySelector('form'));

async function openCreate() {
  await loadedEmpty();
  fireEvent.click(screen.getByRole('button', { name: /Create Token/ }));
}

beforeEach(() => {
  vi.clearAllMocks();
  tokenApi.getPATScopes.mockResolvedValue({ data: { scopes: SCOPES } });
  tokenApi.createPAT.mockResolvedValue({ data: { token: 'pat_secret' } });
  tokenApi.revokePAT.mockResolvedValue({});
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('PersonalAccessToken display', () => {
  it('shows Loading… then the empty state when there is no token', async () => {
    tokenApi.getPAT.mockResolvedValue({ data: { pat: null } });
    render(<PersonalAccessToken />);
    expect(screen.getByText('Loading…')).toBeInTheDocument();
    expect(await screen.findByText('No active token')).toBeInTheDocument();
    expect(tokenApi.getPAT).toHaveBeenCalledTimes(1);
    expect(tokenApi.getPATScopes).toHaveBeenCalledTimes(1);
  });

  it('treats a missing token field as no token', async () => {
    tokenApi.getPAT.mockResolvedValue({});
    tokenApi.getPATScopes.mockResolvedValue({});
    render(<PersonalAccessToken />);
    expect(await screen.findByText('No active token')).toBeInTheDocument();
  });

  it('shows the active token with dates and its scope ceiling', async () => {
    await loadedWithToken();
    expect(screen.getByText(`Created ${new Date(TOKEN.created_at).toLocaleDateString()}`)).toBeInTheDocument();
    expect(screen.getByText(new Date(TOKEN.last_used_at).toLocaleDateString())).toBeInTheDocument();
    expect(screen.getByText('profile:read')).toBeInTheDocument();
    expect(screen.getByText('misc:ping')).toBeInTheDocument();
    expect(screen.queryByText('Full permissions')).not.toBeInTheDocument();
  });

  it.each([
    ['no ceiling', { scope_ceiling: null }],
    ['an empty ceiling', { scope_ceiling: [] }],
  ])('shows Full permissions and Never for a token with %s that was never used', async (_label, extra) => {
    await loadedWithToken({ ...TOKEN, last_used_at: null, ...extra });
    expect(screen.getByText('Full permissions')).toBeInTheDocument();
    expect(screen.getByText('Never')).toBeInTheDocument();
  });

  it.each([
    ['the error message', new Error('api down'), 'api down'],
    ['a generic fallback', {}, 'Failed to load token data.'],
  ])('shows %s when loading fails', async (_label, err, text) => {
    tokenApi.getPAT.mockRejectedValue(err);
    render(<PersonalAccessToken />);
    expect(await screen.findByText(text)).toBeInTheDocument();
  });
});

describe('PersonalAccessToken create', () => {
  it('lists scopes grouped by category, defaulting to General', async () => {
    await openCreate();
    expect(form().getByText('Profile')).toBeInTheDocument();
    expect(form().getByText('General')).toBeInTheDocument();
    expect(form().getByText('Read profile')).toBeInTheDocument();
  });

  it('requires a token name', async () => {
    await openCreate();
    fireEvent.click(form().getByRole('button', { name: 'Create Token' }));
    expect(await screen.findByText('Token name is required.')).toBeInTheDocument();
    expect(tokenApi.createPAT).not.toHaveBeenCalled();
  });

  it('sends a trimmed name with the chosen scope ceiling and reveals the token once', async () => {
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: '  ci  ' } });
    fireEvent.click(form().getByRole('checkbox', { name: /profile:read/ }));
    fireEvent.click(form().getByRole('checkbox', { name: /profile:write/ }));
    fireEvent.click(form().getByRole('checkbox', { name: /profile:write/ }));
    fireEvent.click(form().getByRole('button', { name: 'Create Token' }));

    expect(await screen.findByText('pat_secret')).toBeInTheDocument();
    expect(tokenApi.createPAT).toHaveBeenCalledWith({ name: 'ci', scope_ceiling: ['profile:read'] });
    expect(screen.getByText(/will not be shown again/)).toBeInTheDocument();
  });

  it('sends a null ceiling (full permissions) when no scope is ticked', async () => {
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: 'ci' } });
    fireEvent.click(form().getByRole('button', { name: 'Create Token' }));
    await waitFor(() => expect(tokenApi.createPAT).toHaveBeenCalledWith({ name: 'ci', scope_ceiling: null }));
  });

  it('reloads the token card when the reveal dialog is dismissed', async () => {
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: 'ci' } });
    fireEvent.click(form().getByRole('button', { name: 'Create Token' }));
    await screen.findByText('pat_secret');
    tokenApi.getPAT.mockResolvedValue({ data: { pat: { ...TOKEN, name: 'ci' } } });

    fireEvent.click(screen.getByRole('button', { name: 'Done' }));

    expect(await screen.findByText('ci')).toBeInTheDocument();
    expect(screen.queryByText('pat_secret')).not.toBeInTheDocument();
    expect(tokenApi.getPAT).toHaveBeenCalledTimes(2);
  });

  it('copies the token and resets the label after 2s', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: 'ci' } });
    fireEvent.click(form().getByRole('button', { name: 'Create Token' }));
    await screen.findByText('pat_secret');

    vi.useFakeTimers({ toFake: ['setTimeout'] });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    });
    expect(writeText).toHaveBeenCalledWith('pat_secret');
    expect(screen.getByRole('button', { name: 'Copied!' })).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument();
  });

  it('survives a clipboard failure', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    });
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: 'ci' } });
    fireEvent.click(form().getByRole('button', { name: 'Create Token' }));
    await screen.findByText('pat_secret');

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Copied!' })).not.toBeInTheDocument();
  });

  it.each([
    ['the error message', new Error('already have one'), 'already have one'],
    ['a generic fallback', {}, 'Failed to create token.'],
  ])('shows %s when creation fails and keeps the dialog open', async (_label, err, text) => {
    tokenApi.createPAT.mockRejectedValue(err);
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: 'ci' } });
    fireEvent.click(form().getByRole('button', { name: 'Create Token' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Create Personal Access Token' })).toBeInTheDocument();
  });

  it('resets the form on reopen and closes on Cancel', async () => {
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: 'leftover' } });
    fireEvent.click(form().getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Create Personal Access Token' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Create Token/ }));
    expect(screen.getByPlaceholderText('e.g. My deploy script')).toHaveValue('');
  });

  it('shows Creating… while pending', async () => {
    let release;
    tokenApi.createPAT.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: 'ci' } });
    fireEvent.click(form().getByRole('button', { name: 'Create Token' }));
    expect(await screen.findByRole('button', { name: 'Creating…' })).toBeDisabled();
    release({ data: { token: 'tok' } });
    await screen.findByText('tok');
  });
});

describe('PersonalAccessToken revoke', () => {
  it('revokes after confirmation and falls back to the empty state', async () => {
    await loadedWithToken();
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));
    expect(await screen.findByText('No active token')).toBeInTheDocument();
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('Revoke your PAT?'));
    expect(tokenApi.revokePAT).toHaveBeenCalledTimes(1);
  });

  it('does nothing when the confirmation is declined', async () => {
    window.confirm.mockReturnValue(false);
    await loadedWithToken();
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));
    expect(tokenApi.revokePAT).not.toHaveBeenCalled();
    expect(screen.getByText(TOKEN.name)).toBeInTheDocument();
  });

  it('shows Revoking… while pending', async () => {
    let release;
    tokenApi.revokePAT.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await loadedWithToken();
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));
    expect(await screen.findByRole('button', { name: 'Revoking…' })).toBeDisabled();
    release({});
    await screen.findByText('No active token');
  });

  it.each([
    ['the error message', new Error('cannot revoke'), 'cannot revoke'],
    ['a generic fallback', {}, 'Failed to revoke token.'],
  ])('keeps the token and shows %s when revoking fails', async (_label, err, text) => {
    tokenApi.revokePAT.mockRejectedValue(err);
    await loadedWithToken();
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText(TOKEN.name)).toBeInTheDocument();
  });
});
