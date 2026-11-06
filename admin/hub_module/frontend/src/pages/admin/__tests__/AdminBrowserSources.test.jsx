/**
 * Browser sources are served by hub-api: AdminStreamOverlays and AdminHome call the real
 * (stubbed-at-HTTP) endpoints with no ServedFeature gate.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import api from '../../../services/api';
import { apiClient } from '../../../lib/apiClient';
import { stubHttp } from '../../../test/httpStub';
import { isFeatureServed } from '../../../lib/unservedFeatures';
import AdminStreamOverlays from '../AdminStreamOverlays';

const URL_BASE = 'https://overlay.example.test/a1b2c3d4e5f60718';
const sources = [
  { sourceType: 'ticker', url: `${URL_BASE}/ticker?key=k1`, isActive: true, createdAt: '2026-11-06T00:00:00Z' },
  { sourceType: 'media', url: `${URL_BASE}/media?key=k1`, isActive: true, createdAt: '2026-11-06T00:00:00Z' },
  { sourceType: 'general', url: `${URL_BASE}/full_screen?key=k1`, isActive: true, createdAt: '2026-11-06T00:00:00Z' },
];

let http;
afterEach(() => {
  http?.restore();
  vi.restoreAllMocks();
});

describe('browser-sources feature', () => {
  it('is no longer registered as unserved', () => {
    expect(isFeatureServed('browser-sources')).toBe(true);
  });

  it('AdminStreamOverlays renders the served URLs and regenerates then refetches', async () => {
    let regenerated = false;
    http = stubHttp([api, apiClient], {
      'GET /api/v1/admin/7/overlay': { overlay: { is_active: true, overlay_key: 'k'.repeat(64), overlayUrl: 'x' } },
      'GET /api/v1/admin/7/overlay/stats': { stats: { total_access_count: 0, daily: [] } },
      'GET /api/v1/admin/7/browser-sources': () => ({
        success: true,
        sources: regenerated ? sources.map((s) => ({ ...s, url: s.url.replace('k1', 'k2') })) : sources,
      }),
      'POST /api/v1/admin/7/browser-sources/regenerate': () => {
        regenerated = true;
        return { success: true, message: 'ok' };
      },
    });
    render(
      <MemoryRouter initialEntries={['/admin/7/stream-overlays']}>
        <Routes>
          <Route path="/admin/:communityId/stream-overlays" element={<AdminStreamOverlays />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByText('Individual Sources'));
    const inputs = await screen.findAllByLabelText('Browser source URL');
    expect(inputs.map((i) => i.value)).toEqual(sources.map((s) => s.url));
    expect(screen.queryByTestId('not-available-yet')).toBeNull();

    fireEvent.click(screen.getAllByTitle('Regenerate URL')[0]);
    await waitFor(() => expect(screen.getAllByLabelText('Browser source URL')[0].value).toContain('key=k2'));
    expect(http.seen.find((r) => r.method === 'POST').body).toEqual({ sourceType: 'ticker' });
  });
});
