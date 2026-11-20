/**
 * Regression: AdminMusicSettings must use the real `music/settings` endpoints, never
 * `updateModuleConfig(communityId, 'music', ...)` (PUT /modules/music/config -> 404).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminMusicSettings from '../AdminMusicSettings';
import { adminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  adminApi: {
    getMusicSettings: vi.fn(),
    updateMusicSettings: vi.fn(),
    updateModuleConfig: vi.fn(),
  },
}));

const SETTINGS = {
  isActive: true,
  autoplayEnabled: false,
  requireDjApproval: true,
  volumeLimit: 60,
  allowedGenres: ['rock'],
  blockedArtists: ['Bad Band'],
};

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/music/settings']}>
      <Routes>
        <Route path="/admin/:communityId/music/settings" element={<AdminMusicSettings />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('AdminMusicSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminApi.getMusicSettings.mockResolvedValue({ data: { success: true, settings: SETTINGS } });
    adminApi.updateMusicSettings.mockResolvedValue({ data: { success: true } });
  });

  it('loads settings from the music/settings endpoint, not module config', async () => {
    mount();
    await screen.findByText('Music Settings');
    expect(adminApi.getMusicSettings).toHaveBeenCalledWith('7');
    expect(adminApi.updateModuleConfig).not.toHaveBeenCalled();
    expect(screen.getByText('Bad Band')).toBeTruthy();
    expect(screen.getByLabelText('Require DJ approval').checked).toBe(true);
  });

  it('saves the edited settings through updateMusicSettings', async () => {
    mount();
    await screen.findByText('Music Settings');
    fireEvent.click(screen.getByLabelText('Autoplay'));
    fireEvent.change(screen.getByLabelText('Blocked artist'), { target: { value: 'Worse Band' } });
    fireEvent.click(screen.getByText('Add'));
    fireEvent.click(screen.getAllByText('Save Settings')[0]);
    await waitFor(() => expect(adminApi.updateMusicSettings).toHaveBeenCalledTimes(1));
    expect(adminApi.updateMusicSettings).toHaveBeenCalledWith('7', {
      isActive: true,
      autoplayEnabled: true,
      requireDjApproval: true,
      volumeLimit: 60,
      allowedGenres: ['rock'],
      blockedArtists: ['Bad Band', 'Worse Band'],
    });
    expect(adminApi.updateModuleConfig).not.toHaveBeenCalled();
    await screen.findByText('Music settings saved successfully');
  });

  it('shows the API error and falls back to defaults when load fails', async () => {
    adminApi.getMusicSettings.mockRejectedValue({ response: { status: 402, data: { error: { message: 'not enabled' } } } });
    mount();
    await screen.findByText('not enabled');
    expect(screen.getByText('No blocked artists')).toBeTruthy();
  });

  it('surfaces save failures', async () => {
    adminApi.updateMusicSettings.mockRejectedValue({ response: { data: { error: { message: 'nope' } } } });
    mount();
    await screen.findByText('Music Settings');
    fireEvent.click(screen.getAllByText('Save Settings')[0]);
    await screen.findByText('nope');
  });
});
