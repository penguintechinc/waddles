/**
 * Tests for the per-community OAuth connections page (gh-320) — renders all
 * six providers, drives the popup+postMessage connect flow (same-origin
 * only), disconnect confirm flow, and the provider_not_configured notice.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import CommunityConnections from '../CommunityConnections';
import { adminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  adminApi: {
    listCommunityConnections: vi.fn(),
    authorizeCommunityConnection: vi.fn(),
    disconnectCommunityConnection: vi.fn(),
  },
}));

// The embedded `DiscordBotInstallLink` card reads its own feature flag --
// default OFF here so this suite's assertions (which predate that card)
// stay scoped to the OAuth provider grid; the card's own behavior is
// covered by `components/settings/__tests__/DiscordBotInstallLink.test.tsx`.
vi.mock('../../../lib/useFeatureFlag', () => ({
  useFeatureFlag: vi.fn().mockReturnValue(false),
}));

const EMPTY_RESPONSE = { data: { connections: [], callback_base: 'https://hub.example/api/v1' } };

function connectionsResponse(overrides = []) {
  return {
    data: {
      connections: overrides,
      callback_base: 'https://hub.example/api/v1',
    },
  };
}

function mount(communityId = '42') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/admin/${communityId}/connections`]}>
        <Routes>
          <Route path="/admin/:communityId/connections" element={<CommunityConnections />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

let openSpy;

beforeEach(() => {
  vi.clearAllMocks();
  openSpy = vi.spyOn(window, 'open').mockReturnValue({ closed: false });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('CommunityConnections', () => {
  it('renders all six providers with connected/not-connected states', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(
      connectionsResponse([
        {
          provider: 'twitch',
          connected: true,
          scopes: ['chat:read', 'chat:write'],
          expires_at: '2026-12-01T00:00:00Z',
          updated_at: '2026-09-01T00:00:00Z',
          account_label: 'waddlebot_streamer',
        },
      ]),
    );

    mount();

    expect(await screen.findByTestId('connection-status-twitch')).toHaveTextContent(
      'Connected as waddlebot_streamer',
    );
    for (const provider of ['youtube', 'spotify', 'discord', 'kick', 'slack']) {
      expect(screen.getByTestId(`connection-status-${provider}`)).toHaveTextContent('Not connected');
    }
    expect(screen.getByText('chat:read')).toBeInTheDocument();
    expect(screen.getByText('chat:write')).toBeInTheDocument();
    expect(adminApi.listCommunityConnections).toHaveBeenCalledWith('42');
  });

  it('shows an error state if the load fails', async () => {
    adminApi.listCommunityConnections.mockRejectedValue({ response: { status: 500 } });

    mount();

    expect(await screen.findByTestId('connections-error')).toHaveTextContent('Failed to load connections');
  });

  it('calls authorize and opens a centered popup on Connect', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);
    adminApi.authorizeCommunityConnection.mockResolvedValue({
      data: { authorize_url: 'https://oauth.example/twitch/authorize' },
    });

    mount();
    await screen.findByTestId('connection-connect-twitch');

    await act(async () => {
      fireEvent.click(screen.getByTestId('connection-connect-twitch'));
    });

    await waitFor(() =>
      expect(adminApi.authorizeCommunityConnection).toHaveBeenCalledWith('42', 'twitch'),
    );
    expect(openSpy).toHaveBeenCalledWith(
      'https://oauth.example/twitch/authorize',
      'waddlebot-oauth',
      expect.stringContaining('width=600,height=750'),
    );
  });

  it('refetches when a same-origin OAUTH_CALLBACK message arrives', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);
    adminApi.authorizeCommunityConnection.mockResolvedValue({
      data: { authorize_url: 'https://oauth.example/discord/authorize' },
    });

    mount();
    await screen.findByTestId('connection-connect-discord');

    await act(async () => {
      fireEvent.click(screen.getByTestId('connection-connect-discord'));
    });
    await waitFor(() => expect(adminApi.authorizeCommunityConnection).toHaveBeenCalled());

    adminApi.listCommunityConnections.mockResolvedValue(
      connectionsResponse([
        { provider: 'discord', connected: true, scopes: [], expires_at: null, updated_at: null, account_label: 'Acme Guild' },
      ]),
    );

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'OAUTH_CALLBACK', provider: 'discord', ok: true },
          origin: window.location.origin,
        }),
      );
    });

    expect(await screen.findByTestId('connection-status-discord')).toHaveTextContent(
      'Connected as Acme Guild',
    );
  });

  it('ignores a postMessage from a different origin', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);

    mount();
    await screen.findByTestId('connection-connect-slack');

    const callsBefore = adminApi.listCommunityConnections.mock.calls.length;

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'OAUTH_CALLBACK', provider: 'slack', ok: true },
          origin: 'https://evil.example',
        }),
      );
    });

    expect(adminApi.listCommunityConnections.mock.calls.length).toBe(callsBefore);
  });

  it('shows the not-configured notice on a 503 provider_not_configured response', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);
    adminApi.authorizeCommunityConnection.mockRejectedValue({
      response: { status: 503, data: { error: 'provider_not_configured', provider: 'kick' } },
    });

    mount();
    await screen.findByTestId('connection-connect-kick');

    await act(async () => {
      fireEvent.click(screen.getByTestId('connection-connect-kick'));
    });

    expect(await screen.findByTestId('connection-not-configured-kick')).toHaveTextContent(
      'Ask the platform admin to configure the Kick OAuth client',
    );
  });

  it('disconnects a connected provider after confirm and refetches', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(
      connectionsResponse([
        { provider: 'spotify', connected: true, scopes: [], expires_at: null, updated_at: null, account_label: 'DJ Waddle' },
      ]),
    );
    adminApi.disconnectCommunityConnection.mockResolvedValue({});

    mount();
    const disconnectBtn = await screen.findByTestId('connection-disconnect-spotify');

    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);

    await act(async () => {
      fireEvent.click(disconnectBtn);
    });

    await waitFor(() =>
      expect(adminApi.disconnectCommunityConnection).toHaveBeenCalledWith('42', 'spotify'),
    );
    expect(await screen.findByTestId('connections-feedback')).toHaveTextContent('Spotify disconnected');
    expect(adminApi.listCommunityConnections).toHaveBeenCalledTimes(2);
  });

  it('does not disconnect when the confirm dialog is dismissed', async () => {
    window.confirm.mockReturnValue(false);
    adminApi.listCommunityConnections.mockResolvedValue(
      connectionsResponse([
        { provider: 'youtube', connected: true, scopes: [], expires_at: null, updated_at: null, account_label: 'Waddle Channel' },
      ]),
    );

    mount();
    const disconnectBtn = await screen.findByTestId('connection-disconnect-youtube');

    await act(async () => {
      fireEvent.click(disconnectBtn);
    });

    expect(adminApi.disconnectCommunityConnection).not.toHaveBeenCalled();
  });

  it('shows an error toast when disconnect fails', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(
      connectionsResponse([
        { provider: 'youtube', connected: true, scopes: [], expires_at: null, updated_at: null, account_label: 'Waddle Channel' },
      ]),
    );
    adminApi.disconnectCommunityConnection.mockRejectedValue({ response: { status: 500 } });

    mount();
    const disconnectBtn = await screen.findByTestId('connection-disconnect-youtube');

    await act(async () => {
      fireEvent.click(disconnectBtn);
    });

    expect(await screen.findByTestId('connections-feedback')).toHaveTextContent(
      'Failed to disconnect YouTube',
    );
  });

  it('shows a generic error toast when authorize fails for a reason other than provider_not_configured', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);
    adminApi.authorizeCommunityConnection.mockRejectedValue({ response: { status: 500 } });

    mount();
    await screen.findByTestId('connection-connect-slack');

    await act(async () => {
      fireEvent.click(screen.getByTestId('connection-connect-slack'));
    });

    expect(await screen.findByTestId('connections-feedback')).toHaveTextContent(
      'Failed to start Slack connection',
    );
    expect(screen.queryByTestId('connection-not-configured-slack')).not.toBeInTheDocument();
  });

  it('treats a missing connections field and an invalid timestamp as safe fallbacks', async () => {
    adminApi.listCommunityConnections.mockResolvedValueOnce({ data: {} });

    mount();

    expect(await screen.findByTestId('connection-status-youtube')).toHaveTextContent('Not connected');

    adminApi.listCommunityConnections.mockResolvedValueOnce(
      connectionsResponse([
        {
          provider: 'youtube',
          connected: true,
          scopes: [],
          expires_at: 'not-a-real-date',
          updated_at: null,
          account_label: 'Waddle Channel',
        },
      ]),
    );
    await act(async () => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'OAUTH_CALLBACK', provider: 'youtube', ok: true },
          origin: window.location.origin,
        }),
      );
    });

    expect(await screen.findByTestId('connection-status-youtube')).toHaveTextContent(
      'Connected as Waddle Channel',
    );
    expect(screen.queryByText(/Expires/)).not.toBeInTheDocument();
  });

  it('shows a per-provider error message on OAUTH_CALLBACK ok:false for a known provider', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);

    mount();
    await screen.findByTestId('connection-connect-twitch');

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'OAUTH_CALLBACK', provider: 'twitch', ok: false },
          origin: window.location.origin,
        }),
      );
    });

    expect(await screen.findByTestId('connections-feedback')).toHaveTextContent('Failed to connect Twitch');
  });

  it('shows "unknown account" when a connected provider has no account_label', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(
      connectionsResponse([
        { provider: 'twitch', connected: true, scopes: [], expires_at: null, updated_at: null, account_label: null },
      ]),
    );

    mount();

    expect(await screen.findByTestId('connection-status-twitch')).toHaveTextContent(
      'Connected as unknown account',
    );
  });

  it('ignores a same-origin message with an unrelated type', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);

    mount();
    await screen.findByTestId('connection-connect-slack');
    const callsBefore = adminApi.listCommunityConnections.mock.calls.length;

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'SOME_OTHER_MESSAGE' },
          origin: window.location.origin,
        }),
      );
    });

    expect(adminApi.listCommunityConnections.mock.calls.length).toBe(callsBefore);
  });

  it('shows an error toast on OAUTH_CALLBACK ok:false for an unrecognized provider', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);
    adminApi.authorizeCommunityConnection.mockResolvedValue({
      data: { authorize_url: 'https://oauth.example/kick/authorize' },
    });

    mount();
    await screen.findByTestId('connection-connect-kick');
    await act(async () => {
      fireEvent.click(screen.getByTestId('connection-connect-kick'));
    });
    await waitFor(() => expect(adminApi.authorizeCommunityConnection).toHaveBeenCalled());

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'OAUTH_CALLBACK', provider: 'not-a-real-provider', ok: false },
          origin: window.location.origin,
        }),
      );
    });

    expect(await screen.findByTestId('connections-feedback')).toHaveTextContent(
      'Failed to connect not-a-real-provider',
    );
  });

  it('dismisses the feedback banner when the close button is clicked', async () => {
    adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);
    adminApi.disconnectCommunityConnection.mockResolvedValue({});
    adminApi.listCommunityConnections.mockResolvedValueOnce(
      connectionsResponse([
        { provider: 'youtube', connected: true, scopes: [], expires_at: null, updated_at: null, account_label: 'Waddle Channel' },
      ]),
    );

    mount();
    const disconnectBtn = await screen.findByTestId('connection-disconnect-youtube');
    await act(async () => {
      fireEvent.click(disconnectBtn);
    });
    const feedback = await screen.findByTestId('connections-feedback');

    await act(async () => {
      fireEvent.click(screen.getByLabelText('Dismiss message'));
    });

    expect(feedback).not.toBeInTheDocument();
  });

  it(
    'falls back to polling and refetches once the popup closes without a postMessage',
    async () => {
      const popupMock = { closed: false };
      openSpy.mockReturnValue(popupMock);
      adminApi.listCommunityConnections.mockResolvedValue(EMPTY_RESPONSE);
      adminApi.authorizeCommunityConnection.mockResolvedValue({
        data: { authorize_url: 'https://oauth.example/spotify/authorize' },
      });

      mount();
      await screen.findByTestId('connection-connect-spotify');

      await act(async () => {
        fireEvent.click(screen.getByTestId('connection-connect-spotify'));
      });
      await waitFor(() => expect(adminApi.authorizeCommunityConnection).toHaveBeenCalled());

      // The popup closing (without ever posting OAUTH_CALLBACK) is the
      // signal the 2s polling fallback watches for.
      popupMock.closed = true;

      await waitFor(
        () => expect(adminApi.listCommunityConnections).toHaveBeenCalledTimes(2),
        { timeout: 4000, interval: 250 },
      );
    },
    8000,
  );
});
