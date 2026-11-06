/** AdminAIKnowledge: sources go through the served /api/v1/admin/ai-knowledge routes; settings/health are gated. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { apiClient } from '../../lib/apiClient';
import api from '../../services/api';
import { stubHttp } from '../../test/httpStub';
import AdminAIKnowledge from '../admin/AdminAIKnowledge';

const BASE = '/api/v1/admin/ai-knowledge/sources';
let http;
afterEach(() => {
  http?.restore();
  vi.restoreAllMocks();
});

const wire = { id: 4, source_name: 'Wiki', source_type: 'github_wiki', source_url: 'https://github.com/o/r', branch: 'main', docs_path: '/', refresh_interval: 'weekly', is_active: true, last_indexed_at: '2026-01-01T00:00:00Z', indexed_page_count: 2, index_errors: null };

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/ai-knowledge']}>
      <Routes>
        <Route path="/admin/:communityId/ai-knowledge" element={<AdminAIKnowledge />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('AdminAIKnowledge', () => {
  it('loads sources with ?communityId, renders them, and gates settings + health (no dead calls)', async () => {
    http = stubHttp([api, apiClient], { [`GET ${BASE}`]: { sources: [wire] } });
    mount();
    expect(await screen.findByText('Wiki')).toBeInTheDocument();
    expect(http.seen.map((r) => `${r.method} ${r.url}`)).toEqual([`GET ${BASE}`]);
    expect(http.seen[0].params).toEqual({ communityId: 7 });
    const gated = screen.getAllByTestId('not-available-yet');
    expect(gated.map((g) => g.getAttribute('data-feature'))).toEqual(['ai-knowledge-settings', 'ai-knowledge-health']);
    expect(screen.queryByText('Save Settings')).not.toBeInTheDocument();
  });

  it('shows a toast when the list cannot be loaded', async () => {
    http = stubHttp([api, apiClient], {});
    mount();
    expect(await screen.findByText('Failed to load AI knowledge data')).toBeInTheDocument();
  });

  it('reindexes via POST .../reindex and deletes via DELETE .../<id>', async () => {
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: { sources: [wire] },
      [`POST ${BASE}/4/reindex`]: { success: true },
      [`DELETE ${BASE}/4`]: { success: true },
    });
    mount();
    await screen.findByText('Wiki');
    fireEvent.click(screen.getByTitle(/re-?index/i));
    await waitFor(() => expect(http.seen.some((r) => r.method === 'POST' && r.url === `${BASE}/4/reindex`)).toBe(true));
    fireEvent.click(screen.getByTitle('Delete'));
    const dialog = within(await screen.findByRole('dialog'));
    fireEvent.click(dialog.getByRole('button', { name: /delete/i }));
    await waitFor(() => expect(http.seen.some((r) => r.method === 'DELETE' && r.url === `${BASE}/4`)).toBe(true));
  });

  it('adds a source with the snake_case wire body and edits via PUT', async () => {
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: { sources: [wire] },
      [`POST ${BASE}`]: { success: true },
      [`PUT ${BASE}/4`]: { success: true },
    });
    mount();
    await screen.findByText('Wiki');
    fireEvent.click(screen.getByRole('button', { name: /add source|add knowledge/i }));
    const dialog = within(await screen.findByRole('dialog'));
    fireEvent.change(dialog.getByPlaceholderText('https://github.com/owner/repo'), { target: { value: 'https://github.com/o/new' } });
    const nameInput = dialog.getAllByRole('textbox')[0];
    fireEvent.change(nameInput, { target: { value: 'New' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Add Source' }));
    await waitFor(() => expect(http.seen.some((r) => r.method === 'POST' && r.url === BASE)).toBe(true));
    expect(http.seen.find((r) => r.method === 'POST' && r.url === BASE).body).toMatchObject({ source_name: 'New', source_type: 'github_wiki', community_id: 7 });

    fireEvent.click(screen.getByTitle('Edit'));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(http.seen.some((r) => r.method === 'PUT' && r.url === `${BASE}/4`)).toBe(true));
  });
});
