/**
 * AdminMusicProviders runs against hub-api's served `music/providers` routes (stubbed only at
 * the HTTP adapter): list -> cards, enable toggle -> PUT .../config, OAuth start sends
 * redirectUri, and every failure is shown, never swallowed.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import api from '../../../services/api';
import { apiClient } from '../../../lib/apiClient';
import { stubHttp } from '../../../test/httpStub';
import { isFeatureServed } from '../../../lib/unservedFeatures';
import AdminMusicProviders, { toProviderMap } from '../AdminMusicProviders';

const CID = '0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f41';
const BASE = `/api/v1/admin/${CID}/music/providers`;

const spotify = (over = {}) => ({
  communityId: CID,
  providerName: 'spotify',
  isConnected: true,
  isActive: false,
  oauthExpiresAt: null,
  lastSync: null,
  config: {},
  createdAt: '2026-11-01T00:00:00Z',
  updatedAt: '2026-11-01T00:00:00Z',
  ...over,
});

const apiError = (status, message) =>
  Object.assign(new Error(message), { isAxiosError: true, response: { status, data: { error: { message } } } });

let http;
afterEach(() => {
  http?.restore();
  vi.restoreAllMocks();
});

function mount() {
  return render(
    <MemoryRouter initialEntries={[`/admin/${CID}/music/providers`]}>
      <Routes>
        <Route path="/admin/:communityId/music/providers" element={<AdminMusicProviders />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('AdminMusicProviders', () => {
  it('is no longer registered as unserved and renders no stub', async () => {
    expect(isFeatureServed('music-provider-config')).toBe(true);
    http = stubHttp([api, apiClient], { [`GET ${BASE}`]: { success: true, providers: [] } });
    mount();
    expect(await screen.findByTestId('music-providers-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('not-available-yet')).toBeNull();
    expect(screen.getAllByText('Not Connected')).toHaveLength(3);
  });

  it('shows a loading state before the list resolves', async () => {
    http = stubHttp([api, apiClient], { [`GET ${BASE}`]: { success: true, providers: [] } });
    mount();
    expect(screen.getByRole('status', { name: 'Loading music providers' })).toBeInTheDocument();
    expect(await screen.findByTestId('music-providers-empty')).toBeInTheDocument();
    expect(screen.queryByRole('status', { name: 'Loading music providers' })).toBeNull();
  });

  it('shows the load error with a retry that refetches', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    let fail = true;
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: () => {
        if (fail) throw apiError(403, 'Forbidden');
        return { success: true, providers: [spotify()] };
      },
    });
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('Forbidden');
    fail = false;
    fireEvent.click(screen.getByText('Retry'));
    expect(await screen.findByLabelText('Enable Spotify')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('enables a connected provider via PUT .../spotify/config and reflects the response', async () => {
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: { success: true, providers: [spotify()] },
      [`PUT ${BASE}/spotify/config`]: (req) => ({ success: true, provider: spotify({ isActive: req.body.enabled }) }),
    });
    mount();
    const toggle = await screen.findByLabelText('Enable Spotify');
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    await waitFor(() => expect(screen.getByLabelText('Enable Spotify')).toBeChecked());
    expect(screen.getByText('Spotify enabled')).toBeInTheDocument();
    expect(http.seen.find((r) => r.method === 'PUT').body).toEqual({ enabled: true });
    expect(screen.getByText('Enabled & Active')).toBeInTheDocument();
  });

  it('surfaces a 402 plan gate from the toggle instead of failing silently', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: { success: true, providers: [spotify()] },
      [`PUT ${BASE}/spotify/config`]: () => {
        throw apiError(402, 'The Music Station is not enabled for this plan');
      },
    });
    mount();
    fireEvent.click(await screen.findByLabelText('Enable Spotify'));
    expect(await screen.findByText('The Music Station is not enabled for this plan')).toBeInTheDocument();
    expect(screen.getByLabelText('Enable Spotify')).not.toBeChecked();
  });

  it('starts OAuth with this page as redirectUri and reports a blocked pop-up', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: { success: true, providers: [] },
      [`POST ${BASE}/youtube/oauth`]: { success: true, authUrl: 'https://accounts.example/auth', stateToken: 's' },
    });
    mount();
    await screen.findByTestId('music-providers-empty');
    fireEvent.click(screen.getAllByText('Connect')[1]);
    expect(await screen.findByText(/sign-in window was blocked/)).toBeInTheDocument();
    const post = http.seen.find((r) => r.method === 'POST');
    expect(post.body).toEqual({ redirectUri: window.location.href });
    expect(window.open).toHaveBeenCalledWith('https://accounts.example/auth', 'youtube_oauth', expect.any(String));
  });

  it('disconnects after confirmation and refetches', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    let connected = true;
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: () => ({ success: true, providers: [spotify({ isConnected: connected })] }),
      [`DELETE ${BASE}/spotify`]: () => {
        connected = false;
        return { success: true, message: 'spotify provider disconnected' };
      },
    });
    mount();
    fireEvent.click(await screen.findByText('Disconnect'));
    expect(await screen.findByText('Spotify disconnected successfully')).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText('Not Connected')).toHaveLength(3));
  });
});

describe('toProviderMap', () => {
  it('keys by providerName and flags an expired token', () => {
    const map = toProviderMap([
      spotify({ isActive: true, oauthExpiresAt: '2000-01-01T00:00:00Z' }),
      spotify({ providerName: 'youtube', isConnected: false }),
    ]);
    expect(map.spotify).toEqual({ connected: true, enabled: true, tokenExpired: true, connectedAt: '2026-11-01T00:00:00Z' });
    expect(map.youtube.connected).toBe(false);
    expect(map.youtube.tokenExpired).toBe(false);
    expect(toProviderMap(undefined)).toEqual({});
  });
});
