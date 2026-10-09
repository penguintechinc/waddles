/**
 * Wire-level contract for the Personal Access Token page: the REAL `tokenApi`
 * runs over a recording HTTP adapter (nothing about the client is mocked).
 *
 * Regression: `tokenApi.*` returns the raw axios response, but the page used
 * to read `res.token` / `res.scopes` straight off it (no `.data`), so against
 * the real client an existing token was never shown and a newly created token
 * value was never revealed (the user can't copy it; it is not shown again).
 * The page now reads `res.data.*`, using the shapes hub-api actually returns
 * (`hub_api/blueprints/v1/access_token.py`): `GET /pat` -> `{ pat }`,
 * `GET /scopes` -> `{ scopes: [{ scope_key, ... }] }`, `POST /pat` -> `{ token }`.
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
    pat: { name: 'Deploy script', created_at: '2026-01-02T12:00:00Z', last_used_at: null, scope_ceiling: null },
    scopes: [{ scope_key: 'misc:ping' }],
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

  it('shows the existing token returned by the API', async () => {
    render(<PersonalAccessToken />);
    expect(await screen.findByText('Deploy script')).toBeInTheDocument();
  });

  it('offers the scope catalogue returned by the API in the create form', async () => {
    rec.reply.data = { pat: null, scopes: [{ scope_key: 'misc:ping' }] };
    render(<PersonalAccessToken />);
    fireEvent.click(await screen.findByRole('button', { name: /Create Token/ }));
    expect(screen.getByRole('checkbox', { name: /misc:ping/ })).toBeInTheDocument();
  });

  it('reveals the new token value after creating one', async () => {
    rec.reply.data = { pat: null, scopes: [] };
    render(<PersonalAccessToken />);
    fireEvent.click(await screen.findByRole('button', { name: /Create Token/ }));
    fireEvent.change(screen.getByPlaceholderText('e.g. My deploy script'), { target: { value: 'ci' } });
    rec.reply.data = { token: 'pat_secret_value' };
    fireEvent.click(screen.getAllByRole('button', { name: 'Create Token' }).pop());
    await waitFor(() => expect(rec.calls.some((c) => c.method === 'post')).toBe(true));
    expect(await screen.findByText('pat_secret_value')).toBeInTheDocument();
  });
});
