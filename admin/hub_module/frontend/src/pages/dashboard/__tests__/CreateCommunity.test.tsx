/**
 * Community creation. The legacy `POST /communities/create` is not served; the identical-shape
 * `POST /superadmin/communities` is, for platform admins. Everyone else gets "not available yet".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import CreateCommunity from '../CreateCommunity';
import { toCreatePayload } from '../../../services/communityAdminApi';
import { apiClient } from '../../../lib/apiClient';
import { createTestQueryClient } from '../../../test/renderWithProviders';
import { httpError, ok } from '../../../test/api';

const auth = vi.hoisted(() => ({ isSuperAdmin: true }));

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'tux' }, isAuthenticated: true, isSuperAdmin: auth.isSuperAdmin, refreshUser: vi.fn() }),
}));

function renderPage() {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter initialEntries={['/communities/create']}>
        <Routes>
          <Route path="/communities/create" element={<CreateCommunity />} />
          <Route path="/dashboard/community/:id" element={<div>community dashboard</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('CreateCommunity', () => {
  beforeEach(() => {
    auth.isSuperAdmin = true;
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it('creates on the served super-admin route and opens the new community', async () => {
    const post = vi
      .spyOn(apiClient, 'post')
      .mockResolvedValue(ok({ success: true, community: { id: 42, name: 'penguin-pals', displayName: 'Penguin Pals' } }, 201));
    renderPage();
    fireEvent.change(screen.getByTestId('community-name-input'), { target: { value: 'Penguin Pals' } });
    fireEvent.click(screen.getByRole('button', { name: /create/i }));
    expect(await screen.findByText('community dashboard')).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
    const [url, body] = post.mock.calls[0]!;
    expect(url).toBe('/api/v1/superadmin/communities');
    expect(body).toMatchObject({ name: 'penguin-pals', platform: 'discord', isPublic: true, communityType: 'creator' });
    expect(post.mock.calls.map(([u]) => u)).not.toContain('/api/v1/communities/create');
  });

  it('shows the server error and stays on the form', async () => {
    vi.spyOn(apiClient, 'post').mockRejectedValue(httpError(409, { success: false, error: { code: 'CONFLICT', message: 'A community with that name already exists' } }));
    renderPage();
    fireEvent.change(screen.getByTestId('community-name-input'), { target: { value: 'dupe' } });
    fireEvent.click(screen.getByRole('button', { name: /create/i }));
    expect(await screen.findByTestId('community-form-error')).toHaveTextContent('A community with that name already exists');
    expect(screen.queryByText('community dashboard')).not.toBeInTheDocument();
  });

  it('never posts a blank name', () => {
    const post = vi.spyOn(apiClient, 'post');
    renderPage();
    // The form strips invalid characters, so this leaves the required name field empty.
    fireEvent.change(screen.getByTestId('community-name-input'), { target: { value: '!!!' } });
    expect(screen.getByTestId('community-name-input')).toHaveValue('');
    fireEvent.click(screen.getByTestId('create-community-submit'));
    expect(post).not.toHaveBeenCalled();
  });

  it('tells ordinary users self-service creation is not available yet, without any request', () => {
    auth.isSuperAdmin = false;
    const post = vi.spyOn(apiClient, 'post');
    const get = vi.spyOn(apiClient, 'get');
    renderPage();
    expect(screen.getByTestId('not-available-yet')).toHaveAttribute('data-feature', 'community-self-create');
    expect(screen.getByText(/only be created by platform administrators/i)).toBeInTheDocument();
    expect(screen.queryByTestId('community-name-input')).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
  });
});

describe('toCreatePayload', () => {
  it('omits blank optionals and keeps booleans', () => {
    expect(toCreatePayload({ name: 'x', platform: '', displayName: ' ', isPublic: false, communityType: 'gaming' }, false)).toEqual({
      name: 'x',
      platform: 'discord',
      isPublic: false,
      communityType: 'gaming',
    });
  });

  it('only honours the owner fields for super admins', () => {
    const form = { name: 'x', platform: 'twitch', ownerId: ' 55 ', ownerName: 'Owner' };
    expect(toCreatePayload(form, true)).toMatchObject({ ownerId: '55', ownerName: 'Owner' });
    expect(toCreatePayload(form, false)).not.toHaveProperty('ownerId');
    expect(toCreatePayload(form, false)).not.toHaveProperty('ownerName');
  });

  it('treats non-object form values as empty and rejects them', () => {
    expect(() => toCreatePayload(null, true)).toThrow('Community name is required');
  });
});
