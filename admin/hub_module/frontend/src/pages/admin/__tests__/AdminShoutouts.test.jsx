/**
 * Tests for the Shoutout Settings page: config load (defaults / 404 / error),
 * the settings tab (permissions, auto-shoutout, widget, browser-source URL),
 * saving, the auto-shoutout creator list (add / Enter / remove) and the
 * filterable history tab.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminShoutouts from '../AdminShoutouts';
import { adminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  adminApi: {
    getShoutoutConfig: vi.fn(),
    updateShoutoutConfig: vi.fn(),
    getShoutoutCreators: vi.fn(),
    addShoutoutCreator: vi.fn(),
    removeShoutoutCreator: vi.fn(),
    getShoutoutHistory: vi.fn(),
  },
}));

const CONFIG = {
  soEnabled: true,
  soPermission: 'vip',
  vsoEnabled: true,
  vsoPermission: 'subscriber',
  autoShoutoutMode: 'list_only',
  triggerFirstMessage: true,
  triggerRaidHost: false,
  widgetPosition: 'top-left',
  widgetDurationSeconds: 45,
  cooldownMinutes: 15,
};

const DEFAULTS = {
  soEnabled: true,
  soPermission: 'mod',
  vsoEnabled: true,
  vsoPermission: 'mod',
  autoShoutoutMode: 'disabled',
  triggerFirstMessage: false,
  triggerRaidHost: true,
  widgetPosition: 'bottom-right',
  widgetDurationSeconds: 30,
  cooldownMinutes: 60,
};

const CREATORS = [
  { id: 1, platform: 'twitch', platformUsername: 'ninja', createdAt: '2026-02-03T12:00:00Z' },
  { id: 2, platform: 'youtube', platformUsername: 'pewds', createdAt: '2026-02-04T12:00:00Z' },
];

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/shoutouts']}>
      <Routes>
        <Route path="/admin/:communityId/shoutouts" element={<AdminShoutouts />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function loaded(config = CONFIG) {
  adminApi.getShoutoutConfig.mockResolvedValue({ data: { success: true, config } });
  mount();
  await screen.findByText('Shoutout Settings');
}

const autoMode = () => screen.getAllByRole('combobox')[0];
const widgetPosition = () => screen.getAllByRole('combobox')[1];
const tab = (name) => fireEvent.click(screen.getByRole('button', { name }));
const failure = (message) => ({ response: { data: { error: { message } } } });

beforeEach(() => {
  vi.clearAllMocks();
  adminApi.getShoutoutCreators.mockResolvedValue({ data: { success: true, creators: CREATORS } });
  adminApi.updateShoutoutConfig.mockResolvedValue({ data: { success: true } });
  adminApi.addShoutoutCreator.mockResolvedValue({ data: { success: true } });
  adminApi.removeShoutoutCreator.mockResolvedValue({ data: { success: true } });
  adminApi.getShoutoutHistory.mockResolvedValue({ data: { history: [] } });
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AdminShoutouts loading', () => {
  it('shows a spinner then fetches config and creators for the community', async () => {
    adminApi.getShoutoutConfig.mockResolvedValue({ data: { success: true, config: CONFIG } });
    const { container } = mount();
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();
    await screen.findByText('Shoutout Settings');
    expect(adminApi.getShoutoutConfig).toHaveBeenCalledWith('7');
    expect(adminApi.getShoutoutCreators).toHaveBeenCalledWith('7');
  });

  it('falls back to defaults when the API returns no config', async () => {
    adminApi.getShoutoutConfig.mockResolvedValue({ data: { success: true } });
    mount();
    await screen.findByText('Shoutout Settings');
    expect(autoMode()).toHaveValue(DEFAULTS.autoShoutoutMode);
  });

  it('shows a failure panel when the API reports success:false', async () => {
    adminApi.getShoutoutConfig.mockResolvedValue({ data: { success: false } });
    mount();
    expect(await screen.findByText('Failed to load configuration')).toBeInTheDocument();
  });

  it('uses defaults silently when the config does not exist yet (404)', async () => {
    adminApi.getShoutoutConfig.mockRejectedValue({ response: { status: 404 } });
    mount();
    await screen.findByText('Shoutout Settings');
    expect(screen.queryByText('Failed to load shoutout configuration')).not.toBeInTheDocument();
  });

  it('uses defaults but warns on any other load error', async () => {
    adminApi.getShoutoutConfig.mockRejectedValue({ response: { status: 500 } });
    mount();
    expect(await screen.findByText('Failed to load shoutout configuration')).toBeInTheDocument();
  });

  it('tolerates a failing creators request', async () => {
    adminApi.getShoutoutCreators.mockRejectedValue(new Error('x'));
    await loaded();
    expect(screen.getByRole('button', { name: 'Creator List (0)' })).toBeInTheDocument();
  });
});

describe('AdminShoutouts settings tab', () => {
  it('hides permission choices when !so / !vso are switched off', async () => {
    await loaded();
    expect(screen.getByText('Who can use !so')).toBeInTheDocument();
    expect(screen.getByText('Who can use !vso')).toBeInTheDocument();
    const [soToggle, vsoToggle] = screen.getAllByRole('checkbox');
    fireEvent.click(soToggle);
    expect(screen.queryByText('Who can use !so')).not.toBeInTheDocument();
    fireEvent.click(vsoToggle);
    expect(screen.queryByText('Who can use !vso')).not.toBeInTheDocument();
  });

  it('reflects and changes the permission levels', async () => {
    await loaded();
    const so = screen.getAllByRole('radio').filter((r) => r.name === 'soPermission');
    const vso = screen.getAllByRole('radio').filter((r) => r.name === 'vsoPermission');
    expect(so.find((r) => r.checked)).toHaveAttribute('value', 'vip');
    expect(vso.find((r) => r.checked)).toHaveAttribute('value', 'subscriber');

    fireEvent.click(so.find((r) => r.value === 'everyone'));
    fireEvent.click(vso.find((r) => r.value === 'admin_only'));
    expect(so.find((r) => r.checked)).toHaveAttribute('value', 'everyone');
    expect(vso.find((r) => r.checked)).toHaveAttribute('value', 'admin_only');
  });

  it('shows the trigger options only while auto-shoutout is enabled', async () => {
    await loaded();
    expect(screen.getByText('First Message Trigger')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /First Message Trigger/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /Raid\/Host Trigger/ })).not.toBeChecked();

    fireEvent.click(screen.getByRole('checkbox', { name: /Raid\/Host Trigger/ }));
    expect(screen.getByRole('checkbox', { name: /Raid\/Host Trigger/ })).toBeChecked();

    fireEvent.change(autoMode(), { target: { value: 'disabled' } });
    expect(screen.queryByText('First Message Trigger')).not.toBeInTheDocument();
  });

  it('moves the widget preview with the position setting', async () => {
    await loaded();
    const preview = screen.getByText('Video Widget');
    expect(preview).toHaveClass('top-2', 'left-2');
    const select = widgetPosition();
    for (const [value, classes] of [
      ['top-right', ['top-2', 'right-2']],
      ['bottom-left', ['bottom-2', 'left-2']],
      ['bottom-right', ['bottom-2', 'right-2']],
    ]) {
      fireEvent.change(select, { target: { value } });
      expect(screen.getByText('Video Widget')).toHaveClass(...classes);
    }
  });

  it('parses duration and cooldown, falling back to defaults for junk', async () => {
    await loaded();
    const [duration, cooldown] = screen.getAllByRole('spinbutton');
    expect(duration).toHaveValue(45);
    expect(cooldown).toHaveValue(15);
    fireEvent.change(duration, { target: { value: '90' } });
    fireEvent.change(cooldown, { target: { value: '120' } });
    expect(duration).toHaveValue(90);
    expect(cooldown).toHaveValue(120);
    fireEvent.change(duration, { target: { value: '' } });
    fireEvent.change(cooldown, { target: { value: '' } });
    expect(duration).toHaveValue(30);
    expect(cooldown).toHaveValue(60);
  });

  it('builds the browser source URL from origin, community and position, and copies it', async () => {
    const writeText = vi.fn();
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    await loaded();
    const expected = `${window.location.origin}/overlay/video-shoutout?community=7&position=top-left`;
    expect(screen.getByDisplayValue(expected)).toHaveAttribute('readonly');

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith(expected);
    expect(await screen.findByText('URL copied to clipboard')).toBeInTheDocument();
  });
});

describe('AdminShoutouts saving', () => {
  it('saves the edited configuration', async () => {
    await loaded();
    fireEvent.click(screen.getAllByRole('radio').find((r) => r.name === 'soPermission' && r.value === 'everyone'));
    fireEvent.change(widgetPosition(), { target: { value: 'bottom-left' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

    expect(await screen.findByText('Shoutout configuration saved')).toBeInTheDocument();
    expect(adminApi.updateShoutoutConfig).toHaveBeenCalledWith(
      '7',
      { ...CONFIG, soPermission: 'everyone', widgetPosition: 'bottom-left' },
    );
  });

  it.each([
    ['the server message', failure('read only plan'), 'read only plan'],
    ['a generic fallback', new Error('x'), 'Failed to save'],
  ])('shows %s when saving fails, dismissible', async (_label, err, text) => {
    adminApi.updateShoutoutConfig.mockRejectedValue(err);
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    const message = await screen.findByText(text);
    fireEvent.click(within(message).getByRole('button', { name: '×' }));
    expect(screen.queryByText(text)).not.toBeInTheDocument();
  });

  it('shows Saving... while in flight', async () => {
    let release;
    adminApi.updateShoutoutConfig.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    expect(await screen.findByRole('button', { name: 'Saving...' })).toBeDisabled();
    release({ data: {} });
    await screen.findByText('Shoutout configuration saved');
  });
});

describe('AdminShoutouts creators tab', () => {
  it('lists creators with platform badge, name and date', async () => {
    await loaded();
    tab('Creator List (2)');
    const ninja = within(screen.getByText('ninja').closest('div.rounded-lg'));
    expect(ninja.getByText('twitch')).toHaveClass('text-purple-400');
    expect(ninja.getByText(`Added ${new Date('2026-02-03T12:00:00Z').toLocaleDateString()}`)).toBeInTheDocument();
    expect(within(screen.getByText('pewds').closest('div.rounded-lg')).getByText('youtube')).toHaveClass('text-red-400');
    expect(screen.getByText('Auto-Shoutout Creators (2)')).toBeInTheDocument();
  });

  it('shows the empty state', async () => {
    adminApi.getShoutoutCreators.mockResolvedValue({ data: { success: true } });
    await loaded();
    tab('Creator List (0)');
    expect(screen.getByText(/No creators added yet/)).toBeInTheDocument();
  });

  it('adds a creator for the chosen platform, clears the field and refreshes', async () => {
    await loaded();
    tab('Creator List (2)');
    expect(screen.getByRole('button', { name: 'Add Creator' })).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText('Username'), { target: { value: 'shroud' } });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'youtube' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Creator' }));

    expect(await screen.findByText('Creator added successfully')).toBeInTheDocument();
    expect(adminApi.addShoutoutCreator).toHaveBeenCalledWith('7', { platform: 'youtube', username: 'shroud' });
    expect(screen.getByPlaceholderText('Username')).toHaveValue('');
    expect(screen.getByRole('combobox')).toHaveValue('twitch');
    await waitFor(() => expect(adminApi.getShoutoutCreators).toHaveBeenCalledTimes(2));
  });

  it('adds on Enter, ignoring other keys and blank names', async () => {
    await loaded();
    tab('Creator List (2)');
    const input = screen.getByPlaceholderText('Username');
    fireEvent.keyPress(input, { key: 'Enter', charCode: 13 });
    expect(adminApi.addShoutoutCreator).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'shroud' } });
    fireEvent.keyPress(input, { key: 'a', charCode: 97 });
    expect(adminApi.addShoutoutCreator).not.toHaveBeenCalled();
    fireEvent.keyPress(input, { key: 'Enter', charCode: 13 });
    await waitFor(() =>
      expect(adminApi.addShoutoutCreator).toHaveBeenCalledWith('7', { platform: 'twitch', username: 'shroud' }),
    );
  });

  it.each([
    ['the server message', failure('already listed'), 'already listed'],
    ['a generic fallback', new Error('x'), 'Failed to add'],
  ])('shows %s when adding fails', async (_label, err, text) => {
    adminApi.addShoutoutCreator.mockRejectedValue(err);
    await loaded();
    tab('Creator List (2)');
    fireEvent.change(screen.getByPlaceholderText('Username'), { target: { value: 'dup' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Creator' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('shows Adding... while in flight', async () => {
    let release;
    adminApi.addShoutoutCreator.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await loaded();
    tab('Creator List (2)');
    fireEvent.change(screen.getByPlaceholderText('Username'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Creator' }));
    expect(await screen.findByRole('button', { name: 'Adding...' })).toBeDisabled();
    release({ data: {} });
    await screen.findByText('Creator added successfully');
  });

  it('removes a creator and refreshes', async () => {
    await loaded();
    tab('Creator List (2)');
    fireEvent.click(within(screen.getByText('ninja').closest('div.rounded-lg')).getByRole('button', { name: 'Remove' }));
    expect(await screen.findByText('Creator removed')).toBeInTheDocument();
    expect(adminApi.removeShoutoutCreator).toHaveBeenCalledWith('7', 1);
    await waitFor(() => expect(adminApi.getShoutoutCreators).toHaveBeenCalledTimes(2));
  });

  it('shows an error when removal fails', async () => {
    adminApi.removeShoutoutCreator.mockRejectedValue(new Error('x'));
    await loaded();
    tab('Creator List (2)');
    fireEvent.click(within(screen.getByText('ninja').closest('div.rounded-lg')).getByRole('button', { name: 'Remove' }));
    expect(await screen.findByText('Failed to remove creator')).toBeInTheDocument();
  });
});

describe('AdminShoutouts history tab', () => {
  const HISTORY = [
    {
      id: 1,
      targetUsername: 'ninja',
      shoutoutType: 'text',
      triggeredBy: 'alice',
      platform: 'twitch',
      createdAt: '2026-03-01T12:00:00Z',
    },
    {
      id: 2,
      target_username: 'pewds',
      type: 'auto',
      triggered_by: 'bot',
      platform: 'youtube',
      created_at: '2026-03-02T12:00:00Z',
    },
    { id: 3 },
  ];

  it('fetches history when the tab opens and renders camelCase and snake_case rows', async () => {
    adminApi.getShoutoutHistory.mockResolvedValue({ data: { history: HISTORY } });
    await loaded();
    tab('History');
    expect(await screen.findByText('ninja')).toBeInTheDocument();
    expect(adminApi.getShoutoutHistory).toHaveBeenCalledWith('7', {});

    const first = within(screen.getByText('ninja').closest('tr'));
    expect(first.getByText('text')).toBeInTheDocument();
    expect(first.getByText('alice')).toBeInTheDocument();
    expect(first.getByText(new Date('2026-03-01T12:00:00Z').toLocaleString())).toBeInTheDocument();

    const second = within(screen.getByText('pewds').closest('tr'));
    expect(second.getByText('auto')).toBeInTheDocument();
    expect(second.getByText('bot')).toBeInTheDocument();

    const bare = screen.getAllByRole('row')[3];
    expect(within(bare).getAllByText('—')).toHaveLength(5);
  });

  it('accepts the alternate "shoutouts" response key', async () => {
    adminApi.getShoutoutHistory.mockResolvedValue({ data: { shoutouts: [HISTORY[0]] } });
    await loaded();
    tab('History');
    expect(await screen.findByText('ninja')).toBeInTheDocument();
  });

  it('shows the empty state', async () => {
    await loaded();
    tab('History');
    expect(await screen.findByText('No shoutout history found.')).toBeInTheDocument();
  });

  it('shows an error when history fails to load', async () => {
    adminApi.getShoutoutHistory.mockRejectedValue(new Error('x'));
    await loaded();
    tab('History');
    expect(await screen.findByText('Failed to load shoutout history.')).toBeInTheDocument();
  });

  it('applies the date and type filters', async () => {
    await loaded();
    tab('History');
    await screen.findByText('No shoutout history found.');
    const [from, to] = document.querySelectorAll('input[type="date"]');
    fireEvent.change(from, { target: { value: '2026-03-01' } });
    fireEvent.change(to, { target: { value: '2026-03-31' } });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'auto' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply Filters' }));

    await waitFor(() =>
      expect(adminApi.getShoutoutHistory).toHaveBeenLastCalledWith('7', {
        from: '2026-03-01',
        to: '2026-03-31',
        type: 'auto',
      }),
    );
  });

  it('Clear resets the filter inputs', async () => {
    await loaded();
    tab('History');
    await screen.findByText('No shoutout history found.');
    const [from] = document.querySelectorAll('input[type="date"]');
    fireEvent.change(from, { target: { value: '2026-03-01' } });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'auto' } });
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));

    expect(from).toHaveValue('');
    expect(screen.getByRole('combobox')).toHaveValue('');
    await waitFor(() => expect(adminApi.getShoutoutHistory).toHaveBeenCalledTimes(2));
  });

  // Regression: `Clear` used to reset the filter state and then call
  // `setTimeout(fetchHistory, 0)`, but that `fetchHistory` closure was
  // captured before the reset, so the refetch still sent the OLD filters.
  it('Clear refetches the history without the old filters', async () => {
    await loaded();
    tab('History');
    await screen.findByText('No shoutout history found.');
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'auto' } });
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    await waitFor(() => expect(adminApi.getShoutoutHistory).toHaveBeenCalledTimes(2));
    expect(adminApi.getShoutoutHistory).toHaveBeenLastCalledWith('7', {});
  });
});
