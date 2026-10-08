import { QueryClient } from '@tanstack/react-query';

/**
 * Singleton TanStack Query client for the hub webui (S0 foundation).
 *
 * New per-domain `useQuery`/`useMutation` hooks (one slice at a time) share
 * this client; existing pages keep using raw `axios` via `services/api.js`
 * unconditionally until they're converted on-touch -- the two coexist by
 * design during the modularity migration.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: 0,
    },
  },
});
