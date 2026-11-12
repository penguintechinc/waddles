import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import api from '../../../services/api';
import { apiClient } from '../../../lib/apiClient';
import { stubHttp } from '../../../test/httpStub';
import AdminStreamOverlays from '../AdminStreamOverlays';

const mockWidgets = [
  {
    widget_id: 'dj.music.album_now_playing',
    bundle_id: 'waddles.core.example.music',
    name: 'Album Cover & Track Info',
    description: 'Displays current album art, song title, and artist',
    category: 'music',
    default_width: 480,
    default_height: 240,
    output_kind: 'surface',
  },
  {
    widget_id: 'overlay.alerts.alert_box',
    bundle_id: 'waddles.core.overlay.alerts',
    name: 'Stream Alerts Box',
    description: 'Displays animated alerts for subs, follows, raids, and cheers',
    category: 'alerts',
    default_width: 600,
    default_height: 300,
    output_kind: 'sanitized_html',
  },
  {
    widget_id: 'overlay.galactic_avatars.stage_strip',
    bundle_id: 'waddles.core.overlay.galactic_avatars',
    name: 'Galactic Avatars Stage Strip',
    description: 'Bottom stage strip where viewer avatars roam, emote, and duel',
    category: 'avatars',
    default_width: 1920,
    default_height: 200,
    output_kind: 'sanitized_html',
  },
];

let http;
afterEach(() => {
  http?.restore();
  vi.restoreAllMocks();
});

describe('AdminStreamOverlays - Overlay Builder tab', () => {
  it('loads layout and widgets, adds component, updates coordinates, changes resolution and theme', async () => {
    let currentLayout = {
      resolution: '1080p',
      theme: 'transparent',
      components: [],
    };

    http = stubHttp([api, apiClient], {
      'GET /api/v1/admin/7/overlay': {
        overlay: { is_active: true, overlay_key: 'k'.repeat(64), overlayUrl: 'https://overlay.test/k' },
      },
      'GET /api/v1/admin/7/overlay/stats': { stats: { total_access_count: 0, daily: [] } },
      'GET /api/v1/admin/7/browser-sources': {
        success: true,
        sources: [
          { sourceType: 'scene', url: 'https://overlay.test/scene/tok123', isActive: true },
          { sourceType: 'media', url: 'https://overlay.test/media/tok123', isActive: true },
        ],
      },
      'GET /api/v1/admin/7/overlay/widgets': {
        success: true,
        widgets: mockWidgets,
      },
      'GET /api/v1/admin/7/overlay/layout': () => ({
        success: true,
        layout: currentLayout,
      }),
      'POST /api/v1/admin/7/overlay/layout/components': (req) => {
        const newComp = {
          instance_id: 'comp_test_1',
          widget_id: req.body.widget_id,
          x: req.body.x,
          y: req.body.y,
          width: req.body.width,
          height: req.body.height,
          z_index: req.body.z_index || 1,
          is_visible: req.body.is_visible !== false,
          props: {},
        };
        currentLayout = {
          ...currentLayout,
          components: [...currentLayout.components, newComp],
        };
        return { success: true, component: newComp };
      },
      'PATCH /api/v1/admin/7/overlay/layout/components/comp_test_1': (req) => {
        const existing = currentLayout.components[0];
        const updated = { ...existing, ...req.body };
        currentLayout = {
          ...currentLayout,
          components: [updated],
        };
        return { success: true, component: updated };
      },
      'POST /api/v1/admin/7/overlay/layout/theme': (req) => {
        currentLayout = {
          ...currentLayout,
          resolution: req.body.resolution || currentLayout.resolution,
          theme: req.body.theme || currentLayout.theme,
        };
        return { success: true, layout: currentLayout };
      },
      'DELETE /api/v1/admin/7/overlay/layout/components/comp_test_1': () => {
        currentLayout = {
          ...currentLayout,
          components: [],
        };
        return { success: true, message: 'removed' };
      },
      'DELETE /api/v1/admin/7/overlay/layout/clear': () => {
        currentLayout = {
          ...currentLayout,
          components: [],
        };
        return { success: true, message: 'cleared' };
      },
    });

    render(
      <MemoryRouter initialEntries={['/admin/7/stream-overlays']}>
        <Routes>
          <Route path="/admin/:communityId/stream-overlays" element={<AdminStreamOverlays />} />
        </Routes>
      </MemoryRouter>,
    );

    // Switch to Overlay Builder tab
    fireEvent.click(await screen.findByText('Overlay Builder'));

    // Verify widgets are listed
    expect(await screen.findByText('Album Cover & Track Info')).toBeDefined();
    expect(screen.getByText('Stream Alerts Box')).toBeDefined();
    expect(screen.getByText('Galactic Avatars Stage Strip')).toBeDefined();

    // Verify initial canvas resolution display
    expect(screen.getByText(/Canvas: 1920 × 1080/)).toBeDefined();

    // Add first widget to canvas
    const addButtons = screen.getAllByText('Add to Canvas');
    fireEvent.click(addButtons[0]);

    // Verify component appears in canvas/inspector
    await waitFor(() => {
      expect(screen.getByText('comp_test_1', { exact: false })).toBeDefined();
    });

    // Test resolution change to 4K
    const btn4k = screen.getByText('4K');
    fireEvent.click(btn4k);
    await waitFor(() => {
      expect(screen.getByText(/Canvas: 3840 × 2160/)).toBeDefined();
    });

    // Verify quick align
    const centerBtn = screen.getByText('Center');
    fireEvent.click(centerBtn);

    // Verify patch was called
    await waitFor(() => {
      const patchCall = http.seen.find((r) => r.method === 'PATCH');
      expect(patchCall).toBeDefined();
    });
  });
});

