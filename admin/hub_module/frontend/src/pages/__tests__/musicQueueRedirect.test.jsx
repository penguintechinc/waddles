/**
 * Legacy music-station removal: the old public queue URL must redirect to the member
 * music page, the dead API client methods must be gone, and settings must no longer
 * render the music-station policy (YouTube labels) control.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { MusicQueueRedirect } from '../../App';
import AdminMusicSettings from '../admin/AdminMusicSettings';
import { adminApi, publicApi } from '../../services/api';

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    adminApi: {
      ...actual.adminApi,
      updateModuleConfig: vi.fn().mockResolvedValue({ data: { success: true, config: { blacklist: [] } } }),
    },
  };
});

describe('legacy music-station removal', () => {
  it('redirects /c/:id/music/queue to /community/:id/music', () => {
    render(
      <MemoryRouter initialEntries={['/c/42/music/queue']}>
        <Routes>
          <Route path="/c/:communityId/music/queue" element={<MusicQueueRedirect />} />
          <Route path="/community/:communityId/music" element={<div>member music 42</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText('member music 42')).toBeInTheDocument();
  });

  it('no longer exposes the removed client methods', () => {
    expect(publicApi.getMusicQueue).toBeUndefined();
    expect(adminApi.removeMusicQueueItem).toBeUndefined();
    expect(adminApi.getMusicStationPolicy).toBeUndefined();
    expect(adminApi.updateMusicStationPolicy).toBeUndefined();
  });

  it('settings page renders without the YouTube labels policy section', async () => {
    render(
      <MemoryRouter initialEntries={['/admin/42/music/settings']}>
        <Routes>
          <Route path="/admin/:communityId/music/settings" element={<AdminMusicSettings />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText(/Music Settings Tips/);
    expect(screen.queryByTestId('youtube-labels-section')).toBeNull();
  });
});
