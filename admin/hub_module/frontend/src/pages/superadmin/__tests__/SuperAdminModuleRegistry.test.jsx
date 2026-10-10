/**
 * Tests for the super-admin Module Registry: filtered/paginated table,
 * create + edit through the form modal, publish toggle and delete, with
 * success banners and API-error handling.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import SuperAdminModuleRegistry from '../SuperAdminModuleRegistry';
import { superAdminApi } from '../../../services/api';

vi.mock('@penguintechinc/react-libs', async () => ({
  FormModalBuilder: (await import('../../../test/formModalStub')).FormModalStub,
}));
vi.mock('../../../services/api', () => ({
  superAdminApi: {
    getAllModules: vi.fn(),
    createModule: vi.fn(),
    updateModule: vi.fn(),
    publishModule: vi.fn(),
    deleteModule: vi.fn(),
  },
}));

const MODULES = [
  {
    id: 'm1',
    name: 'karaoke',
    displayName: 'Karaoke',
    description: 'Sing',
    version: '2.1.0',
    author: 'Ann',
    category: 'entertainment',
    iconUrl: 'https://img/k.png',
    isCore: false,
    isPublished: true,
    avgRating: 4.5,
    reviewCount: 12,
    installCount: 340,
  },
  {
    id: 'm2',
    name: 'ping',
    displayName: 'Ping',
    description: 'Pong',
    version: '1.0.0',
    author: 'Waddles',
    category: 'utility',
    isCore: true,
    isPublished: false,
    avgRating: 0,
    reviewCount: 0,
    installCount: 5,
  },
];

function reply(modules = MODULES, pagination = { totalPages: 1 }) {
  return { data: { modules, pagination } };
}

const failure = (message) => ({ response: { data: { error: { message } } } });
const rowOf = (name) => within(screen.getByText(name).closest('tr'));

async function loaded() {
  const view = render(<SuperAdminModuleRegistry />);
  await screen.findByText('Karaoke');
  return view;
}

beforeEach(() => {
  vi.clearAllMocks();
  superAdminApi.getAllModules.mockResolvedValue(reply());
  for (const fn of ['createModule', 'updateModule', 'publishModule', 'deleteModule']) {
    superAdminApi[fn].mockResolvedValue({ data: { success: true } });
  }
  vi.stubGlobal('confirm', vi.fn(() => true));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SuperAdminModuleRegistry listing', () => {
  it('requests the first page with no filters', async () => {
    await loaded();
    expect(superAdminApi.getAllModules).toHaveBeenCalledWith({
      search: '',
      category: '',
      isPublished: '',
      page: 1,
      limit: 25,
    });
  });

  it('renders each module with identity, category, stats and publish state', async () => {
    await loaded();
    const karaoke = rowOf('Karaoke');
    expect(karaoke.getByText('karaoke')).toBeInTheDocument();
    expect(karaoke.getByAltText('Karaoke')).toHaveAttribute('src', 'https://img/k.png');
    expect(karaoke.getByText('entertainment')).toBeInTheDocument();
    expect(karaoke.getByText('4.5 (12)')).toBeInTheDocument();
    expect(karaoke.getByText('340 installs')).toBeInTheDocument();
    expect(karaoke.getByText('Published')).toBeInTheDocument();
    expect(karaoke.queryByText('Core Module')).not.toBeInTheDocument();

    const ping = rowOf('Ping');
    expect(ping.getByText('Core Module')).toBeInTheDocument();
    expect(ping.getByText('Unpublished')).toBeInTheDocument();
    expect(ping.queryByRole('img')).not.toBeInTheDocument();
  });

  it('refetches when searching and when changing the category or status filters', async () => {
    await loaded();
    fireEvent.change(screen.getByPlaceholderText('Search modules...'), { target: { value: 'kara' } });
    await waitFor(() =>
      expect(superAdminApi.getAllModules).toHaveBeenLastCalledWith(expect.objectContaining({ search: 'kara' })),
    );

    const [categorySelect, statusSelect] = screen.getAllByRole('combobox');
    fireEvent.change(categorySelect, { target: { value: 'music' } });
    await waitFor(() =>
      expect(superAdminApi.getAllModules).toHaveBeenLastCalledWith(expect.objectContaining({ category: 'music' })),
    );
    fireEvent.change(statusSelect, { target: { value: 'false' } });
    await waitFor(() =>
      expect(superAdminApi.getAllModules).toHaveBeenLastCalledWith(expect.objectContaining({ isPublished: 'false' })),
    );
  });

  it('paginates and disables Previous/Next at the ends', async () => {
    superAdminApi.getAllModules.mockResolvedValue(reply(MODULES, { totalPages: 2 }));
    await loaded();
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Page 2 of 2');
    expect(superAdminApi.getAllModules).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }));
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    await screen.findByText('Page 1 of 2');
  });

  it.each([
    ['the server message', failure('registry offline'), 'registry offline'],
    ['a generic fallback', new Error('x'), 'Failed to load modules'],
  ])('shows %s when loading fails, and the banner can be dismissed', async (_label, err, text) => {
    superAdminApi.getAllModules.mockRejectedValue(err);
    render(<SuperAdminModuleRegistry />);
    const banner = await screen.findByText(text);
    fireEvent.click(within(banner).getByRole('button', { name: '×' }));
    expect(screen.queryByText(text)).not.toBeInTheDocument();
  });
});

describe('SuperAdminModuleRegistry create', () => {
  async function openCreate() {
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: /Create Module/ }));
    return within(screen.getByRole('dialog', { name: 'Create Module' }));
  }

  it('opens with sensible defaults', async () => {
    const dialog = await openCreate();
    expect(dialog.getByLabelText('Version')).toHaveValue('1.0.0');
    expect(dialog.getByLabelText('Author')).toHaveValue('Waddles');
    expect(dialog.getByLabelText('Category')).toHaveValue('general');
    expect(dialog.getByLabelText('Mark as Core Module')).not.toBeChecked();
  });

  it('creates the module, announces success, closes and reloads', async () => {
    const dialog = await openCreate();
    fireEvent.change(dialog.getByLabelText('Module Name'), { target: { value: 'dice' } });
    fireEvent.change(dialog.getByLabelText('Display Name'), { target: { value: 'Dice' } });
    fireEvent.change(dialog.getByLabelText('Description'), { target: { value: 'Rolls dice' } });
    fireEvent.change(dialog.getByLabelText('Category'), { target: { value: 'games' } });
    fireEvent.click(dialog.getByLabelText('Mark as Core Module'));
    fireEvent.click(dialog.getByRole('button', { name: 'Create' }));

    expect(await screen.findByText(/Module created successfully/)).toBeInTheDocument();
    expect(superAdminApi.createModule).toHaveBeenCalledWith({
      name: 'dice',
      displayName: 'Dice',
      description: 'Rolls dice',
      version: '1.0.0',
      author: 'Waddles',
      category: 'games',
      iconUrl: '',
      isCore: true,
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(superAdminApi.getAllModules).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['the server message', failure('name taken'), 'name taken'],
    ['a generic fallback', new Error('x'), 'Failed to create module'],
  ])('shows %s on failure and keeps the modal open', async (_label, err, text) => {
    superAdminApi.createModule.mockRejectedValue(err);
    const dialog = await openCreate();
    fireEvent.click(dialog.getByRole('button', { name: 'Create' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Create Module' })).toBeInTheDocument();
  });

  it('closes from Cancel without calling the API', async () => {
    const dialog = await openCreate();
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(superAdminApi.createModule).not.toHaveBeenCalled();
  });
});

describe('SuperAdminModuleRegistry edit', () => {
  async function openEdit(name = 'Karaoke') {
    await loaded();
    fireEvent.click(rowOf(name).getByTitle('Edit'));
    return within(screen.getByRole('dialog', { name: 'Edit Module' }));
  }

  it('pre-fills the module being edited', async () => {
    const dialog = await openEdit();
    expect(dialog.getByLabelText('Module Name')).toHaveValue('karaoke');
    expect(dialog.getByLabelText('Display Name')).toHaveValue('Karaoke');
    expect(dialog.getByLabelText('Version')).toHaveValue('2.1.0');
    expect(dialog.getByLabelText('Author')).toHaveValue('Ann');
    expect(dialog.getByLabelText('Category')).toHaveValue('entertainment');
    expect(dialog.getByLabelText('Icon URL (optional)')).toHaveValue('https://img/k.png');
  });

  it('defaults a missing icon URL to blank and reflects core status', async () => {
    const dialog = await openEdit('Ping');
    expect(dialog.getByLabelText('Icon URL (optional)')).toHaveValue('');
    expect(dialog.getByLabelText('Mark as Core Module')).toBeChecked();
  });

  it('saves the edit by module id, announces success and reloads', async () => {
    const dialog = await openEdit();
    fireEvent.change(dialog.getByLabelText('Display Name'), { target: { value: 'Karaoke Pro' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Update' }));

    expect(await screen.findByText(/Module updated successfully/)).toBeInTheDocument();
    expect(superAdminApi.updateModule).toHaveBeenCalledWith(
      'm1',
      expect.objectContaining({ displayName: 'Karaoke Pro', name: 'karaoke', category: 'entertainment' }),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(superAdminApi.getAllModules).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['the server message', failure('conflict'), 'conflict'],
    ['a generic fallback', new Error('x'), 'Failed to update module'],
  ])('shows %s when the update fails', async (_label, err, text) => {
    superAdminApi.updateModule.mockRejectedValue(err);
    const dialog = await openEdit();
    fireEvent.click(dialog.getByRole('button', { name: 'Update' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Edit Module' })).toBeInTheDocument();
  });

  it('closes from Cancel', async () => {
    const dialog = await openEdit();
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('SuperAdminModuleRegistry publish and delete', () => {
  it('unpublishes a published module', async () => {
    await loaded();
    fireEvent.click(rowOf('Karaoke').getByTitle('Unpublish'));
    expect(await screen.findByText(/Module unpublished successfully/)).toBeInTheDocument();
    expect(superAdminApi.publishModule).toHaveBeenCalledWith('m1', false);
  });

  it('publishes an unpublished module', async () => {
    await loaded();
    fireEvent.click(rowOf('Ping').getByTitle('Publish'));
    expect(await screen.findByText(/Module published successfully/)).toBeInTheDocument();
    expect(superAdminApi.publishModule).toHaveBeenCalledWith('m2', true);
  });

  it.each([
    ['the server message', failure('locked'), 'locked'],
    ['a generic fallback', new Error('x'), 'Failed to update module'],
  ])('shows %s when publishing fails', async (_label, err, text) => {
    superAdminApi.publishModule.mockRejectedValue(err);
    await loaded();
    fireEvent.click(rowOf('Ping').getByTitle('Publish'));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('deletes after confirmation and reloads', async () => {
    await loaded();
    fireEvent.click(rowOf('Ping').getByTitle('Delete'));
    expect(await screen.findByText(/Module deleted successfully/)).toBeInTheDocument();
    expect(superAdminApi.deleteModule).toHaveBeenCalledWith('m2');
    expect(superAdminApi.getAllModules).toHaveBeenCalledTimes(2);
  });

  it('does nothing when deletion is declined', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    await loaded();
    fireEvent.click(rowOf('Ping').getByTitle('Delete'));
    expect(superAdminApi.deleteModule).not.toHaveBeenCalled();
  });

  it.each([
    ['the server message', failure('in use'), 'in use'],
    ['a generic fallback', new Error('x'), 'Failed to delete module'],
  ])('shows %s when deletion fails', async (_label, err, text) => {
    superAdminApi.deleteModule.mockRejectedValue(err);
    await loaded();
    fireEvent.click(rowOf('Ping').getByTitle('Delete'));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('lets the success banner be dismissed', async () => {
    await loaded();
    fireEvent.click(rowOf('Ping').getByTitle('Publish'));
    const banner = await screen.findByText(/Module published successfully/);
    fireEvent.click(within(banner).getByRole('button', { name: '×' }));
    expect(screen.queryByText(/Module published successfully/)).not.toBeInTheDocument();
  });
});
