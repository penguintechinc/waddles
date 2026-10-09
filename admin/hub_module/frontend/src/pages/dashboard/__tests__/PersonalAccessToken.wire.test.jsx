/**
 * Wire-level contract for the Personal Access Token page: the REAL `tokenApi`
 * runs over a recording HTTP adapter (nothing about the client is mocked).
 *
 * KNOWN DEFECT: `tokenApi.*` returns the raw axios response, but the page
 * reads `res.token` / `res.scopes` straight off it (no `.data`), so against
 * the real client an existing token is never shown and a newly created token
 * value is never revealed (the user can't copy it; it is not shown again).
 * `it.fails` goes red once that is fixed -- then convert these to plain `it`.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterAll, beforeEach, describe, expect, it } from 'vitest';

import PersonalAccessToken from '../PersonalAccessToken';
import api from '../../../services/api';
import { recordRequests } from '../../../test/apiContract';

const rec = recordRequests(api);
afterAll(rec.restore);

beforeEach(() => {
  rec.calls.length = 0;
  rec.reply.data = {
    token: { name: 'Deploy script', created_at: '2026-01-02T12:00:00Z', last_used_at: null, scope_ceiling: null },
    scopes: [{ key: 'misc:ping' }],
  };
});

describe('PersonalAccessToken against the real tokenApi', () => {
  it('requests the PAT and the scope catalogue', async () => {
    render(<PersonalAccessToken />);
    await screen.findByText('Personal Access Token');
    expect(rec.calls.map((c) => `${c.method} ${c.url}`).sort()).toEqual([
      'get /api/v1/user/tokens/pat',
      'get /api/v1/user/tokens/scopes',
    ]);
  });

  it.fails('shows the existing token returned by the API', async () => {
    render(<PersonalAccessToken />);
    expect(await screen.findByText('Deploy script')).toBeInTheDocument();
  });

  it.fails('reveals the new token value after creating one', async () => {
    rec.reply.data = { token: null, scopes: [] };
    render(<PersonalAccessToken />);
    fireEvent.click(await screen.findByRole('button', { name: /Create Token/ }));
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: 'ci' } });
    rec.reply.data = { token: 'pat_secret_value' };
    fireEvent.click(screen.getAllByRole('button', { name: 'Create Token' }).pop());
    await waitFor(() => expect(rec.calls.some((c) => c.method === 'post')).toBe(true));
    expect(await screen.findByText('pat_secret_value')).toBeInTheDocument();
  });
});
