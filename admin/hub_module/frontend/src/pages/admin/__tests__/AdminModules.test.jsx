/**
 * Tests for the community Modules admin page: installed-module table (toggle,
 * core-module guard, JSON config editor, dedicated-config routing, uninstall),
 * the marketplace browser (search/filter/pagination/install/remove/pricing)
 * and the bundle-apps placeholder tab -- success and API-error paths.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminModules from '../AdminModules';
import { adminApi, marketplaceApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  adminApi: { getModules: vi.fn(), updateModuleConfig: vi.fn() },
  marketplaceApi: { browseModules: vi.fn(), installModule: vi.fn(), uninstallModule: vi.fn() },
}));

const INSTALLED = [
  {
    installationId: 'i1',
    moduleId: 'm1',
    name: 'karaoke',
    displayName: 'Karaoke',
    description: 'Sing along',
    category: 'entertainment',
    isEnabled: true,
    isCore: false,
    installedAt: '2026-02-03T12:00:00Z',
    config: { songs: 3 },
  },
  {
    installationId: 'i2',
    moduleId: 'm2',
    name: 'moderation',
    displayName: 'Moderation',
    description: 'Keep it clean',
    category: 'moderation',
    isEnabled: true,
    isCore: true,
    installedAt: null,
  },
  {
    installationId: 'i3',
    moduleId: 'm3',
    name: 'lfg',
    displayName: 'LFG',
    description: 'Find a group',
    category: undefined,
    isEnabled: false,
    isCore: false,
    installedAt: null,
  },
];

const CATALOG = [
  { id: 'c1', displayName: 'Free Thing', description: 'd1', category: 'utility', pricing: 'free', version: '1.0', author: 'ann', avgRating: 4.5, reviewCount: 2, installCount: 10, isInstalled: false },
  { id: 'c2', displayName: 'Once Thing', description: 'd2', category: 'games', pricing: 'one-time', price: 5, iconUrl: 'https://img/x.png', isInstalled: false },
  { id: 'c3', displayName: 'Sub Thing', description: 'd3', category: 'ai', pricing: 'subscription', price: 7, isInstalled: true, isCore: false },
  { id: 'c4', displayName: 'Seat Thing', description: 'd4', category: 'music', pricing: 'per-seat', price: 2, isInstalled: true, isCore: true },
  { id: 'c5', displayName: 'Odd Thing', description: 'd5', category: 'mystery', pricing: 'barter', isInstalled: false },
];

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/modules']}>
      <Routes>
        <Route path="/admin/:communityId/modules" element={<AdminModules />} />
        <Route path="/admin/:communityId/modules/:slug/config" element={<div>dedicated config page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const apiError = (message) => ({ response: { data: { error: { message } } } });
const row = (name) => screen.getByText(name).closest('tr');

async function openMarketplace() {
  mount();
  await screen.findByText('Karaoke');
  fireEvent.click(screen.getByRole('button', { name: 'Browse Marketplace' }));
  await screen.findByText('Free Thing');
}

beforeEach(() => {
  vi.clearAllMocks();
  adminApi.getModules.mockResolvedValue({ data: { modules: INSTALLED } });
  adminApi.updateModuleConfig.mockResolvedValue({ data: { success: true } });
  marketplaceApi.browseModules.mockResolvedValue({ data: { modules: CATALOG, pagination: { totalPages: 3 } } });
  marketplaceApi.installModule.mockResolvedValue({ data: { success: true } });
  marketplaceApi.uninstallModule.mockResolvedValue({ data: { success: true } });
  vi.stubGlobal('confirm', vi.fn(() => true));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminModules installed tab', () => {
  it('lists installed modules with badges, status, install date and actions', async () => {
    mount();
    expect(await screen.findByText('Karaoke')).toBeInTheDocument();
    expect(adminApi.getModules).toHaveBeenCalledWith('7');

    const karaoke = within(row('Karaoke'));
    expect(karaoke.getByText('entertainment')).toBeInTheDocument();
    expect(karaoke.getByRole('button', { name: 'Enabled' })).toBeInTheDocument();
    expect(karaoke.getByText(new Date('2026-02-03T12:00:00Z').toLocaleDateString())).toBeInTheDocument();
    expect(karaoke.getByRole('button', { name: 'Uninstall' })).toBeInTheDocument();

    const core = within(row('Moderation'));
    expect(core.getByText('Core')).toBeInTheDocument();
    expect(core.queryByRole('button', { name: 'Uninstall' })).not.toBeInTheDocument();
    expect(core.getByText('N/A')).toBeInTheDocument();

    const lfg = within(row('LFG'));
    expect(lfg.getByText('General')).toBeInTheDocument();
    expect(lfg.getByRole('button', { name: 'Disabled' })).toBeInTheDocument();
  });

  it('shows the empty state and jumps to the marketplace from it', async () => {
    adminApi.getModules.mockResolvedValue({ data: {} });
    mount();
    expect(await screen.findByText('No Modules Installed')).toBeInTheDocument();

    const buttons = screen.getAllByRole('button', { name: 'Browse Marketplace' });
    fireEvent.click(buttons[buttons.length - 1]);

    expect(await screen.findByText('Free Thing')).toBeInTheDocument();
  });

  it('shows the server error when modules fail to load, and a fallback otherwise', async () => {
    adminApi.getModules.mockRejectedValueOnce(apiError('db is down'));
    const first = mount();
    expect(await screen.findByText('db is down')).toBeInTheDocument();
    first.unmount();

    adminApi.getModules.mockRejectedValueOnce(new Error('network'));
    mount();
    expect(await screen.findByText('Failed to load modules')).toBeInTheDocument();
  });

  it('lets the user dismiss the error banner', async () => {
    adminApi.getModules.mockRejectedValueOnce(apiError('db is down'));
    mount();
    const banner = (await screen.findByText('db is down')).parentElement.parentElement;
    fireEvent.click(within(banner).getByRole('button'));
    expect(screen.queryByText('db is down')).not.toBeInTheDocument();
  });

  it('disables a non-core module immediately and reloads the list', async () => {
    mount();
    await screen.findByText('Karaoke');

    fireEvent.click(within(row('Karaoke')).getByRole('button', { name: 'Enabled' }));

    expect(await screen.findByText('Module disabled successfully')).toBeInTheDocument();
    expect(adminApi.updateModuleConfig).toHaveBeenCalledWith('7', 'm1', { isEnabled: false });
    expect(adminApi.getModules).toHaveBeenCalledTimes(2);
  });

  it('enables a disabled module and lets the message be dismissed', async () => {
    mount();
    await screen.findByText('LFG');

    fireEvent.click(within(row('LFG')).getByRole('button', { name: 'Disabled' }));

    const message = await screen.findByText('Module enabled successfully');
    expect(adminApi.updateModuleConfig).toHaveBeenCalledWith('7', 'm3', { isEnabled: true });
    fireEvent.click(within(message.parentElement.parentElement).getByRole('button'));
    expect(screen.queryByText('Module enabled successfully')).not.toBeInTheDocument();
  });

  it('reports a failed toggle', async () => {
    adminApi.updateModuleConfig.mockRejectedValue(apiError('not allowed'));
    mount();
    await screen.findByText('Karaoke');
    fireEvent.click(within(row('Karaoke')).getByRole('button', { name: 'Enabled' }));
    expect(await screen.findByText('not allowed')).toBeInTheDocument();
  });

  it('falls back to a generic message for a failed toggle without a server message', async () => {
    adminApi.updateModuleConfig.mockRejectedValue(new Error('x'));
    mount();
    await screen.findByText('Karaoke');
    fireEvent.click(within(row('Karaoke')).getByRole('button', { name: 'Enabled' }));
    expect(await screen.findByText('Failed to toggle module')).toBeInTheDocument();
  });

  it('asks for confirmation before disabling a core module, and Cancel makes no change', async () => {
    mount();
    await screen.findByText('Moderation');

    fireEvent.click(within(row('Moderation')).getByRole('button', { name: 'Enabled' }));

    expect(screen.getByText('Disable Core Module?')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByText('Disable Core Module?')).not.toBeInTheDocument();
    expect(adminApi.updateModuleConfig).not.toHaveBeenCalled();
  });

  it('disables the core module once the warning is confirmed', async () => {
    mount();
    await screen.findByText('Moderation');
    fireEvent.click(within(row('Moderation')).getByRole('button', { name: 'Enabled' }));

    fireEvent.click(screen.getByRole('button', { name: 'Disable Anyway' }));

    await waitFor(() => expect(adminApi.updateModuleConfig).toHaveBeenCalledWith('7', 'm2', { isEnabled: false }));
    await waitFor(() => expect(screen.queryByText('Disable Core Module?')).not.toBeInTheDocument());
  });

  it('routes modules that have a dedicated page to it', async () => {
    mount();
    await screen.findByText('LFG');

    fireEvent.click(within(row('LFG')).getByRole('button', { name: 'Configure' }));

    expect(await screen.findByText('dedicated config page')).toBeInTheDocument();
  });

  it('matches a dedicated config page by display name as well as module name', async () => {
    adminApi.getModules.mockResolvedValue({
      data: { modules: [{ ...INSTALLED[0], name: 'x', displayName: 'Looking-For-Group' }] },
    });
    mount();
    await screen.findByText('Looking-For-Group');

    fireEvent.click(screen.getByRole('button', { name: 'Configure' }));

    expect(await screen.findByText('dedicated config page')).toBeInTheDocument();
  });

  describe('JSON config editor', () => {
    async function openEditor(name = 'Karaoke') {
      mount();
      await screen.findByText(name);
      fireEvent.click(within(row(name)).getByRole('button', { name: 'Configure' }));
      return screen.getByRole('textbox');
    }

    it('opens pre-filled with the pretty-printed config and saves valid JSON', async () => {
      const editor = await openEditor();
      expect(screen.getByText('Configure: Karaoke')).toBeInTheDocument();
      expect(editor).toHaveValue(JSON.stringify({ songs: 3 }, null, 2));

      fireEvent.change(editor, { target: { value: '{"songs": 9}' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

      expect(await screen.findByText('Module configuration saved successfully')).toBeInTheDocument();
      expect(adminApi.updateModuleConfig).toHaveBeenCalledWith('7', 'm1', { config: { songs: 9 } });
      expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
      expect(adminApi.getModules).toHaveBeenCalledTimes(2);
    });

    it('starts from an empty object when the module has no config', async () => {
      const editor = await openEditor('Moderation');
      expect(editor).toHaveValue('{}');
    });

    it('rejects invalid JSON without calling the API', async () => {
      const editor = await openEditor();
      fireEvent.change(editor, { target: { value: '{nope' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

      expect(await screen.findByText('Invalid JSON format')).toBeInTheDocument();
      expect(adminApi.updateModuleConfig).not.toHaveBeenCalled();
    });

    it('keeps the editor open and shows the server error when saving fails', async () => {
      adminApi.updateModuleConfig.mockRejectedValue(apiError('config rejected'));
      await openEditor();
      fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

      expect(await screen.findByText('config rejected')).toBeInTheDocument();
      expect(screen.getByRole('textbox')).toBeInTheDocument();
    });

    it('falls back to a generic save error', async () => {
      adminApi.updateModuleConfig.mockRejectedValue(new Error('x'));
      await openEditor();
      fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));
      expect(await screen.findByText('Failed to save configuration')).toBeInTheDocument();
    });

    it('closes from Cancel and from the header X', async () => {
      await openEditor();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(screen.queryByRole('textbox')).not.toBeInTheDocument();

      fireEvent.click(within(row('Karaoke')).getByRole('button', { name: 'Configure' }));
      const header = screen.getByText('Configure: Karaoke').parentElement;
      fireEvent.click(within(header).getByRole('button'));
      expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    });
  });

  describe('uninstall', () => {
    it('uninstalls after confirmation and reloads both lists', async () => {
      mount();
      await screen.findByText('Karaoke');

      fireEvent.click(within(row('Karaoke')).getByRole('button', { name: 'Uninstall' }));

      expect(await screen.findByText('Module uninstalled successfully')).toBeInTheDocument();
      expect(marketplaceApi.uninstallModule).toHaveBeenCalledWith('7', 'm1');
      expect(adminApi.getModules).toHaveBeenCalledTimes(2);
    });

    it('does nothing when the confirmation is declined', async () => {
      vi.stubGlobal('confirm', vi.fn(() => false));
      mount();
      await screen.findByText('Karaoke');

      fireEvent.click(within(row('Karaoke')).getByRole('button', { name: 'Uninstall' }));

      expect(marketplaceApi.uninstallModule).not.toHaveBeenCalled();
    });

    it('shows an error when uninstalling fails', async () => {
      marketplaceApi.uninstallModule.mockRejectedValue(apiError('in use'));
      mount();
      await screen.findByText('Karaoke');
      fireEvent.click(within(row('Karaoke')).getByRole('button', { name: 'Uninstall' }));
      expect(await screen.findByText('in use')).toBeInTheDocument();
    });
  });
});

describe('AdminModules marketplace tab', () => {
  it('requests the first page with default filters and renders each module card', async () => {
    await openMarketplace();

    expect(marketplaceApi.browseModules).toHaveBeenCalledWith('7', { search: '', category: '', page: 1, limit: 12 });
    expect(screen.getByText('Free Thing')).toBeInTheDocument();
    expect(screen.getByText('v1.0')).toBeInTheDocument();
    expect(screen.getByText('by ann')).toBeInTheDocument();
    expect(screen.getByAltText('Once Thing')).toHaveAttribute('src', 'https://img/x.png');
    expect(screen.getByText('Core Module')).toBeInTheDocument();
  });

  it('shows the right pricing badge per pricing model', async () => {
    await openMarketplace();
    expect(screen.getByText('Free')).toBeInTheDocument();
    expect(screen.getByText('$5')).toBeInTheDocument();
    expect(screen.getByText('$7/mo')).toBeInTheDocument();
    expect(screen.getByText('$2/seat/mo')).toBeInTheDocument();
    expect(screen.queryByText(/barter/)).not.toBeInTheDocument();
  });

  it('refetches from page 1 when searching or changing category', async () => {
    await openMarketplace();

    fireEvent.change(screen.getByPlaceholderText('Search modules...'), { target: { value: 'karaoke' } });
    await waitFor(() =>
      expect(marketplaceApi.browseModules).toHaveBeenLastCalledWith('7', { search: 'karaoke', category: '', page: 1, limit: 12 }),
    );

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'music' } });
    await waitFor(() =>
      expect(marketplaceApi.browseModules).toHaveBeenLastCalledWith('7', { search: 'karaoke', category: 'music', page: 1, limit: 12 }),
    );
  });

  it('paginates with Previous/Next and disables them at the ends', async () => {
    await openMarketplace();
    expect(screen.getByText('Page 1 of 3')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Page 2 of 3');
    expect(marketplaceApi.browseModules).toHaveBeenLastCalledWith('7', expect.objectContaining({ page: 2 }));

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Page 3 of 3');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    await screen.findByText('Page 2 of 3');
  });

  it('hides pagination for a single page of results', async () => {
    marketplaceApi.browseModules.mockResolvedValue({ data: { modules: CATALOG, pagination: { totalPages: 1 } } });
    await openMarketplace();
    expect(screen.queryByText(/Page 1 of/)).not.toBeInTheDocument();
  });

  it('installs a module and refreshes catalog and installed lists', async () => {
    await openMarketplace();
    const card = screen.getByText('Free Thing').closest('div.bg-navy-800');

    fireEvent.click(within(card).getByRole('button', { name: 'Install' }));

    expect(await screen.findByText('Module installed successfully')).toBeInTheDocument();
    expect(marketplaceApi.installModule).toHaveBeenCalledWith('7', 'c1');
    await waitFor(() => expect(marketplaceApi.browseModules).toHaveBeenCalledTimes(2));
    expect(adminApi.getModules).toHaveBeenCalledTimes(2);
  });

  it('shows an error when install fails', async () => {
    marketplaceApi.installModule.mockRejectedValue(apiError('license required'));
    await openMarketplace();
    fireEvent.click(within(screen.getByText('Free Thing').closest('div.bg-navy-800')).getByRole('button', { name: 'Install' }));
    expect(await screen.findByText('license required')).toBeInTheDocument();
  });

  it('shows Installed with a Remove button for non-core modules only', async () => {
    await openMarketplace();
    const sub = within(screen.getByText('Sub Thing').closest('div.bg-navy-800'));
    expect(sub.getByText('Installed')).toBeInTheDocument();
    fireEvent.click(sub.getByRole('button', { name: 'Remove' }));

    expect(await screen.findByText('Module uninstalled successfully')).toBeInTheDocument();
    expect(marketplaceApi.uninstallModule).toHaveBeenCalledWith('7', 'c3');

    const core = within(screen.getByText('Seat Thing').closest('div.bg-navy-800'));
    expect(core.getByText('Installed')).toBeInTheDocument();
    expect(core.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument();
  });

  it('shows an error when the marketplace fails to load', async () => {
    marketplaceApi.browseModules.mockRejectedValue(new Error('x'));
    mount();
    await screen.findByText('Karaoke');
    fireEvent.click(screen.getByRole('button', { name: 'Browse Marketplace' }));
    expect(await screen.findByText('Failed to load marketplace')).toBeInTheDocument();
  });
});

describe('AdminModules bundle apps tab', () => {
  it('shows the coming-soon placeholder', async () => {
    mount();
    await screen.findByText('Karaoke');
    fireEvent.click(screen.getByTestId('admin-modules-bundle-apps-tab'));
    expect(screen.getByTestId('community-bundle-apps-followup')).toHaveTextContent('Bundle Apps (Coming Soon)');
  });

  it('returns to the installed list from another tab', async () => {
    mount();
    await screen.findByText('Karaoke');
    fireEvent.click(screen.getByTestId('admin-modules-bundle-apps-tab'));
    fireEvent.click(screen.getByRole('button', { name: 'Installed Modules' }));
    expect(await screen.findByText('Karaoke')).toBeInTheDocument();
  });
});
