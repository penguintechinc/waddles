/**
 * Tests for the "Add Waddles to Discord" bot-install card -- flag-gated
 * render, the resolved install link, and the 503 `provider_not_configured`
 * error surface.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import DiscordBotInstallLink from '../DiscordBotInstallLink';
import { useFeatureFlag } from '../../../lib/useFeatureFlag';
import { discordBotInstallApi } from '../../../services/discordBotInstallApi';

vi.mock('../../../lib/useFeatureFlag', () => ({
  useFeatureFlag: vi.fn(),
}));

vi.mock('../../../services/discordBotInstallApi', () => ({
  discordBotInstallApi: {
    getInstallUrl: vi.fn(),
  },
  isProviderNotConfigured: (err: unknown) =>
    typeof err === 'object' &&
    err !== null &&
    (err as { response?: { status?: number; data?: { error?: string } } }).response?.status ===
      503 &&
    (err as { response?: { data?: { error?: string } } }).response?.data?.error ===
      'provider_not_configured',
}));

const mockedUseFeatureFlag = vi.mocked(useFeatureFlag);
const mockedGetInstallUrl = vi.mocked(discordBotInstallApi.getInstallUrl);

function renderWithQueryClient(ui: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

describe('DiscordBotInstallLink', () => {
  beforeEach(() => {
    mockedUseFeatureFlag.mockReset();
    mockedGetInstallUrl.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders nothing when the feature flag is off', () => {
    mockedUseFeatureFlag.mockReturnValue(false);
    const { container } = renderWithQueryClient(<DiscordBotInstallLink />);
    expect(container).toBeEmptyDOMElement();
    expect(mockedGetInstallUrl).not.toHaveBeenCalled();
  });

  it('renders the install link pointing at the resolved URL', async () => {
    mockedUseFeatureFlag.mockReturnValue(true);
    mockedGetInstallUrl.mockResolvedValue(
      'https://discord.com/oauth2/authorize?client_id=123&scope=bot+applications.commands&permissions=1108906961990&integration_type=0',
    );

    renderWithQueryClient(<DiscordBotInstallLink />);

    const link = await screen.findByTestId('discord-bot-install-link');
    await waitFor(() =>
      expect(link).toHaveAttribute(
        'href',
        'https://discord.com/oauth2/authorize?client_id=123&scope=bot+applications.commands&permissions=1108906961990&integration_type=0',
      ),
    );
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('shows a configuration message on 503 provider_not_configured', async () => {
    mockedUseFeatureFlag.mockReturnValue(true);
    const err = {
      response: { status: 503, data: { error: 'provider_not_configured', provider: 'discord' } },
    };
    mockedGetInstallUrl.mockRejectedValue(err);
    vi.spyOn(console, 'error').mockImplementation(() => {});

    renderWithQueryClient(<DiscordBotInstallLink />);

    const message = await screen.findByTestId('discord-bot-install-error');
    expect(message).toHaveTextContent(/configure the Discord application/i);
  });

  it('shows a generic failure message for any other error', async () => {
    mockedUseFeatureFlag.mockReturnValue(true);
    mockedGetInstallUrl.mockRejectedValue(new Error('network error'));
    vi.spyOn(console, 'error').mockImplementation(() => {});

    renderWithQueryClient(<DiscordBotInstallLink />);

    const message = await screen.findByTestId('discord-bot-install-error');
    expect(message).toHaveTextContent(/Failed to load the Discord install link/i);
  });
});
