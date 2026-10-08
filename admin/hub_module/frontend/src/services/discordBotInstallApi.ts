import { apiClient } from '../lib/apiClient';

/**
 * Typed client for the SaaS/global "Add Waddles to Discord" bot-install
 * link (`hub_api/blueprints/v1/discord_bot_install.py`). PRE-AUTH endpoint
 * -- `apiClient`'s HttpOnly-cookie auth is irrelevant here, the route
 * accepts anonymous requests exactly like `publicApi`'s own routes
 * (`services/api.js`). Separate from the per-tenant custom-app install
 * flow (`tenants/discord/install/authorize`) and from the `identify
 * guilds` OAuth login connection card (`CommunityConnections.jsx`) -- this
 * is the one link that actually adds the shared SaaS bot to a Discord
 * server.
 */

interface BotInstallUrlResponse {
  success: boolean;
  installUrl: string;
}

/** Shape of the 503 `provider_not_configured` error body this route returns. */
export interface DiscordBotInstallErrorBody {
  error: 'provider_not_configured';
  provider: 'discord';
}

export const discordBotInstallApi = {
  /** `GET /api/v1/public/discord/bot-install` -- resolves the install URL, never a secret. */
  getInstallUrl: () =>
    apiClient
      .get<BotInstallUrlResponse>('/api/v1/public/discord/bot-install')
      .then((res) => res.data.installUrl),
};

/** `true` when `err` is this route's 503 `provider_not_configured` response. */
export function isProviderNotConfigured(err: unknown): boolean {
  if (typeof err !== 'object' || err === null || !('response' in err)) {
    return false;
  }
  const response = (err as { response?: { status?: number; data?: Partial<DiscordBotInstallErrorBody> } })
    .response;
  return response?.status === 503 && response?.data?.error === 'provider_not_configured';
}
