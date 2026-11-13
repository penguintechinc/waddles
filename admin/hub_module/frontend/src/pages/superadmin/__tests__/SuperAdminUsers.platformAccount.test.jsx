/**
 * The bootstrap admin is a non-person PLATFORM account: the Users page badges it and disables
 * delete / revoke-super-admin for it, while person rows keep every control enabled.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import SuperAdminUsers from '../SuperAdminUsers';
import { superAdminApi, superadminTenantApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  default: { post: vi.fn() },
  superAdminApi: {
    listUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    deleteUser: vi.fn(),
    assignSuperAdminRole: vi.fn(),
    assignVendorRole: vi.fn(),
    setEmailVerification: vi.fn(),
  },
  superadminTenantApi: { list: vi.fn() },
}));

const users = [
  {
    id: 1,
    email: 'root@example.com',
    username: 'root',
    isActive: true,
    isSuperAdmin: true,
    isPlatformAccount: true,
    emailVerified: true,
  },
  {
    id: 2,
    email: 'person@example.com',
    username: 'person',
    isActive: true,
    isSuperAdmin: true,
    isPlatformAccount: false,
    emailVerified: true,
  },
];

describe('SuperAdminUsers platform account', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    superadminTenantApi.list.mockResolvedValue({ data: { tenants: [] } });
    superAdminApi.listUsers.mockResolvedValue({
      data: { success: true, users, pagination: { total: 2, totalPages: 1 } },
    });
  });

  it('badges the platform account once and disables its delete and demote controls', async () => {
    render(<SuperAdminUsers />);
    await waitFor(() => expect(screen.getAllByText('Platform account').length).toBe(1));
    expect(screen.getByTitle('Platform account cannot be deleted').disabled).toBe(true);
    expect(screen.getByTitle('Platform account cannot be demoted').disabled).toBe(true);
    // the person admin keeps working controls
    expect(screen.getByTitle('Delete user').disabled).toBe(false);
    expect(screen.getByTitle('Revoke super admin').disabled).toBe(false);
  });
});
