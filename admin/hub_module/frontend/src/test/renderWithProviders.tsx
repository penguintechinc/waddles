import type { ReactElement } from 'react';
import { render, type RenderResult } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

export interface RenderOptions {
  /** Initial location, e.g. `/dashboard/community/7/members`. */
  route?: string;
  /** Route pattern the element is mounted at, e.g. `/dashboard/community/:id/members`. */
  path?: string;
}

/** Fresh, retry-free query client per render so tests never share cache or wait on backoff. */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
}

/** Renders `ui` inside a router + TanStack Query provider, mounted at `path`. */
export function renderWithProviders(ui: ReactElement, { route = '/', path = '*' }: RenderOptions = {}): RenderResult {
  const client = createTestQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
