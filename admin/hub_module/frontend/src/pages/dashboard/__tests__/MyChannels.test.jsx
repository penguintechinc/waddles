/** MyChannels: account linking via hub-api's GET /api/v1/auth/oauth/<platform>/link, and the gated link-requests section. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import api from '../../../services/api';
import { apiClient } from '../../../lib/apiClient';
import { stubHttp } from '../../../test/httpStub';
import * as AuthContext from '../../../contexts/AuthContext';
import MyChannels from '../MyChannels';

const LINKED = '/api/v1/user/linked-platforms';
let http;
const realLocation = window.location;

beforeEach(() => {
  vi.spyOn(AuthContext, 'useAuth').mockReturnValue({ user: { id: 1 } });
  // jsdom forbids navigation; capture assignments to location.href instead.
  Object.defineProperty(window, 'location', { configurable: true, value: { href: 'http://localhost/dashboard/my-channels' } });
});

afterEach(() => {
  http?.restore();
  Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
  vi.restoreAllMocks();
});

function mount() {
  return render(
    <MemoryRouter>
      <MyChannels />
    </MemoryRouter>,
  );
}

describe('MyChannels connect flow', () => {
  it('GETs the link route with the session and redirects to the authorizeUrl from hub-api', async () => {
    http = stubHttp([api, apiClient], {
      [`GET ${LINKED}`]: { platforms: [] },
      'GET /api/v1/auth/oauth/discord/link': { success: true, authorizeUrl: 'https://discord.example/authorize?state=s1', state: 's1' },
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Discord' }));
    await waitFor(() => expect(window.location.href).toBe('https://discord.example/authorize?state=s1'));
    expect(http.seen.map((r) => `${r.method} ${r.url}`)).toContain('GET /api/v1/auth/oauth/discord/link');
    expect(http.seen.some((r) => r.url === '/api/v1/auth/discord')).toBe(false);
  });

  it.each(['twitch', 'slack'])('supports %s', async (key) => {
    http = stubHttp([api, apiClient], {
      [`GET ${LINKED}`]: { platforms: [] },
      [`GET /api/v1/auth/oauth/${key}/link`]: { authorizeUrl: `https://${key}.example/a` },
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(`Connect ${key}`, 'i') }));
    await waitFor(() => expect(window.location.href).toBe(`https://${key}.example/a`));
  });

  it('surfaces the server error and does not navigate', async () => {
    http = stubHttp([api, apiClient], {
      [`GET ${LINKED}`]: { platforms: [] },
      'GET /api/v1/auth/oauth/discord/link': () => {
        throw Object.assign(new Error('bad'), { isAxiosError: true, response: { status: 400, data: { error: { message: 'Discord login is not configured' } } } });
      },
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Discord' }));
    expect(await screen.findByText('Discord login is not configured')).toBeInTheDocument();
    expect(window.location.href).toBe('http://localhost/dashboard/my-channels');
    expect(screen.getByRole('button', { name: 'Connect Discord' })).toBeEnabled();
  });

  it('shows a generic error when the request fails without a body, and when no URL comes back', async () => {
    http = stubHttp([api, apiClient], {
      [`GET ${LINKED}`]: { platforms: [] },
      'GET /api/v1/auth/oauth/twitch/link': { success: true },
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Twitch' }));
    expect(await screen.findByText('Could not start the Twitch connection. Please try again.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Connect Discord' })); // no stub -> 404 with message
    expect(await screen.findByText('Not found')).toBeInTheDocument();
  });

  it('refuses providers hub-api cannot link (YouTube/KICK) with a visible message and no request', async () => {
    http = stubHttp([api, apiClient], { [`GET ${LINKED}`]: { platforms: [] } });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Connect YouTube' }));
    expect(await screen.findByText("Connecting YouTube isn't supported yet.")).toBeInTheDocument();
    expect(http.seen.map((r) => r.url)).toEqual([LINKED]);
  });
});

describe('MyChannels gating', () => {
  it('shows "not available yet" for link requests and never calls the unserved routes', async () => {
    http = stubHttp([api, apiClient], { [`GET ${LINKED}`]: { platforms: [{ platform: 'discord', username: 'penguin' }] } });
    mount();
    await screen.findByText('penguin');
    const gated = screen.getByTestId('not-available-yet');
    expect(gated).toHaveAttribute('data-feature', 'member-server-link-requests');
    expect(screen.queryByText('Request to Join a Community')).not.toBeInTheDocument();
    expect(http.seen.map((r) => r.url)).toEqual([LINKED]);
  });

  it('reports a load failure', async () => {
    http = stubHttp([api, apiClient], {});
    mount();
    expect(await screen.findByText('Failed to load channel data')).toBeInTheDocument();
  });
});
