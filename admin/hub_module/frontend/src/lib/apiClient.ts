import axios, { type AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios';

// SECURITY (security.md C4 / OWASP A07): mirrors `services/api.js` exactly --
// the session JWT lives ONLY in the HttpOnly `wb_session` cookie hub-api
// sets on login/OAuth-exchange/refresh, never in localStorage/JS-readable
// storage. `withCredentials: true` sends it automatically; there is nothing
// here for an XSS payload to read. Do NOT add an Authorization header or
// token storage to this client -- see `services/api.js` for the full
// rationale.
//
// This module is the typed foundation NEW per-domain service modules
// (`src/services/<domain>Api.ts`, one per feature slice) build on. It does
// NOT replace `services/api.js` -- existing pages keep using that import
// unchanged; do not migrate them as part of adding a new domain module. See
// `src/services/README.md` for the convention.

interface RetryableRequestConfig extends InternalAxiosRequestConfig {
  _retry?: boolean;
}

export const apiClient: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? '',
  timeout: 30000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

const REFRESH_URL = '/api/v1/auth/refresh';

apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as RetryableRequestConfig | undefined;

    // Session expired -- hub-api rotates the session cookie on a successful
    // /refresh; the browser applies the new Set-Cookie automatically, so
    // the retried request needs nothing attached by hand.
    //
    // The refresh call itself is never refreshed: a 401 from /auth/refresh means
    // the refresh cookie is dead, so it rejects straight to the catch below of
    // the request that triggered it (-> /login) instead of re-entering here and
    // refreshing without bound.
    const isRefreshCall = originalRequest?.url === REFRESH_URL;
    if (error.response?.status === 401 && originalRequest && !originalRequest._retry && !isRefreshCall) {
      originalRequest._retry = true;

      try {
        const refreshResponse = await apiClient.post<{ success: boolean }>(REFRESH_URL);
        if (refreshResponse.data.success) {
          return apiClient(originalRequest);
        }
      } catch (refreshError) {
        window.location.href = '/login';
        return Promise.reject(refreshError);
      }
    }

    return Promise.reject(error);
  },
);

export default apiClient;
