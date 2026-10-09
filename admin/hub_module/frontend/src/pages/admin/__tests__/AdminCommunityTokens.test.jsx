/**
 * Tests for the Community Access Token admin page: token table + quota,
 * create modal (validation, grouped scopes, one-time token reveal, copy),
 * revoke, and error states.
 *
 * `tokenApi` is mocked with the UNWRAPPED payloads this page reads
 * (`res.tokens`, `res.token`); see AdminCommunityTokens.wire.test.jsx for the
 * contract against the real HTTP-backed client.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminCommunityTokens from '../AdminCommunityTokens';
import { tokenApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  tokenApi: { listCATs: vi.fn(), getCATScopes: vi.fn(), createCAT: vi.fn(), revokeCAT: vi.fn() },
}));

const TOKENS = [
  {
    id: 't1',
    name: 'Event bot',
    scopes: ['a:read', 'b:read', 'c:read', 'd:read', 'e:read'],
    created_by: 'alice',
    last_used_at: '2026-03-01T12:00:00Z',
    created_at: '2026-01-02T12:00:00Z',
  },
  { id: 't2', name: 'Welcomer', scopes: [], created_by: null, last_used_at: null, created_at: null },
  { id: 't3', name: 'Single', scopes: ['x:write'] },
];

const SCOPES = [
  { key: 'events:read', description: 'Read events', category: 'Events' },
  { key: 'events:write', description: 'Write events', category: 'Events' },
  { key: 'misc:ping' },
];

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/tokens']}>
      <Routes>
        <Route path="/admin/:communityId/tokens" element={<AdminCommunityTokens />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function openCreate() {
  mount();
  await screen.findByText('Event bot');
  fireEvent.click(screen.getByRole('button', { name: /New Token/ }));
  return screen.getByRole('heading', { name: 'Create Community Access Token' });
}

const modal = () =>
  within(screen.getByRole('heading', { name: 'Create Community Access Token' }).parentElement.querySelector('form'));

beforeEach(() => {
  vi.clearAllMocks();
  tokenApi.listCATs.mockResolvedValue({ tokens: TOKENS, quota: 10 });
  tokenApi.getCATScopes.mockResolvedValue({ scopes: SCOPES });
  tokenApi.createCAT.mockResolvedValue({ token: 'cat_secret_value' });
  tokenApi.revokeCAT.mockResolvedValue({});
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('AdminCommunityTokens listing', () => {
  it('shows a loading state, then loads tokens and scopes for the community', async () => {
    mount();
    expect(screen.getByText('Loading…')).toBeInTheDocument();
    expect(await screen.findByText('Event bot')).toBeInTheDocument();
    expect(tokenApi.listCATs).toHaveBeenCalledWith('7');
    expect(tokenApi.getCATScopes).toHaveBeenCalledWith('7');
  });

  it('renders token rows with scope badges (max 3 + overflow), creator and dates', async () => {
    mount();
    await screen.findByText('Event bot');

    const bot = within(screen.getByText('Event bot').closest('tr'));
    expect(bot.getByText('a:read')).toBeInTheDocument();
    expect(bot.getByText('c:read')).toBeInTheDocument();
    expect(bot.queryByText('d:read')).not.toBeInTheDocument();
    expect(bot.getByText('+2 more')).toBeInTheDocument();
    expect(bot.getByText('alice')).toBeInTheDocument();
    expect(bot.getByText(new Date('2026-03-01T12:00:00Z').toLocaleDateString())).toBeInTheDocument();
    expect(bot.getByText(new Date('2026-01-02T12:00:00Z').toLocaleDateString())).toBeInTheDocument();

    const welcomer = within(screen.getByText('Welcomer').closest('tr'));
    expect(welcomer.getByText('No scopes')).toBeInTheDocument();
    expect(welcomer.getByText('Never')).toBeInTheDocument();
    expect(welcomer.getAllByText('—')).toHaveLength(2);

    expect(within(screen.getByText('Single').closest('tr')).queryByText(/more/)).not.toBeInTheDocument();
  });

  it('shows the quota usage', async () => {
    mount();
    expect(await screen.findByText('/ 10 tokens used')).toBeInTheDocument();
    expect(screen.getByText('3')).toHaveClass('text-sky-100');
    expect(screen.getByRole('button', { name: /New Token/ })).toBeEnabled();
  });

  it('blocks creation and flags the quota in red once it is reached', async () => {
    tokenApi.listCATs.mockResolvedValue({ tokens: TOKENS, quota: 3 });
    mount();
    await screen.findByText('Event bot');
    expect(screen.getByText('3')).toHaveClass('text-red-400');
    const button = screen.getByRole('button', { name: /New Token/ });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'Token quota reached');
  });

  it('omits the quota line when the API gives none', async () => {
    tokenApi.listCATs.mockResolvedValue({ tokens: TOKENS });
    mount();
    await screen.findByText('Event bot');
    expect(screen.queryByText(/tokens used/)).not.toBeInTheDocument();
  });

  it('shows the empty state when there are no tokens', async () => {
    tokenApi.listCATs.mockResolvedValue({});
    mount();
    expect(await screen.findByText('No community tokens yet')).toBeInTheDocument();
  });

  it.each([
    ['the error message', new Error('backend down'), 'backend down'],
    ['a generic fallback', {}, 'Failed to load community tokens.'],
  ])('shows %s when loading fails', async (_label, err, text) => {
    tokenApi.listCATs.mockRejectedValue(err);
    mount();
    expect(await screen.findByText(text)).toBeInTheDocument();
  });
});

describe('AdminCommunityTokens create', () => {
  it('groups scopes by category, defaulting uncategorised ones to General', async () => {
    await openCreate();
    const form = modal();
    expect(form.getByText('Events')).toBeInTheDocument();
    expect(form.getByText('General')).toBeInTheDocument();
    expect(form.getByText('events:read')).toBeInTheDocument();
    expect(form.getByText('Read events')).toBeInTheDocument();
    expect(form.getByText('misc:ping')).toBeInTheDocument();
  });

  it('requires a name', async () => {
    await openCreate();
    fireEvent.click(modal().getByRole('button', { name: 'Create Token' }));
    expect(await screen.findByText('Token name is required.')).toBeInTheDocument();
    expect(tokenApi.createCAT).not.toHaveBeenCalled();
  });

  it('requires at least one scope', async () => {
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText(/Event bot, Welcome/), { target: { value: '  My bot ' } });
    fireEvent.click(modal().getByRole('button', { name: 'Create Token' }));
    expect(await screen.findByText('At least one scope must be selected.')).toBeInTheDocument();
    expect(tokenApi.createCAT).not.toHaveBeenCalled();
  });

  it('creates with a trimmed name and the chosen scopes, then reveals the token once', async () => {
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText(/Event bot, Welcome/), { target: { value: '  My bot ' } });
    const form = modal();
    fireEvent.click(form.getByRole('checkbox', { name: /events:read/ }));
    fireEvent.click(form.getByRole('checkbox', { name: /events:write/ }));
    fireEvent.click(form.getByRole('checkbox', { name: /events:write/ }));
    fireEvent.click(form.getByRole('checkbox', { name: /misc:ping/ }));
    fireEvent.click(form.getByRole('button', { name: 'Create Token' }));

    expect(await screen.findByText('cat_secret_value')).toBeInTheDocument();
    expect(tokenApi.createCAT).toHaveBeenCalledWith('7', { name: 'My bot', scopes: ['events:read', 'misc:ping'] });
    expect(screen.getByText(/It will not be shown again/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Create Community Access Token' })).not.toBeInTheDocument();
  });

  it('reloads the list and hides the token when the reveal dialog is dismissed', async () => {
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText(/Event bot, Welcome/), { target: { value: 'b' } });
    fireEvent.click(modal().getByRole('checkbox', { name: /misc:ping/ }));
    fireEvent.click(modal().getByRole('button', { name: 'Create Token' }));
    await screen.findByText('cat_secret_value');

    fireEvent.click(screen.getByRole('button', { name: 'Done' }));

    await waitFor(() => expect(screen.queryByText('cat_secret_value')).not.toBeInTheDocument());
    await waitFor(() => expect(tokenApi.listCATs).toHaveBeenCalledTimes(2));
  });

  it('copies the token to the clipboard and flips the label back after 2s', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText(/Event bot, Welcome/), { target: { value: 'b' } });
    fireEvent.click(modal().getByRole('checkbox', { name: /misc:ping/ }));
    fireEvent.click(modal().getByRole('button', { name: 'Create Token' }));
    await screen.findByText('cat_secret_value');

    vi.useFakeTimers({ toFake: ['setTimeout'] });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    });
    expect(writeText).toHaveBeenCalledWith('cat_secret_value');
    expect(screen.getByRole('button', { name: 'Copied!' })).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument();
  });

  it('survives a clipboard failure without showing Copied!', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    });
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText(/Event bot, Welcome/), { target: { value: 'b' } });
    fireEvent.click(modal().getByRole('checkbox', { name: /misc:ping/ }));
    fireEvent.click(modal().getByRole('button', { name: 'Create Token' }));
    await screen.findByText('cat_secret_value');

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));

    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Copied!' })).not.toBeInTheDocument();
  });

  it.each([
    ['the server message', new Error('quota exceeded'), 'quota exceeded'],
    ['a generic fallback', {}, 'Failed to create token.'],
  ])('shows %s when creation fails and keeps the modal open', async (_label, err, text) => {
    tokenApi.createCAT.mockRejectedValue(err);
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText(/Event bot, Welcome/), { target: { value: 'b' } });
    fireEvent.click(modal().getByRole('checkbox', { name: /misc:ping/ }));
    fireEvent.click(modal().getByRole('button', { name: 'Create Token' }));

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Create Community Access Token' })).toBeInTheDocument();
  });

  it('shows Creating… while pending and closes on Cancel', async () => {
    let release;
    tokenApi.createCAT.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText(/Event bot, Welcome/), { target: { value: 'b' } });
    fireEvent.click(modal().getByRole('checkbox', { name: /misc:ping/ }));
    fireEvent.click(modal().getByRole('button', { name: 'Create Token' }));
    expect(await screen.findByRole('button', { name: 'Creating…' })).toBeDisabled();
    release({ token: 'tok' });
    await screen.findByText('tok');
  });

  it('resets the form each time the modal is reopened', async () => {
    await openCreate();
    fireEvent.change(screen.getByPlaceholderText(/Event bot, Welcome/), { target: { value: 'leftover' } });
    fireEvent.click(modal().getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Create Community Access Token' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /New Token/ }));
    expect(screen.getByPlaceholderText(/Event bot, Welcome/)).toHaveValue('');
  });
});

describe('AdminCommunityTokens revoke', () => {
  it('revokes after confirmation and removes the row', async () => {
    mount();
    await screen.findByText('Welcomer');

    fireEvent.click(within(screen.getByText('Welcomer').closest('tr')).getByRole('button', { name: 'Revoke' }));

    await waitFor(() => expect(screen.queryByText('Welcomer')).not.toBeInTheDocument());
    expect(window.confirm).toHaveBeenCalledWith('Revoke token "Welcomer"?');
    expect(tokenApi.revokeCAT).toHaveBeenCalledWith('7', 't2');
    expect(screen.getByText('Event bot')).toBeInTheDocument();
  });

  it('does nothing when the confirmation is declined', async () => {
    window.confirm.mockReturnValue(false);
    mount();
    await screen.findByText('Welcomer');
    fireEvent.click(within(screen.getByText('Welcomer').closest('tr')).getByRole('button', { name: 'Revoke' }));
    expect(tokenApi.revokeCAT).not.toHaveBeenCalled();
    expect(screen.getByText('Welcomer')).toBeInTheDocument();
  });

  it('shows Revoking… while in flight', async () => {
    let release;
    tokenApi.revokeCAT.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    mount();
    await screen.findByText('Welcomer');
    fireEvent.click(within(screen.getByText('Welcomer').closest('tr')).getByRole('button', { name: 'Revoke' }));
    expect(await screen.findByRole('button', { name: 'Revoking…' })).toBeDisabled();
    release({});
    await waitFor(() => expect(screen.queryByText('Welcomer')).not.toBeInTheDocument());
  });

  it.each([
    ['the error message', new Error('cannot revoke'), 'cannot revoke'],
    ['a generic fallback', {}, 'Failed to revoke token.'],
  ])('keeps the row and shows %s when revoking fails', async (_label, err, text) => {
    tokenApi.revokeCAT.mockRejectedValue(err);
    mount();
    await screen.findByText('Welcomer');
    fireEvent.click(within(screen.getByText('Welcomer').closest('tr')).getByRole('button', { name: 'Revoke' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText('Welcomer')).toBeInTheDocument();
  });
});
