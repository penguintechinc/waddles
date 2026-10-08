import { useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import type { FormField } from '@penguintechinc/react-libs';
import { WADDLES_GOLD_COLORS } from '../../theme/waddlebotTheme';
import { superTenantApi } from '../../services/superTenantApi';
import type {
  CreateTenantPayload,
  TenantDTO,
  UpdateTenantPayload,
} from '../../services/superTenantApi';
import { useFeatureFlag } from '../../lib/useFeatureFlag';

const LIMIT = 25;

const tenantKeys = {
  all: ['superadmin', 'tenants'] as const,
  list: (page: number, search: string) => ['superadmin', 'tenants', page, search] as const,
};

function getErrorMessage(err: unknown, fallback: string): string {
  if (axios.isAxiosError<{ error?: { message?: string } }>(err)) {
    return err.response?.data?.error?.message ?? fallback;
  }
  return fallback;
}

function SuperAdminTenants() {
  const enabled = useFeatureFlag('waddles.webui.super_tenants');
  const queryClient = useQueryClient();

  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [editingTenant, setEditingTenant] = useState<TenantDTO | null>(null);
  const [deactivatingTenant, setDeactivatingTenant] = useState<TenantDTO | null>(null);
  const [confirmSlug, setConfirmSlug] = useState('');

  const listQuery = useQuery({
    queryKey: tenantKeys.list(page, search),
    queryFn: async () => {
      const params: { page: number; limit: number; search?: string } = { page, limit: LIMIT };
      if (search) params.search = search;
      console.debug('[SuperTenants] List', { page, hasSearch: Boolean(search) });
      const response = await superTenantApi.list(params);
      return response.data;
    },
    enabled,
  });

  const createMutation = useMutation({
    mutationFn: (payload: CreateTenantPayload) => superTenantApi.create(payload),
    onSuccess: (_response, payload) => {
      console.debug('[SuperTenants] Create', { slug: payload.slug });
      setCreateOpen(false);
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: tenantKeys.all });
    },
    onError: (err: unknown) => {
      setActionError(getErrorMessage(err, 'Failed to create tenant'));
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: UpdateTenantPayload }) =>
      superTenantApi.update(id, data),
    onSuccess: (_response, variables) => {
      console.debug('[SuperTenants] Update', { tenantId: variables.id });
      setEditingTenant(null);
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: tenantKeys.all });
    },
    onError: (err: unknown) => {
      setActionError(getErrorMessage(err, 'Failed to update tenant'));
    },
  });

  const deactivateMutation = useMutation({
    mutationFn: (id: number) => superTenantApi.deactivate(id),
    onSuccess: (_response, id) => {
      console.debug('[SuperTenants] Deactivate', { tenantId: id });
      setDeactivatingTenant(null);
      setConfirmSlug('');
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: tenantKeys.all });
    },
    onError: (err: unknown) => {
      setActionError(getErrorMessage(err, 'Failed to deactivate tenant'));
    },
  });

  const handleSearchSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setPage(1);
    setSearch(searchInput.trim());
  };

  const createFields = useMemo<FormField[]>(
    () => [
      {
        name: 'slug',
        type: 'text',
        label: 'Slug',
        placeholder: 'my-tenant',
        required: true,
        defaultValue: '',
      },
      {
        name: 'displayName',
        type: 'text',
        label: 'Display Name',
        placeholder: 'My Tenant',
        required: true,
        defaultValue: '',
      },
      {
        name: 'description',
        type: 'textarea',
        label: 'Description',
        placeholder: 'Optional description',
        defaultValue: '',
      },
      {
        name: 'seatLimit',
        type: 'number',
        label: 'Seat Limit',
        placeholder: 'Leave blank for unlimited',
        min: 1,
        defaultValue: '',
      },
    ],
    [],
  );

  const editFields = useMemo<FormField[]>(
    () => [
      {
        name: 'displayName',
        type: 'text',
        label: 'Display Name',
        defaultValue: editingTenant?.displayName ?? '',
      },
      {
        name: 'description',
        type: 'textarea',
        label: 'Description',
        defaultValue: editingTenant?.description ?? '',
      },
      {
        name: 'logoUrl',
        type: 'url',
        label: 'Logo URL',
        placeholder: 'https://example.com/logo.png',
        defaultValue: editingTenant?.logoUrl ?? '',
      },
      {
        name: 'seatLimit',
        type: 'number',
        label: 'Seat Limit',
        placeholder: 'Leave blank for unlimited',
        min: 1,
        defaultValue: editingTenant?.seatLimit ?? '',
      },
      {
        name: 'isActive',
        type: 'checkbox',
        label: 'Active',
        defaultValue: editingTenant?.isActive ?? true,
      },
    ],
    [editingTenant],
  );

  if (!enabled) {
    return (
      <div className="flex items-center justify-center min-h-[50vh] text-navy-400" role="status">
        Tenant management is not yet available.
      </div>
    );
  }

  const handleCreateSubmit = async (formData: Record<string, unknown>): Promise<void> => {
    const rawSlug = String(formData.slug ?? '').trim().toLowerCase().replace(/[^a-z0-9-]/g, '');
    const rawSeatLimit = String(formData.seatLimit ?? '').trim();
    await createMutation.mutateAsync({
      slug: rawSlug,
      displayName: String(formData.displayName ?? '').trim(),
      description: formData.description ? String(formData.description) : null,
      seatLimit: rawSeatLimit !== '' ? Number.parseInt(rawSeatLimit, 10) : null,
    });
  };

  const handleEditSubmit = async (formData: Record<string, unknown>): Promise<void> => {
    if (!editingTenant) return;
    const rawSeatLimit = String(formData.seatLimit ?? '').trim();
    await updateMutation.mutateAsync({
      id: editingTenant.id,
      data: {
        displayName: String(formData.displayName ?? '').trim(),
        description: formData.description ? String(formData.description) : null,
        logoUrl: formData.logoUrl ? String(formData.logoUrl) : null,
        seatLimit: rawSeatLimit !== '' ? Number.parseInt(rawSeatLimit, 10) : null,
        isActive: Boolean(formData.isActive),
      },
    });
  };

  const handleDeactivateSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!deactivatingTenant || confirmSlug !== deactivatingTenant.slug) return;
    deactivateMutation.mutate(deactivatingTenant.id);
  };

  const tenants = listQuery.data?.tenants ?? [];
  const pagination = listQuery.data?.pagination;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-sky-100">Manage Tenants</h1>
        <button
          onClick={() => setCreateOpen(true)}
          className="btn btn-primary"
          aria-label="Create tenant"
        >
          + Create Tenant
        </button>
      </div>

      <div className="card p-4 mb-6">
        <form onSubmit={handleSearchSubmit} className="flex gap-4">
          <input
            type="text"
            placeholder="Search tenants..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            className="input flex-1"
            aria-label="Search tenants"
          />
          <button type="submit" className="btn btn-secondary">
            Search
          </button>
        </form>
      </div>

      {(listQuery.isError || actionError) && (
        <div className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 mb-6">
          {actionError ?? getErrorMessage(listQuery.error, 'Failed to load tenants')}
        </div>
      )}

      <div className="bg-navy-800 rounded-xl border border-navy-700 overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-navy-700">
              <th className="text-navy-400 text-xs uppercase px-4 py-3 text-left">Slug</th>
              <th className="text-navy-400 text-xs uppercase px-4 py-3 text-left">Display Name</th>
              <th className="text-navy-400 text-xs uppercase px-4 py-3 text-left">Type</th>
              <th className="text-navy-400 text-xs uppercase px-4 py-3 text-left">Status</th>
              <th className="text-navy-400 text-xs uppercase px-4 py-3 text-left">Seat Limit</th>
              <th className="text-navy-400 text-xs uppercase px-4 py-3 text-left">Created</th>
              <th className="text-navy-400 text-xs uppercase px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {listQuery.isLoading ? (
              <tr>
                <td colSpan={7} className="p-8 text-center">
                  <div
                    className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400 mx-auto"
                    role="status"
                    aria-label="Loading tenants"
                  />
                </td>
              </tr>
            ) : tenants.length === 0 ? (
              <tr>
                <td colSpan={7} className="p-8 text-center text-navy-400">
                  No tenants found
                </td>
              </tr>
            ) : (
              tenants.map((tenant) => (
                <tr
                  key={tenant.id}
                  className="border-b border-navy-700 hover:bg-navy-700/50 transition-colors"
                >
                  <td className="px-4 py-3">
                    <span className="font-mono text-sm text-sky-200">{tenant.slug}</span>
                  </td>
                  <td className="px-4 py-3 font-medium text-sky-100">{tenant.displayName}</td>
                  <td className="px-4 py-3">
                    {tenant.isGlobal ? (
                      <span
                        className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gold-500/20 text-gold-400"
                        title="Tenant 0 -- shared catch-all tenant for individual/free customers. Not a normal customer org."
                      >
                        Default (Shared)
                      </span>
                    ) : (
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-navy-700 text-navy-400">
                        Customer Org
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {tenant.isActive ? (
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400">
                        Active
                      </span>
                    ) : (
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-red-500/20 text-red-400">
                        Inactive
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-sm text-navy-300">
                    {tenant.seatLimit ?? <span className="text-navy-500">Unlimited</span>}
                  </td>
                  <td className="px-4 py-3 text-sm text-navy-400">
                    {tenant.createdAt ? new Date(tenant.createdAt).toLocaleDateString() : '—'}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => setEditingTenant(tenant)}
                        className="text-sky-400 hover:text-sky-300 text-sm"
                        aria-label={`Edit ${tenant.displayName}`}
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => setDeactivatingTenant(tenant)}
                        disabled={tenant.isGlobal}
                        className="text-red-400 hover:text-red-300 text-sm disabled:opacity-30 disabled:cursor-not-allowed"
                        title={
                          tenant.isGlobal
                            ? 'Cannot deactivate the default (shared) tenant'
                            : 'Deactivate tenant'
                        }
                        aria-label={`Deactivate ${tenant.displayName}`}
                      >
                        Deactivate
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>

        {pagination && pagination.totalPages > 1 && (
          <div className="flex items-center justify-between p-4 border-t border-navy-700">
            <div className="text-sm text-navy-400">
              Showing {(page - 1) * LIMIT + 1} to {Math.min(page * LIMIT, pagination.total)} of{' '}
              {pagination.total}
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Previous
              </button>
              <button
                onClick={() => setPage((p) => Math.min(pagination.totalPages, p + 1))}
                disabled={page === pagination.totalPages}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>

      <FormModalBuilder
        title="Create Tenant"
        fields={createFields}
        isOpen={createOpen}
        onClose={() => setCreateOpen(false)}
        onSubmit={handleCreateSubmit}
        submitButtonText="Create"
        cancelButtonText="Cancel"
        themeMode="dark"
        colors={WADDLES_GOLD_COLORS}
      />

      <FormModalBuilder
        title={`Edit Tenant${editingTenant ? ` — ${editingTenant.slug}` : ''}`}
        fields={editFields}
        isOpen={editingTenant !== null}
        onClose={() => setEditingTenant(null)}
        onSubmit={handleEditSubmit}
        submitButtonText="Save Changes"
        cancelButtonText="Cancel"
        themeMode="dark"
        colors={WADDLES_GOLD_COLORS}
      />

      {deactivatingTenant && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
          <div className="bg-navy-900 rounded-xl shadow-xl max-w-md w-full mx-4 border border-navy-700">
            <div className="p-6 border-b border-navy-700">
              <h2 className="text-xl font-semibold text-red-400">Deactivate Tenant</h2>
            </div>
            <form onSubmit={handleDeactivateSubmit}>
              <div className="p-6 space-y-4">
                <div className="p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm">
                  This will deactivate the tenant and suspend access for all its members.
                </div>
                <p className="text-sky-100">
                  To confirm, type the tenant slug:{' '}
                  <strong className="text-red-400">{deactivatingTenant.slug}</strong>
                </p>
                <input
                  type="text"
                  value={confirmSlug}
                  onChange={(e) => setConfirmSlug(e.target.value)}
                  className="input w-full font-mono"
                  placeholder="Type tenant slug to confirm"
                  aria-label="Confirm tenant slug"
                />
              </div>
              <div className="p-6 border-t border-navy-700 flex justify-end gap-3">
                <button
                  type="button"
                  onClick={() => {
                    setDeactivatingTenant(null);
                    setConfirmSlug('');
                  }}
                  className="btn btn-secondary"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={deactivateMutation.isPending || confirmSlug !== deactivatingTenant.slug}
                  className="btn btn-danger disabled:opacity-50"
                >
                  {deactivateMutation.isPending ? 'Deactivating...' : 'Deactivate Tenant'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default SuperAdminTenants;
