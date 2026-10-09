/**
 * Tests for the community Security dashboard: configuration toggles with
 * dependent-control gating and save, the blocked-word list (add / Enter key /
 * delete with confirmation), warnings, moderation log, response-shape
 * fallbacks and per-request load failures.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminSecurity from '../AdminSecurity';
import { adminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  adminApi: {
    getSecurityConfig: vi.fn(),
    updateSecurityConfig: vi.fn(),
    getSecurityBlockedWords: vi.fn(),
    addSecurityBlockedWord: vi.fn(),
    deleteSecurityBlockedWord: vi.fn(),
    getSecurityWarnings: vi.fn(),
    getSecurityModerationLog: vi.fn(),
  },
}));

const CONFIG = {
  spamDetectionEnabled: true,
  autoDeleteSpam: false,
  contentFilterEnabled: true,
  use_default_profanity_list: false,
  filterCaseSensitive: false,
  filterAction: 'delete',
};

const WORDS = [
  { id: 1, word: 'badword', reason: 'rude', createdAt: '2026-02-01T10:00:00Z' },
  { id: 2, text: 'legacy', createdAt: '2026-02-02T10:00:00Z' },
];

const WARNINGS = [
  { username: 'alice', reason: 'spam', count: 3, createdAt: '2026-03-01T10:00:00Z' },
  { userId: 'u-9', createdAt: '2026-03-02T10:00:00Z' },
];

const LOG = [
  { action: 'timeout', username: 'bob', status: 'success', details: 'muted 10m', createdAt: '2026-03-03T10:00:00Z' },
  { userId: 'u-7', reason: 'because', createdAt: '2026-03-04T10:00:00Z' },
];

const wrap = (data) => ({ data: { data } });

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/security']}>
      <Routes>
        <Route path="/admin/:communityId/security" element={<AdminSecurity />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function openTab(name) {
  mount();
  await screen.findByText('Security Dashboard');
  fireEvent.click(screen.getByRole('button', { name }));
}

beforeEach(() => {
  vi.clearAllMocks();
  adminApi.getSecurityConfig.mockResolvedValue(wrap({ ...CONFIG }));
  adminApi.getSecurityBlockedWords.mockResolvedValue(wrap(WORDS));
  adminApi.getSecurityWarnings.mockResolvedValue(wrap(WARNINGS));
  adminApi.getSecurityModerationLog.mockResolvedValue(wrap(LOG));
  adminApi.updateSecurityConfig.mockResolvedValue({});
  adminApi.addSecurityBlockedWord.mockResolvedValue({});
  adminApi.deleteSecurityBlockedWord.mockResolvedValue({});
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('AdminSecurity loading', () => {
  it('shows a spinner, then fetches all four datasets for the community', async () => {
    const { container } = mount();
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();

    expect(await screen.findByText('Security Dashboard')).toBeInTheDocument();
    for (const fn of ['getSecurityConfig', 'getSecurityBlockedWords', 'getSecurityWarnings', 'getSecurityModerationLog']) {
      expect(adminApi[fn]).toHaveBeenCalledWith('7');
    }
  });

  it('degrades to empty data when individual requests fail', async () => {
    for (const fn of ['getSecurityConfig', 'getSecurityBlockedWords', 'getSecurityWarnings', 'getSecurityModerationLog']) {
      adminApi[fn].mockRejectedValue(new Error('down'));
    }
    mount();
    await screen.findByText('Security Dashboard');
    expect(screen.getByRole('checkbox', { name: /Enable Spam Detection/ })).not.toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: 'Blocked Words' }));
    expect(screen.getByText('No blocked words yet')).toBeInTheDocument();
  });

  it('accepts un-enveloped responses too (data instead of data.data)', async () => {
    adminApi.getSecurityConfig.mockResolvedValue({ data: { spamDetectionEnabled: true } });
    adminApi.getSecurityBlockedWords.mockResolvedValue({ data: [{ id: 5, word: 'plain', createdAt: '2026-01-01T00:00:00Z' }] });
    mount();
    await screen.findByText('Security Dashboard');
    expect(screen.getByRole('checkbox', { name: /Enable Spam Detection/ })).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Blocked Words' }));
    expect(screen.getByText('plain')).toBeInTheDocument();
  });
});

describe('AdminSecurity configuration tab', () => {
  async function mountConfig(config = CONFIG) {
    adminApi.getSecurityConfig.mockResolvedValue(wrap({ ...config }));
    mount();
    await screen.findByText('Security Dashboard');
  }
  const box = (name) => screen.getByRole('checkbox', { name: new RegExp(name) });

  it('reflects the saved configuration', async () => {
    await mountConfig();
    expect(box('Enable Spam Detection')).toBeChecked();
    expect(box('Auto-Delete Spam')).not.toBeChecked();
    expect(box('Enable Content Filter')).toBeChecked();
    expect(screen.getByRole('combobox')).toHaveValue('delete');
  });

  it('disables dependent spam controls until spam detection is on', async () => {
    await mountConfig({ ...CONFIG, spamDetectionEnabled: false });
    expect(box('Auto-Delete Spam')).toBeDisabled();
    fireEvent.click(box('Enable Spam Detection'));
    expect(box('Auto-Delete Spam')).toBeEnabled();
  });

  it('disables dependent filter controls until the content filter is on', async () => {
    await mountConfig({ ...CONFIG, contentFilterEnabled: false, filterAction: undefined });
    expect(box('Enable Built-in Profanity Filter')).toBeDisabled();
    expect(box('Case Sensitive')).toBeDisabled();
    expect(screen.getByRole('combobox')).toBeDisabled();
    expect(screen.getByRole('combobox')).toHaveValue('warn');
    fireEvent.click(box('Enable Content Filter'));
    expect(box('Case Sensitive')).toBeEnabled();
    expect(screen.getByRole('combobox')).toBeEnabled();
  });

  it('saves the edited configuration', async () => {
    await mountConfig();
    fireEvent.click(box('Auto-Delete Spam'));
    fireEvent.click(box('Enable Built-in Profanity Filter'));
    fireEvent.click(box('Case Sensitive'));
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'timeout' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    expect(await screen.findByText('Security configuration saved')).toBeInTheDocument();
    expect(adminApi.updateSecurityConfig).toHaveBeenCalledWith('7', {
      ...CONFIG,
      autoDeleteSpam: true,
      use_default_profanity_list: true,
      filterCaseSensitive: true,
      filterAction: 'timeout',
    });
  });

  it('shows the server error message when saving fails', async () => {
    adminApi.updateSecurityConfig.mockRejectedValue({ response: { data: { error: { message: 'read-only plan' } } } });
    await mountConfig();
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));
    expect(await screen.findByText('read-only plan')).toBeInTheDocument();
  });

  it('falls back to a generic message and lets it be dismissed', async () => {
    adminApi.updateSecurityConfig.mockRejectedValue(new Error('x'));
    await mountConfig();
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));
    const message = await screen.findByText('Failed to save configuration');
    fireEvent.click(within(message).getByRole('button', { name: '×' }));
    expect(screen.queryByText('Failed to save configuration')).not.toBeInTheDocument();
  });

  it('shows Saving... while the request is in flight', async () => {
    let release;
    adminApi.updateSecurityConfig.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await mountConfig();
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));
    expect(await screen.findByRole('button', { name: 'Saving...' })).toBeDisabled();
    release({});
    await screen.findByRole('button', { name: 'Save Configuration' });
  });
});

describe('AdminSecurity blocked words tab', () => {
  it('lists words with reason (default when missing) and date', async () => {
    await openTab('Blocked Words');
    expect(screen.getByText('Blocked Words (2)')).toBeInTheDocument();
    expect(screen.getByText('badword')).toBeInTheDocument();
    expect(screen.getByText('legacy')).toBeInTheDocument();
    expect(screen.getByText('rude')).toBeInTheDocument();
    expect(screen.getByText('User blocked')).toBeInTheDocument();
    expect(screen.getByText(new Date('2026-02-01T10:00:00Z').toLocaleDateString())).toBeInTheDocument();
  });

  it('refuses to add an empty word', async () => {
    await openTab('Blocked Words');
    fireEvent.click(screen.getByRole('button', { name: /Add to Block List/ }));
    expect(await screen.findByText('Please enter a word')).toBeInTheDocument();
    expect(adminApi.addSecurityBlockedWord).not.toHaveBeenCalled();
  });

  it('adds a trimmed word with the given reason, clears the inputs and refreshes', async () => {
    await openTab('Blocked Words');
    const word = screen.getByPlaceholderText('Enter word or phrase to block');
    const reason = screen.getByPlaceholderText('Reason for blocking (optional)');
    fireEvent.change(word, { target: { value: '  spoiler  ' } });
    fireEvent.change(reason, { target: { value: 'no spoilers' } });
    fireEvent.click(screen.getByRole('button', { name: /Add to Block List/ }));

    expect(await screen.findByText('Word added to block list')).toBeInTheDocument();
    expect(adminApi.addSecurityBlockedWord).toHaveBeenCalledWith('7', { word: 'spoiler', reason: 'no spoilers' });
    // The refresh remounts the tab, so re-query the (now cleared) inputs.
    expect(screen.getByPlaceholderText('Enter word or phrase to block')).toHaveValue('');
    expect(screen.getByPlaceholderText('Reason for blocking (optional)')).toHaveValue('');
    expect(adminApi.getSecurityBlockedWords).toHaveBeenCalledTimes(2);
  });

  it('defaults the reason and also adds when Enter is pressed', async () => {
    await openTab('Blocked Words');
    const word = screen.getByPlaceholderText('Enter word or phrase to block');
    fireEvent.change(word, { target: { value: 'oops' } });
    fireEvent.keyPress(word, { key: 'a', charCode: 97 });
    expect(adminApi.addSecurityBlockedWord).not.toHaveBeenCalled();
    fireEvent.keyPress(word, { key: 'Enter', code: 'Enter', charCode: 13 });

    await waitFor(() =>
      expect(adminApi.addSecurityBlockedWord).toHaveBeenCalledWith('7', { word: 'oops', reason: 'User blocked' }),
    );
  });

  it('reports a failed add', async () => {
    adminApi.addSecurityBlockedWord.mockRejectedValue(new Error('dup'));
    await openTab('Blocked Words');
    fireEvent.change(screen.getByPlaceholderText('Enter word or phrase to block'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: /Add to Block List/ }));
    expect(await screen.findByText('Failed to add word to block list')).toBeInTheDocument();
  });

  it('removes a word after confirmation and refreshes', async () => {
    await openTab('Blocked Words');
    fireEvent.click(within(screen.getByText('badword').closest('tr')).getByTitle('Remove word'));

    expect(await screen.findByText('Word removed from block list')).toBeInTheDocument();
    expect(adminApi.deleteSecurityBlockedWord).toHaveBeenCalledWith('7', 1);
    expect(adminApi.getSecurityBlockedWords).toHaveBeenCalledTimes(2);
  });

  it('keeps the word when the confirmation is declined', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    await openTab('Blocked Words');
    fireEvent.click(within(screen.getByText('badword').closest('tr')).getByTitle('Remove word'));
    expect(adminApi.deleteSecurityBlockedWord).not.toHaveBeenCalled();
  });

  it('reports a failed removal', async () => {
    adminApi.deleteSecurityBlockedWord.mockRejectedValue(new Error('x'));
    await openTab('Blocked Words');
    fireEvent.click(within(screen.getByText('badword').closest('tr')).getByTitle('Remove word'));
    expect(await screen.findByText('Failed to remove word')).toBeInTheDocument();
  });
});

describe('AdminSecurity warnings and log tabs', () => {
  it('lists warnings with user fallback, default reason and count', async () => {
    await openTab('Warnings');
    expect(screen.getByText('Recent Warnings (2)')).toBeInTheDocument();
    const alice = within(screen.getByText('alice').closest('tr'));
    expect(alice.getByText('spam')).toBeInTheDocument();
    expect(alice.getByText('3')).toBeInTheDocument();
    const anon = within(screen.getByText('u-9').closest('tr'));
    expect(anon.getByText('Content violation')).toBeInTheDocument();
    expect(anon.getByText('1')).toBeInTheDocument();
  });

  it('caps the warnings table at 20 rows but counts them all', async () => {
    adminApi.getSecurityWarnings.mockResolvedValue(
      wrap(Array.from({ length: 25 }, (_, i) => ({ username: `user${i}`, createdAt: '2026-03-01T10:00:00Z' }))),
    );
    await openTab('Warnings');
    expect(screen.getByText('Recent Warnings (25)')).toBeInTheDocument();
    expect(screen.getAllByRole('row')).toHaveLength(21);
  });

  it('shows the empty states', async () => {
    adminApi.getSecurityWarnings.mockResolvedValue(wrap([]));
    adminApi.getSecurityModerationLog.mockResolvedValue(wrap([]));
    mount();
    await screen.findByText('Security Dashboard');
    fireEvent.click(screen.getByRole('button', { name: 'Warnings' }));
    expect(screen.getByText('No warnings yet')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Moderation Log' }));
    expect(screen.getByText('No moderation actions yet')).toBeInTheDocument();
  });

  it('lists moderation actions with status styling and detail fallbacks', async () => {
    await openTab('Moderation Log');
    expect(screen.getByText('Moderation Log (2)')).toBeInTheDocument();
    expect(screen.getByText('timeout')).toBeInTheDocument();
    expect(screen.getByText('User: bob')).toBeInTheDocument();
    expect(screen.getByText('muted 10m')).toBeInTheDocument();
    expect(screen.getByText('success')).toHaveClass('text-emerald-300');

    expect(screen.getByText('Action')).toBeInTheDocument();
    expect(screen.getByText('User: u-7')).toBeInTheDocument();
    expect(screen.getByText('because')).toBeInTheDocument();
    expect(screen.getByText('pending')).toHaveClass('text-red-300');
  });
});
