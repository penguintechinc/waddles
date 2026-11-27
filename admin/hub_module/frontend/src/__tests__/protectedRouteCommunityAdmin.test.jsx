/**
 * The admin gate is SCOPE-based: a membership holding an admin-surface scope enters, whatever its
 * role is called; role names (community-owner, owner, ...) carry no authority.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { ProtectedRoute } from '../App';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useAuth: vi.fn() };
});

/** Renders the admin gate for a user holding one community membership with `role` and `scopes`. */
function renderGate(role, scopes) {
  const user = { id: 1, roles: [], communities: [{ id: '00000005-0000-4000-8000-000000000000', role, scopes }] };
  useAuth.mockReturnValue({
    user,
    loading: false,
    hasRole: (r) => user.roles.includes(r),
    isAnalyticsConsumer: false,
  });
  render(
    <MemoryRouter initialEntries={['/admin/00000005-0000-4000-8000-000000000000']}>
      <Routes>
        <Route
          path="/admin/00000005-0000-4000-8000-000000000000"
          element={
            <ProtectedRoute requireCommunityAdmin>
              <div>admin panel</div>
            </ProtectedRoute>
          }
        />
        <Route path="/dashboard" element={<div>dashboard</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('ProtectedRoute requireCommunityAdmin', () => {
  it('lets a membership holding community:admin into the admin panel, whatever the role is called', () => {
    renderGate('Warden', ['community:admin']);
    expect(screen.getByText('admin panel')).toBeTruthy();
  });

  it('lets a viewer scope (community.members:view) in', () => {
    renderGate('member', ['community.members:view']);
    expect(screen.getByText('admin panel')).toBeTruthy();
  });

  it('denies a plain member with no admin-surface scope', () => {
    renderGate('member', []);
    expect(screen.getByText('dashboard')).toBeTruthy();
  });

  it('gives a role literally named community-owner no authority without scopes', () => {
    renderGate('community-owner', []);
    expect(screen.getByText('dashboard')).toBeTruthy();
  });

  it('ignores unrelated scopes', () => {
    renderGate('member', ['community.roles:view']);
    expect(screen.getByText('dashboard')).toBeTruthy();
  });
});
