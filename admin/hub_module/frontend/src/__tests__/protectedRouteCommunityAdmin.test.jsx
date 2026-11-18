/**
 * Regression: community owners were stored with role 'owner' while every gate checks
 * 'community-owner', so owners were redirected away from the admin panel. The gate accepts the
 * canonical role only (no 'owner' alias) and denies plain members.
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

/** Renders the admin gate for a user holding one community membership with `role`. */
function renderGate(role) {
  const user = { id: 1, roles: [], communities: [{ id: 5, role }] };
  useAuth.mockReturnValue({
    user,
    loading: false,
    hasRole: (r) => user.roles.includes(r),
    isAnalyticsConsumer: false,
  });
  render(
    <MemoryRouter initialEntries={['/admin/5']}>
      <Routes>
        <Route
          path="/admin/5"
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
  it('lets a community-owner into the admin panel', () => {
    renderGate('community-owner');
    expect(screen.getByText('admin panel')).toBeTruthy();
  });

  it('denies a plain member', () => {
    renderGate('member');
    expect(screen.getByText('dashboard')).toBeTruthy();
  });

  it('does not alias the non-canonical "owner" role', () => {
    renderGate('owner');
    expect(screen.getByText('dashboard')).toBeTruthy();
  });
});
