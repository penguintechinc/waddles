/**
 * Wire-level contract for the Community Access Token page: the REAL
 * `tokenApi` runs over a recording HTTP adapter (nothing about the client is
 * mocked), so this proves what the page actually shows for a real response.
 *
 * Regression: `tokenApi.*` returns the raw axios response, but the page used
 * to read `res.tokens` / `res.quota` / `res.token` straight off it (no
 * `.data`), so against the real client the token list was always empty and a
 * newly created token was never revealed. The page now reads `res.data.*`,
 * using the shapes hub-api actually returns
 * (`hub_api/blueprints/v1/access_token.py`): `GET /cats` ->
 * `{ tokens: [{ ..., created_by_name }], quota }`, `GET /scopes` ->
 * `{ scopes: [{ scope_key, ... }] }`, `POST /cats` -> `{ token }`.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterAll, beforeEach, describe, expect, it } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminCommunityTokens from '../AdminCommunityTokens';
import api from '../../../services/api';
import { recordRequests } from '../../../test/apiContract';

const rec = recordRequests(api);
afterAll(rec.restore);

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/tokens']}>
      <Routes>
        <Route path="/admin/:communityId/tokens" element={<AdminCommunityTokens />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  rec.calls.length = 0;
  rec.reply.data = {
    tokens: [{ id: 't1', name: 'Event bot', scopes: ['a:read'], created_by_name: 'Alice' }],
    quota: 5,
    scopes: [{ scope_key: 'misc:ping' }],
    token: 'cat_secret_value',
  };
});

describe('AdminCommunityTokens against the real tokenApi', () => {
  it('requests the token list and scope catalogue for the community', async () => {
    mount();
    await screen.findByText('Community Access Tokens');
    const urls = rec.calls.map((c) => `${c.method} ${c.url}`).sort();
    expect(urls).toEqual([
      'get /api/v1/admin/7/tokens/cats',
      'get /api/v1/admin/7/tokens/scopes',
    ]);
  });

  it('lists the tokens the API returned', async () => {
    mount();
    expect(await screen.findByText('Event bot')).toBeInTheDocument();
    expect(screen.getByText('Alice')).toBeInTheDocument();
  });

  it('completes the create flow end to end (scopes load, token created and revealed)', async () => {
    mount();
    await screen.findByText('Community Access Tokens');
    fireEvent.click(screen.getByRole('button', { name: /New Token/ }));
    fireEvent.change(screen.getByPlaceholderText(/Event bot, Welcome/), { target: { value: 'b' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /misc:ping/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Create Token' }));
    await waitFor(() => expect(rec.calls.some((c) => c.method === 'post')).toBe(true));
    expect(await screen.findByText('cat_secret_value')).toBeInTheDocument();
  });
});
