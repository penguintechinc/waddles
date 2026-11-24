import { apiClient } from '../lib/apiClient';
import { isRecord, readBool, readOptString } from '../lib/wire';

/**
 * Typed client for the tenant "bring your own Discord app" install flow
 * (`hub_api/blueprints/v1/tenant_discord_install.py`). Thin: credentials are
 * forwarded once to hub-api and never stored or echoed client-side.
 */

export interface DiscordInstallRequest {
  applicationId: string;
  clientSecret: string;
  botToken: string | null;
}

export interface DiscordInstallStatus {
  installed: boolean;
  applicationId: string | null;
  connectionStatus: string | null;
  updatedAt: string | null;
  /** Redirect URI to register in the Discord Developer Portal, when hub-api supplies it. */
  redirectUri: string | null;
}

/** Normalises the status body; tolerates snake_case or camelCase keys. */
export function parseInstallStatus(data: unknown): DiscordInstallStatus {
  const body = isRecord(data) ? data : {};
  const rec = isRecord(body['data']) ? (body['data'] as Record<string, unknown>) : body;
  const pick = (snake: string, camel: string): string | null =>
    readOptString(rec, snake) ?? readOptString(rec, camel);
  return {
    installed: readBool(rec, 'installed', false),
    applicationId: pick('application_id', 'applicationId'),
    connectionStatus: pick('connection_status', 'connectionStatus'),
    updatedAt: pick('updated_at', 'updatedAt'),
    redirectUri: pick('redirect_uri', 'redirectUri'),
  };
}

export const tenantDiscordInstallApi = {
  /** `POST /api/v1/tenants/discord/install/authorize` -- returns the Discord authorize URL. */
  authorize: async (req: DiscordInstallRequest): Promise<string> => {
    const res = await apiClient.post<Record<string, unknown>>('/api/v1/tenants/discord/install/authorize', {
      application_id: req.applicationId,
      client_secret: req.clientSecret,
      bot_token: req.botToken,
    });
    const url = res.data?.['authorize_url'] ?? res.data?.['authorizeUrl'];
    if (typeof url !== 'string' || url === '') {
      throw new Error('hub-api returned no authorize URL');
    }
    return url;
  },

  /** `GET /api/v1/tenants/discord/install/status` -- `{installed, application_id, connection_status, updated_at}`. */
  getStatus: () =>
    apiClient
      .get<unknown>('/api/v1/tenants/discord/install/status')
      .then((res) => parseInstallStatus(res.data)),
};
