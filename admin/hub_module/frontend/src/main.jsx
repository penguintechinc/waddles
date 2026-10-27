import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { AuthProvider } from './contexts/AuthContext';
import { SocketProvider } from './contexts/SocketContext';
import { CookieConsentProvider } from './contexts/CookieConsentContext';
import CookieBanner from './components/CookieBanner';
import CookiePreferencesModal from './components/CookiePreferencesModal';
import { queryClient } from './lib/queryClient';
import './index.css';

// TanStack Query wraps the whole app (S0 foundation) so any new per-domain
// service module (`src/services/<domain>Api.ts`) can use `useQuery`/
// `useMutation` without further provider setup. Existing pages are
// unaffected -- they keep calling raw axios via `services/api.js` directly,
// which TanStack Query has no opinion about.
ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <CookieConsentProvider>
        <AuthProvider>
          <SocketProvider>
            <BrowserRouter>
              <App />
              <CookieBanner />
              <CookiePreferencesModal />
            </BrowserRouter>
          </SocketProvider>
        </AuthProvider>
      </CookieConsentProvider>
    </QueryClientProvider>
  </React.StrictMode>
);
