/**
 * AI Chatter config page: a 503 AI_CHATTER_UNAVAILABLE (backend not deployed) renders a neutral
 * status note, never the raw `service_unavailable` token or a red failure; a configured backend
 * renders the settings form from the `{success, config}` envelope.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

vi.mock('../../services/api', () => ({
  adminApi: { getAIChatterConfig: vi.fn(), updateAIChatterConfig: vi.fn() },
}));

import { adminApi } from '../../services/api';
import AdminAIChatterConfig from '../admin/AdminAIChatterConfig';

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/admin/c1/ai-chatter']}>
      <Routes>
        <Route path="/admin/:communityId/ai-chatter" element={<AdminAIChatterConfig />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('AdminAIChatterConfig', () => {
  it('503 AI_CHATTER_UNAVAILABLE -> neutral status, no raw token, no failure text', async () => {
    adminApi.getAIChatterConfig.mockRejectedValue({
      response: {
        status: 503,
        data: { success: false, error: { code: 'AI_CHATTER_UNAVAILABLE', message: 'x' } },
      },
    });
    renderPage();
    const note = await screen.findByRole('status');
    expect(note.textContent).toMatch(/isn't available on this deployment yet/);
    expect(document.body.textContent).not.toMatch(/service_unavailable|Failed to load/);
  });

  it('configured backend -> renders form from the {success, config} envelope', async () => {
    adminApi.getAIChatterConfig.mockResolvedValue({
      data: { success: true, config: { enabled: true, max_responses_per_window: 7 } },
    });
    renderPage();
    await waitFor(() => expect(screen.getByDisplayValue('7')).toBeTruthy());
    expect(screen.queryByRole('status')).toBeNull();
  });
});
