import { useEffect, useState } from 'react';
import { oauthApi } from '../services/api';

/**
 * Enabled OAuth login/link providers, as reported by hub-api (`GET /api/v1/auth/providers`).
 * hub-api is the only source of truth -- the webui never hardcodes the list. Fails closed:
 * until the response arrives, or if it fails, no provider is shown.
 *
 * @returns {{ providers: Array<{platform: string, displayName: string}>,
 *   platforms: string[], loaded: boolean }}
 */
export default function useAuthProviders() {
  const [providers, setProviders] = useState([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    oauthApi
      .providers()
      .then(({ data }) => {
        if (!cancelled) setProviders(Array.isArray(data?.providers) ? data.providers : []);
      })
      .catch((err) => {
        console.error('[useAuthProviders] failed to load providers', { message: err?.message });
        if (!cancelled) setProviders([]);
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { providers, platforms: providers.map((p) => p.platform), loaded };
}
