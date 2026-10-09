/**
 * Wire-level contract for announcement broadcasting: the REAL `adminApi`
 * runs over a recording HTTP adapter, so the exact JSON the browser would
 * POST is asserted.
 *
 * KNOWN DEFECT: the page calls
 * `adminApi.broadcastAnnouncement(communityId, id, { platforms })`, but the
 * helper itself wraps its third argument as `{ platforms }`, so the request
 * body becomes `{"platforms": {"platforms": [...]}}`. hub-api
 * (`community_announcements.broadcast_route`) requires `platforms` to be a
 * non-empty list and rejects the dict -- broadcasting from this page cannot
 * succeed. `it.fails` goes red once fixed; then make it a plain `it`.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminAnnouncements from '../AdminAnnouncements';
import api from '../../../services/api';
import { recordRequests } from '../../../test/apiContract';

vi.mock('@penguintechinc/react-libs', async () => ({
  FormModalBuilder: (await import('../../../test/formModalStub')).FormModalStub,
}));

const rec = recordRequests(api);
afterAll(rec.restore);

beforeEach(() => {
  rec.calls.length = 0;
  rec.reply.data = {
    success: true,
    data: [
      {
        id: 1,
        title: 'Big news',
        content: 'body',
        announcement_type: 'general',
        status: 'published',
        is_pinned: false,
        created_at: new Date().toISOString(),
      },
    ],
    pagination: { totalPages: 1 },
    connectedPlatforms: [],
    broadcasts: [{ platform: 'twitch', serverName: 'Prior', status: 'success' }],
  };
});

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/announcements']}>
      <Routes>
        <Route path="/admin/:communityId/announcements" element={<AdminAnnouncements />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function pickPlatformAndBroadcast() {
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Broadcast' }));
  await screen.findByText('Prior');
  fireEvent.click(screen.getAllByRole('checkbox')[0]);
  fireEvent.click(screen.getByRole('button', { name: 'Broadcast Now' }));
  await waitFor(() => expect(rec.calls.some((c) => c.method === 'post')).toBe(true));
  const post = rec.calls.find((c) => c.method === 'post');
  return { url: post.url, body: JSON.parse(post.data) };
}

describe('AdminAnnouncements broadcast over the real adminApi', () => {
  it('POSTs to the announcement broadcast route', async () => {
    const { url } = await pickPlatformAndBroadcast();
    expect(url).toBe('/api/v1/admin/7/announcements/1/broadcast');
  });

  it.fails('sends platforms as a flat list, as hub-api requires', async () => {
    const { body } = await pickPlatformAndBroadcast();
    expect(Array.isArray(body.platforms)).toBe(true);
  });
});
