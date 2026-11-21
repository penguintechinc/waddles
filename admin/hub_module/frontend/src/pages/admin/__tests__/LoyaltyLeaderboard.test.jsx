/**
 * Tests for the MVP core-currency loyalty leaderboard admin page (gh-317)
 * — render of `LeaderboardEntryDTO` rows, balance adjust dialog, wipe
 * confirmation flow, and error toasts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import LoyaltyLeaderboard from '../LoyaltyLeaderboard';
import { adminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  adminApi: {
    getLoyaltyLeaderboard: vi.fn(),
    getLoyaltyStats: vi.fn(),
    getLoyaltyConfig: vi.fn(),
    adjustLoyaltyBalance: vi.fn(),
    wipeLoyaltyCurrency: vi.fn(),
  },
}));

// `@penguintechinc/react-libs`'s built dist is a directory-import ESM
// package vitest/node can't resolve directly — same workaround as
// `src/layouts/__tests__/DashboardLayout.test.jsx`. Stub FormModalBuilder
// as a plain <form> that submits a `{ [field.name]: value }` object built
// from its own `fields` prop, close enough to the real component's
// contract for these interaction tests.
vi.mock('@penguintechinc/react-libs', () => ({
  FormModalBuilder: ({ isOpen, title, description, fields, onSubmit, onClose, submitButtonText, cancelButtonText }) => {
    if (!isOpen) return null;
    return (
      <div data-testid="form-modal">
        <h2>{title}</h2>
        {description && <p>{description}</p>}
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            const formEl = e.target;
            const data = {};
            fields.forEach((f) => {
              const el = formEl.elements.namedItem(f.name);
              data[f.name] = el ? el.value : undefined;
            });
            try {
              await onSubmit(data);
            } catch {
              // component under test owns error display; nothing to do here
            }
          }}
        >
          {fields.map((f) => {
            if (f.type === 'select') {
              return (
                <select key={f.name} name={f.name} defaultValue={f.defaultValue}>
                  {f.options.map((opt) => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              );
            }
            if (f.type === 'textarea') {
              return <textarea key={f.name} name={f.name} placeholder={f.placeholder} />;
            }
            return <input key={f.name} name={f.name} type={f.type} placeholder={f.placeholder} />;
          })}
          <button type="submit">{submitButtonText}</button>
          <button type="button" onClick={onClose}>{cancelButtonText}</button>
        </form>
      </div>
    );
  },
}));

const ENTRIES = [
  { platform: 'twitch', platform_user_id: 'user_1', balance: 500, user_uuid: '11111111-1111-4111-8111-111111111111' },
  { platform: 'discord', platform_user_id: 'user_2', balance: 200, user_uuid: '22222222-2222-4222-8222-222222222222' },
];

const STATS = { community_id: 42, total_users: 2, total_currency: 700, average_balance: 350 };
const CONFIG = { currency_name: 'Points', currency_symbol: '$', enabled: true };

function mount(communityId = '42') {
  return render(
    <MemoryRouter initialEntries={[`/admin/${communityId}/loyalty/leaderboard`]}>
      <Routes>
        <Route path="/admin/:communityId/loyalty/leaderboard" element={<LoyaltyLeaderboard />} />
      </Routes>
    </MemoryRouter>,
  );
}

function mockLoadSuccess() {
  adminApi.getLoyaltyLeaderboard.mockResolvedValue({ data: { data: { entries: ENTRIES } } });
  adminApi.getLoyaltyStats.mockResolvedValue({ data: { data: STATS } });
  adminApi.getLoyaltyConfig.mockResolvedValue({ data: { data: CONFIG } });
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('LoyaltyLeaderboard', () => {
  it('renders leaderboard rows and stats from the envelope response', async () => {
    mockLoadSuccess();

    mount();

    expect(await screen.findByTestId('loyalty-row-twitch-user_1')).toHaveTextContent('user_1');
    expect(screen.getByTestId('loyalty-row-discord-user_2')).toHaveTextContent('user_2');
    expect(screen.getByText('$ 700')).toBeInTheDocument();
    expect(adminApi.getLoyaltyLeaderboard).toHaveBeenCalledWith('42', { limit: 25 });
  });

  it('shows an empty state when there are no entries', async () => {
    adminApi.getLoyaltyLeaderboard.mockResolvedValue({ data: { data: { entries: [] } } });
    adminApi.getLoyaltyStats.mockResolvedValue({ data: { data: { ...STATS, total_users: 0, total_currency: 0, average_balance: 0 } } });
    adminApi.getLoyaltyConfig.mockResolvedValue({ data: { data: CONFIG } });

    mount();

    expect(await screen.findByText('No users found')).toBeInTheDocument();
  });

  it('filters rows client-side by platform user id search', async () => {
    mockLoadSuccess();

    mount();
    await screen.findByTestId('loyalty-row-twitch-user_1');

    await act(async () => {
      fireEvent.change(screen.getByTestId('loyalty-search'), { target: { value: 'user_2' } });
    });

    expect(screen.queryByTestId('loyalty-row-twitch-user_1')).not.toBeInTheDocument();
    expect(screen.getByTestId('loyalty-row-discord-user_2')).toBeInTheDocument();
  });

  it('adjusts a balance with a positive delta and refreshes', async () => {
    mockLoadSuccess();
    adminApi.adjustLoyaltyBalance.mockResolvedValue({
      data: { data: { community_id: 42, platform: 'twitch', platform_user_id: 'user_1', balance: 600, lifetime_earned: 600, lifetime_spent: 0 } },
    });

    mount();
    await screen.findByTestId('loyalty-row-twitch-user_1');

    await act(async () => {
      fireEvent.click(screen.getByTestId('loyalty-adjust-twitch-user_1'));
    });

    const amountInput = await screen.findByPlaceholderText('Enter amount...');
    await act(async () => {
      fireEvent.change(amountInput, { target: { value: '100' } });
      fireEvent.click(screen.getByText('Apply Adjustment'));
    });

    await waitFor(() =>
      expect(adminApi.adjustLoyaltyBalance).toHaveBeenCalledWith('42', '11111111-1111-4111-8111-111111111111', {
        platform: 'twitch',
        delta: 100,
        note: undefined,
      }),
    );
    expect(await screen.findByTestId('loyalty-leaderboard-feedback')).toHaveTextContent(
      'increased successfully',
    );
  });

  it('shows the server error message when a balance adjustment is rejected', async () => {
    mockLoadSuccess();
    adminApi.adjustLoyaltyBalance.mockRejectedValue({
      response: { status: 409, data: { error: { message: 'insufficient points' } } },
    });

    mount();
    await screen.findByTestId('loyalty-row-twitch-user_1');

    await act(async () => {
      fireEvent.click(screen.getByTestId('loyalty-adjust-twitch-user_1'));
    });

    const amountInput = await screen.findByPlaceholderText('Enter amount...');
    await act(async () => {
      fireEvent.change(amountInput, { target: { value: '50' } });
      const actionSelect = screen.getByDisplayValue('Add - Increase balance');
      fireEvent.change(actionSelect, { target: { value: 'remove' } });
      fireEvent.click(screen.getByText('Apply Adjustment'));
    });

    expect(await screen.findByTestId('loyalty-leaderboard-feedback')).toHaveTextContent(
      'insufficient points',
    );
  });

  it('wipes all balances after typed confirmation and shows the affected count', async () => {
    mockLoadSuccess();
    adminApi.wipeLoyaltyCurrency.mockResolvedValue({ data: { data: { affected: 2 } } });

    mount();
    await screen.findByTestId('loyalty-row-twitch-user_1');

    await act(async () => {
      fireEvent.click(screen.getByTestId('loyalty-wipe-open'));
    });

    const confirmInput = await screen.findByPlaceholderText('WIPE ALL');
    await act(async () => {
      fireEvent.change(confirmInput, { target: { value: 'WIPE ALL' } });
      fireEvent.click(screen.getByText('Confirm Wipe'));
    });

    await waitFor(() => expect(adminApi.wipeLoyaltyCurrency).toHaveBeenCalledWith('42'));
    expect(await screen.findByTestId('loyalty-leaderboard-feedback')).toHaveTextContent(
      'Wiped 2 balances to zero',
    );
  });

  it('rejects wipe confirmation when the typed text does not match', async () => {
    mockLoadSuccess();

    mount();
    await screen.findByTestId('loyalty-row-twitch-user_1');

    await act(async () => {
      fireEvent.click(screen.getByTestId('loyalty-wipe-open'));
    });

    const confirmInput = await screen.findByPlaceholderText('WIPE ALL');
    await act(async () => {
      fireEvent.change(confirmInput, { target: { value: 'nope' } });
      fireEvent.click(screen.getByText('Confirm Wipe'));
    });

    expect(await screen.findByTestId('loyalty-leaderboard-feedback')).toHaveTextContent(
      'Please type "WIPE ALL" to confirm',
    );
    expect(adminApi.wipeLoyaltyCurrency).not.toHaveBeenCalled();
  });

  it('shows an error state message when the initial load fails', async () => {
    adminApi.getLoyaltyLeaderboard.mockRejectedValue({ response: { status: 500 } });
    adminApi.getLoyaltyStats.mockRejectedValue({ response: { status: 500 } });
    adminApi.getLoyaltyConfig.mockRejectedValue({ response: { status: 500 } });

    mount();

    expect(await screen.findByTestId('loyalty-leaderboard-feedback')).toHaveTextContent(
      'Failed to load loyalty data',
    );
  });
});
