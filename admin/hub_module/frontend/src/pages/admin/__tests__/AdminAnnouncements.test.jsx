/**
 * Tests for the community Announcements admin page: listing (pinned vs
 * recent, badges, truncation), status filter + pagination, create / edit via
 * the announcement modal, publish / pin / unpin / delete actions, the
 * broadcast dialog hand-off, and per-action error banners.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminAnnouncements from '../AdminAnnouncements';
import { adminApi } from '../../../services/api';

vi.mock('@penguintechinc/react-libs', async () => ({
  FormModalBuilder: (await import('../../../test/formModalStub')).FormModalStub,
}));
vi.mock('../../../services/api', () => ({
  adminApi: {
    getAnnouncements: vi.fn(),
    createAnnouncement: vi.fn(),
    updateAnnouncement: vi.fn(),
    deleteAnnouncement: vi.fn(),
    publishAnnouncement: vi.fn(),
    pinAnnouncement: vi.fn(),
    unpinAnnouncement: vi.fn(),
    broadcastAnnouncement: vi.fn(),
    getBroadcastStatus: vi.fn(),
  },
}));

const NOW = new Date().toISOString();

const LONG = 'x'.repeat(200);

const ANNOUNCEMENTS = () => [
  {
    id: 1,
    title: 'Pinned news',
    content: 'Short pinned body',
    announcement_type: 'important',
    status: 'published',
    is_pinned: true,
    author_name: 'Alice',
    created_at: NOW,
  },
  {
    id: 2,
    title: 'Draft idea',
    content: LONG,
    announcement_type: 'event',
    status: 'draft',
    is_pinned: false,
    created_at: '2020-01-15T10:00:00Z',
  },
  {
    id: 3,
    title: 'Old update',
    content: 'Archived body',
    announcement_type: 'mystery',
    status: 'archived',
    is_pinned: false,
    author_name: 'Bob',
    created_at: NOW,
  },
];

function reply(data = ANNOUNCEMENTS(), extra = {}) {
  return { data: { data, pagination: { totalPages: 1 }, connectedPlatforms: ['discord'], ...extra } };
}

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/announcements']}>
      <Routes>
        <Route path="/admin/:communityId/announcements" element={<AdminAnnouncements />} />
      </Routes>
    </MemoryRouter>,
  );
}

const card = (title) => within(screen.getByText(title).closest('div.rounded-lg'));
const failure = (message) => ({ response: { data: { error: { message } } } });

async function loaded() {
  mount();
  await screen.findByText('Pinned news');
}

beforeEach(() => {
  vi.clearAllMocks();
  adminApi.getAnnouncements.mockResolvedValue(reply());
  for (const fn of ['createAnnouncement', 'updateAnnouncement', 'deleteAnnouncement', 'publishAnnouncement', 'pinAnnouncement', 'unpinAnnouncement', 'broadcastAnnouncement']) {
    adminApi[fn].mockResolvedValue({ data: { success: true } });
  }
  adminApi.getBroadcastStatus.mockResolvedValue({ data: { success: true, broadcasts: [] } });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AdminAnnouncements listing', () => {
  it('shows a spinner, then fetches page 1 with the default filter', async () => {
    const { container } = mount();
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();
    await screen.findByText('Pinned news');
    expect(adminApi.getAnnouncements).toHaveBeenCalledWith('7', { page: 1, status: undefined, limit: 20 });
  });

  it('splits pinned from recent announcements', async () => {
    await loaded();
    expect(screen.getByText('Pinned Announcements')).toBeInTheDocument();
    expect(screen.getByText('Recent Announcements')).toBeInTheDocument();
    expect(card('Pinned news').getByTitle('Pinned')).toBeInTheDocument();
    expect(card('Pinned news').getByRole('button', { name: 'Unpin' })).toBeInTheDocument();
    expect(card('Draft idea').getByRole('button', { name: 'Pin' })).toBeInTheDocument();
  });

  it('omits the "Recent" heading when nothing is pinned', async () => {
    adminApi.getAnnouncements.mockResolvedValue(reply(ANNOUNCEMENTS().slice(1)));
    mount();
    await screen.findByText('Draft idea');
    expect(screen.queryByText('Recent Announcements')).not.toBeInTheDocument();
    expect(screen.queryByText('Pinned Announcements')).not.toBeInTheDocument();
  });

  it('renders type and status badges, author fallback and truncated previews', async () => {
    await loaded();
    const pinned = card('Pinned news');
    expect(pinned.getByText('important')).toBeInTheDocument();
    expect(pinned.getByText('Published')).toBeInTheDocument();
    expect(pinned.getByText('by Alice')).toBeInTheDocument();
    expect(pinned.getByText('Short pinned body')).toBeInTheDocument();

    const draft = card('Draft idea');
    expect(draft.getByText('Draft')).toBeInTheDocument();
    expect(draft.getByText('by Unknown')).toBeInTheDocument();
    expect(draft.getByText(`${'x'.repeat(150)}...`)).toBeInTheDocument();
    expect(draft.getByText(/Jan 15, 2020/)).toBeInTheDocument();

    expect(card('Old update').getByText('Archived')).toBeInTheDocument();
  });

  it('offers Publish only for drafts', async () => {
    await loaded();
    expect(card('Draft idea').getByRole('button', { name: 'Publish' })).toBeInTheDocument();
    expect(card('Pinned news').queryByRole('button', { name: 'Publish' })).not.toBeInTheDocument();
  });

  it('shows the empty state', async () => {
    adminApi.getAnnouncements.mockResolvedValue(reply([]));
    mount();
    expect(await screen.findByText(/No announcements yet/)).toBeInTheDocument();
  });

  it.each([
    ['the server message', failure('forbidden'), 'forbidden'],
    ['a generic fallback', new Error('x'), 'Failed to load announcements'],
  ])('shows %s when loading fails, and the banner can be dismissed', async (_label, err, text) => {
    adminApi.getAnnouncements.mockRejectedValue(err);
    mount();
    const banner = (await screen.findByText(text)).parentElement;
    fireEvent.click(within(banner).getByRole('button', { name: '×' }));
    expect(screen.queryByText(text)).not.toBeInTheDocument();
  });

  it('treats a missing pagination block as a single page', async () => {
    adminApi.getAnnouncements.mockResolvedValue({ data: { data: ANNOUNCEMENTS() } });
    await loaded();
    expect(screen.queryByText(/Page 1 of/)).not.toBeInTheDocument();
  });
});

describe('AdminAnnouncements filters and paging', () => {
  it.each([
    ['Published', 'published'],
    ['Draft', 'draft'],
    ['Archived', 'archived'],
  ])('filters by %s', async (label, status) => {
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: label, exact: true }));
    await waitFor(() =>
      expect(adminApi.getAnnouncements).toHaveBeenLastCalledWith('7', { page: 1, status, limit: 20 }),
    );
  });

  it('returns to unfiltered with All', async () => {
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Draft', exact: true }));
    await waitFor(() => expect(adminApi.getAnnouncements).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    await waitFor(() =>
      expect(adminApi.getAnnouncements).toHaveBeenLastCalledWith('7', { page: 1, status: undefined, limit: 20 }),
    );
  });

  it('paginates and resets to page 1 when the filter changes', async () => {
    adminApi.getAnnouncements.mockResolvedValue(reply(ANNOUNCEMENTS(), { pagination: { totalPages: 3 } }));
    await loaded();
    expect(screen.getByText('Page 1 of 3')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Page 2 of 3');
    expect(adminApi.getAnnouncements).toHaveBeenLastCalledWith('7', { page: 2, status: undefined, limit: 20 });

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Page 3 of 3');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    await screen.findByText('Page 2 of 3');

    fireEvent.click(screen.getByRole('button', { name: 'Published' }));
    await screen.findByText('Page 1 of 3');
  });
});

describe('AdminAnnouncements create and edit', () => {
  it('creates an announcement through the modal and refreshes', async () => {
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: /Create Announcement/ }));
    const dialog = within(screen.getByRole('dialog', { name: 'Create Announcement' }));
    fireEvent.change(dialog.getByLabelText('Title'), { target: { value: 'Hello' } });
    fireEvent.change(dialog.getByLabelText('Content'), { target: { value: 'World' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Create Announcement' }));

    await waitFor(() => expect(adminApi.createAnnouncement).toHaveBeenCalledTimes(1));
    expect(adminApi.createAnnouncement).toHaveBeenCalledWith(
      '7',
      expect.objectContaining({ title: 'Hello', content: 'World', status: 'draft', selected_platforms: [] }),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(adminApi.getAnnouncements).toHaveBeenCalledTimes(2));
  });

  it('prefills and updates an existing announcement', async () => {
    await loaded();
    fireEvent.click(card('Draft idea').getByRole('button', { name: 'Edit' }));
    const dialog = within(screen.getByRole('dialog', { name: 'Edit Announcement' }));
    expect(dialog.getByLabelText('Title')).toHaveValue('Draft idea');
    fireEvent.change(dialog.getByLabelText('Title'), { target: { value: 'Draft idea v2' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalledTimes(1));
    expect(adminApi.updateAnnouncement).toHaveBeenCalledWith('7', 2, expect.objectContaining({ title: 'Draft idea v2' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('starts a fresh create form after an edit was abandoned', async () => {
    await loaded();
    fireEvent.click(card('Draft idea').getByRole('button', { name: 'Edit' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: /Create Announcement/ }));
    expect(screen.getByRole('dialog', { name: 'Create Announcement' })).toBeInTheDocument();
  });

  it.each([
    ['the server message', failure('title taken'), 'title taken'],
    ['a generic fallback', new Error('x'), 'Failed to save announcement'],
  ])('shows %s when saving fails', async (_label, err, text) => {
    adminApi.createAnnouncement.mockRejectedValue(err);
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: /Create Announcement/ }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Create Announcement' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });
});

describe('AdminAnnouncements row actions', () => {
  it('publishes a draft and refreshes', async () => {
    await loaded();
    fireEvent.click(card('Draft idea').getByRole('button', { name: 'Publish' }));
    await waitFor(() => expect(adminApi.publishAnnouncement).toHaveBeenCalledWith('7', 2));
    await waitFor(() => expect(adminApi.getAnnouncements).toHaveBeenCalledTimes(2));
  });

  it('pins and unpins', async () => {
    await loaded();
    fireEvent.click(card('Draft idea').getByRole('button', { name: 'Pin' }));
    await waitFor(() => expect(adminApi.pinAnnouncement).toHaveBeenCalledWith('7', 2));
    fireEvent.click(card('Pinned news').getByRole('button', { name: 'Unpin' }));
    await waitFor(() => expect(adminApi.unpinAnnouncement).toHaveBeenCalledWith('7', 1));
  });

  it('deletes after confirmation', async () => {
    await loaded();
    fireEvent.click(card('Old update').getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(adminApi.deleteAnnouncement).toHaveBeenCalledWith('7', 3));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('cannot be undone'));
  });

  it('keeps the announcement when deletion is declined', async () => {
    window.confirm.mockReturnValue(false);
    await loaded();
    fireEvent.click(card('Old update').getByRole('button', { name: 'Delete' }));
    expect(adminApi.deleteAnnouncement).not.toHaveBeenCalled();
  });

  it.each([
    ['publishAnnouncement', 'Draft idea', 'Publish', 'Failed to publish announcement'],
    ['pinAnnouncement', 'Draft idea', 'Pin', 'Failed to pin announcement'],
    ['unpinAnnouncement', 'Pinned news', 'Unpin', 'Failed to unpin announcement'],
    ['deleteAnnouncement', 'Old update', 'Delete', 'Failed to delete announcement'],
  ])('%s failure falls back to a generic message', async (fn, title, button, text) => {
    adminApi[fn].mockRejectedValue(new Error('x'));
    await loaded();
    fireEvent.click(card(title).getByRole('button', { name: button }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('shows the server message for a failed action', async () => {
    adminApi.pinAnnouncement.mockRejectedValue(failure('pin limit reached'));
    await loaded();
    fireEvent.click(card('Draft idea').getByRole('button', { name: 'Pin' }));
    expect(await screen.findByText('pin limit reached')).toBeInTheDocument();
  });

  it('shows a progress label while an action is in flight', async () => {
    let release;
    adminApi.publishAnnouncement.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await loaded();
    fireEvent.click(card('Draft idea').getByRole('button', { name: 'Publish' }));
    expect(await screen.findByText('publishing...')).toBeInTheDocument();
    expect(card('Draft idea').getByRole('button', { name: 'Edit' })).toBeDisabled();
    release({ data: {} });
    await waitFor(() => expect(screen.queryByText('publishing...')).not.toBeInTheDocument());
  });
});

describe('AdminAnnouncements broadcast', () => {
  it('opens the broadcast dialog for the chosen announcement', async () => {
    await loaded();
    fireEvent.click(card('Pinned news').getByRole('button', { name: 'Broadcast' }));
    expect(await screen.findByText('Broadcast Status')).toBeInTheDocument();
    expect(adminApi.getBroadcastStatus).toHaveBeenCalledWith('7', 1);
  });

  it('closes the dialog from its Close button', async () => {
    await loaded();
    fireEvent.click(card('Pinned news').getByRole('button', { name: 'Broadcast' }));
    await screen.findByText('Broadcast Status');
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByText('Broadcast Status')).not.toBeInTheDocument();
  });

  it('sends the broadcast request for the picked platforms and closes the dialog', async () => {
    adminApi.getBroadcastStatus.mockResolvedValue({
      data: { success: true, broadcasts: [{ platform: 'twitch', serverName: 'Prior', status: 'success' }] },
    });
    await loaded();
    fireEvent.click(card('Pinned news').getByRole('button', { name: 'Broadcast' }));
    await screen.findByText('Prior');
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Broadcast Now' }));

    await waitFor(() => expect(adminApi.broadcastAnnouncement).toHaveBeenCalledTimes(1));
    expect(adminApi.broadcastAnnouncement.mock.calls[0].slice(0, 2)).toEqual(['7', 1]);
    await waitFor(() => expect(screen.queryByText('Broadcast Status')).not.toBeInTheDocument());
  });

  it('keeps the dialog open and logs when the broadcast request fails', async () => {
    adminApi.broadcastAnnouncement.mockRejectedValue(new Error('send failed'));
    adminApi.getBroadcastStatus.mockResolvedValue({
      data: { success: true, broadcasts: [{ platform: 'twitch', status: 'success' }] },
    });
    await loaded();
    fireEvent.click(card('Pinned news').getByRole('button', { name: 'Broadcast' }));
    await screen.findByRole('table');
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Broadcast Now' }));

    await waitFor(() =>
      expect(console.error).toHaveBeenCalledWith('Failed to broadcast announcement:', expect.any(Error)),
    );
    expect(screen.getByText('Broadcast Status')).toBeInTheDocument();
  });
});
