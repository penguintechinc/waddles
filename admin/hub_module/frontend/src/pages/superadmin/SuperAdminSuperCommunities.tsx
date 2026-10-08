import { useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useFeatureFlag } from '../../lib/useFeatureFlag';
import {
  platformCommunitiesApi,
  type PlatformCommunityDTO,
  type UpdatePlatformCommunityPayload,
} from '../../services/platformCommunitiesApi';

/**
 * Super-admin cross-tenant community management (wave-1 slice, post-S0).
 *
 * Data model (honored exactly, not re-derived): a community is the
 * Team/OU layer nested inside exactly ONE tenant -- 1 tenant -> N
 * communities, never N:M. The default tenant (tenant 0) is the shared
 * catch-all for individual customers, distinct from a private customer
 * org tenant.
 *
 * KNOWN GAP: `GET /api/v1/platform/communities` (`hub_api/blueprints/v1/
 * platform.py`) does not expose `tenantId`/`tenantName` on its DTO today,
 * even though `communities.tenant_id` is a real NOT NULL column
 * (`hub_api/services/schema.py`). This page therefore cannot group rows
 * under their owning tenant yet -- it lists communities platform-wide
 * (matching what the endpoint is actually capable of) and flags the gap
 * rather than fabricating a tenant column. See the PR description for the
 * proposed `CommunityDTO.tenantId`/`tenantName` follow-up.
 */

const communityKeys = {
  all: ['platform-communities'] as const,
  list: (page: number, search: string, isActive: boolean) =>
    ['platform-communities', 'list', page, search, isActive] as const,
};

const PAGE_SIZE = 25;

export default function SuperAdminSuperCommunities() {
  const enabled = useFeatureFlag('waddles.webui.super_communities');

  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const [isActive, setIsActive] = useState(true);
  const [editing, setEditing] = useState<PlatformCommunityDTO | null>(null);
  const [deactivating, setDeactivating] = useState<PlatformCommunityDTO | null>(null);

  const queryClient = useQueryClient();

  const { data, isLoading, isError, error } = useQuery({
    queryKey: communityKeys.list(page, search, isActive),
    queryFn: () => platformCommunitiesApi.list({ page, limit: PAGE_SIZE, search, isActive }),
    enabled,
    placeholderData: (previous) => previous,
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: UpdatePlatformCommunityPayload }) =>
      platformCommunitiesApi.update(id, payload),
    onSuccess: () => {
      console.debug('[SuperCommunities] UpdateCommunity success');
      void queryClient.invalidateQueries({ queryKey: communityKeys.all });
      setEditing(null);
    },
    onError: (err: unknown) => {
      console.debug('[SuperCommunities] UpdateCommunity error', { error: String(err) });
    },
  });

  const deactivateMutation = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason: string }) =>
      platformCommunitiesApi.deactivate(id, reason || undefined),
    onSuccess: () => {
      console.debug('[SuperCommunities] DeactivateCommunity success');
      void queryClient.invalidateQueries({ queryKey: communityKeys.all });
      setDeactivating(null);
    },
    onError: (err: unknown) => {
      console.debug('[SuperCommunities] DeactivateCommunity error', { error: String(err) });
    },
  });

  if (!enabled) {
    return (
      <div className="flex items-center justify-center min-h-[50vh] text-navy-400" role="status">
        Super communities are not yet available.
      </div>
    );
  }

  const handleSearchSubmit = (e: FormEvent) => {
    e.preventDefault();
    console.debug('[SuperCommunities] Search', { hasTerm: searchInput.length > 0 });
    setSearch(searchInput);
    setPage(1);
  };

  const communities = data?.communities ?? [];
  const pagination = data?.pagination;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold gradient-text">Super Communities</h1>
          <p className="text-sm text-navy-400 mt-1">
            Cross-tenant community management (platform:admin). Default tenant (tenant 0) is the
            shared catch-all for individual customers.
          </p>
        </div>
      </div>

      <div className="card p-4 mb-6">
        <form onSubmit={handleSearchSubmit} className="flex flex-wrap gap-4">
          <input
            type="text"
            placeholder="Search communities..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            className="input flex-1 min-w-[200px]"
            aria-label="Search communities"
          />
          <select
            value={isActive ? 'true' : 'false'}
            onChange={(e) => {
              setIsActive(e.target.value === 'true');
              setPage(1);
            }}
            className="input w-40"
            aria-label="Filter by status"
          >
            <option value="true">Active</option>
            <option value="false">Inactive</option>
          </select>
          <button type="submit" className="btn btn-secondary">
            Search
          </button>
        </form>
      </div>

      {isError && (
        <div
          className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 mb-6"
          role="alert"
        >
          {error instanceof Error ? error.message : 'Failed to load communities'}
        </div>
      )}

      <div className="card overflow-hidden">
        <table>
          <thead>
            <tr>
              <th>Community</th>
              <th>Platform</th>
              <th>Members</th>
              <th>Visibility</th>
              <th>Status</th>
              <th>Created</th>
              <th className="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td colSpan={7} className="p-8 text-center">
                  <div
                    className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400 mx-auto"
                    role="status"
                    aria-label="Loading communities"
                  />
                </td>
              </tr>
            ) : communities.length === 0 ? (
              <tr>
                <td colSpan={7} className="p-8 text-center text-navy-400">
                  No communities found
                </td>
              </tr>
            ) : (
              communities.map((community) => (
                <tr key={community.id}>
                  <td>
                    <div className="font-medium text-sky-100">
                      {community.displayName ?? community.name ?? `#${community.id}`}
                    </div>
                    <div className="text-sm text-navy-400">{community.name}</div>
                  </td>
                  <td className="text-navy-300">{community.primaryPlatform ?? '—'}</td>
                  <td>{community.memberCount}</td>
                  <td>
                    <span className={`badge ${community.isPublic ? 'badge-green' : 'badge-gray'}`}>
                      {community.isPublic ? 'Public' : 'Private'}
                    </span>
                  </td>
                  <td>
                    <span className={`badge ${community.isActive ? 'badge-green' : 'badge-red'}`}>
                      {community.isActive ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="text-sm text-navy-400">
                    {community.createdAt ? new Date(community.createdAt).toLocaleDateString() : '—'}
                  </td>
                  <td className="text-right">
                    <div className="flex justify-end gap-2">
                      <button
                        type="button"
                        onClick={() => setEditing(community)}
                        className="text-sky-400 hover:text-sky-300 text-sm focus:outline-none focus:ring-2 focus:ring-sky-500 rounded"
                        aria-label={`Edit ${community.displayName ?? community.name ?? community.id}`}
                      >
                        Edit
                      </button>
                      {community.isActive && (
                        <button
                          type="button"
                          onClick={() => setDeactivating(community)}
                          className="text-red-400 hover:text-red-300 text-sm focus:outline-none focus:ring-2 focus:ring-red-500 rounded"
                          aria-label={`Deactivate ${community.displayName ?? community.name ?? community.id}`}
                        >
                          Deactivate
                        </button>
                      )}
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
              Showing {(page - 1) * PAGE_SIZE + 1} to{' '}
              {Math.min(page * PAGE_SIZE, pagination.total)} of {pagination.total}
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Previous
              </button>
              <button
                type="button"
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

      {editing && (
        <EditCommunityModal
          community={editing}
          saving={updateMutation.isPending}
          onClose={() => setEditing(null)}
          onSave={(payload) => updateMutation.mutate({ id: editing.id, payload })}
        />
      )}

      {deactivating && (
        <DeactivateCommunityModal
          community={deactivating}
          deactivating={deactivateMutation.isPending}
          onClose={() => setDeactivating(null)}
          onConfirm={(reason) => deactivateMutation.mutate({ id: deactivating.id, reason })}
        />
      )}
    </div>
  );
}

interface EditCommunityModalProps {
  community: PlatformCommunityDTO;
  saving: boolean;
  onClose: () => void;
  onSave: (payload: UpdatePlatformCommunityPayload) => void;
}

function EditCommunityModal({ community, saving, onClose, onSave }: EditCommunityModalProps) {
  const [displayName, setDisplayName] = useState(community.displayName ?? '');
  const [description, setDescription] = useState(community.description ?? '');
  const [isPublic, setIsPublic] = useState(community.isPublic);
  const [isActive, setIsActive] = useState(community.isActive);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    console.debug('[SuperCommunities] SubmitEdit', { communityId: community.id });
    onSave({ displayName, description, isPublic, isActive });
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50" role="dialog" aria-modal="true">
      <div className="bg-navy-900 rounded-xl shadow-xl max-w-md w-full mx-4 border border-navy-700">
        <div className="p-6 border-b border-navy-700">
          <h2 className="text-xl font-semibold text-sky-100">Edit Community</h2>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="p-6 space-y-4">
            <div>
              <label htmlFor="displayName" className="block text-sm font-medium text-sky-200 mb-1">
                Display Name
              </label>
              <input
                id="displayName"
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="input w-full"
              />
            </div>
            <div>
              <label htmlFor="description" className="block text-sm font-medium text-sky-200 mb-1">
                Description
              </label>
              <textarea
                id="description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="input w-full"
                rows={3}
              />
            </div>
            <div className="flex gap-4 flex-wrap">
              <label className="flex items-center gap-2 text-sky-200">
                <input
                  type="checkbox"
                  checked={isActive}
                  onChange={(e) => setIsActive(e.target.checked)}
                  className="w-4 h-4 rounded bg-navy-800 border-navy-600"
                />
                <span className="text-sm">Active</span>
              </label>
              <label className="flex items-center gap-2 text-sky-200">
                <input
                  type="checkbox"
                  checked={isPublic}
                  onChange={(e) => setIsPublic(e.target.checked)}
                  className="w-4 h-4 rounded bg-navy-800 border-navy-600"
                />
                <span className="text-sm">Public</span>
              </label>
            </div>
          </div>
          <div className="p-6 border-t border-navy-700 flex justify-end gap-3">
            <button type="button" onClick={onClose} className="btn btn-secondary">
              Cancel
            </button>
            <button type="submit" disabled={saving} className="btn btn-primary">
              {saving ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

interface DeactivateCommunityModalProps {
  community: PlatformCommunityDTO;
  deactivating: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => void;
}

function DeactivateCommunityModal({
  community,
  deactivating,
  onClose,
  onConfirm,
}: DeactivateCommunityModalProps) {
  const [confirmName, setConfirmName] = useState('');
  const [reason, setReason] = useState('');
  const expectedName = community.name ?? '';

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (confirmName !== expectedName) return;
    console.debug('[SuperCommunities] SubmitDeactivate', { communityId: community.id });
    onConfirm(reason);
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50" role="dialog" aria-modal="true">
      <div className="bg-navy-900 rounded-xl shadow-xl max-w-md w-full mx-4 border border-navy-700">
        <div className="p-6 border-b border-navy-700">
          <h2 className="text-xl font-semibold text-red-400">Deactivate Community</h2>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="p-6 space-y-4">
            <div className="p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm">
              This deactivates the community platform-wide. It can be reactivated later via Edit.
            </div>
            <p className="text-sky-100">
              To confirm, type the community name:{' '}
              <strong className="text-red-400">{expectedName}</strong>
            </p>
            <input
              type="text"
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              className="input w-full"
              placeholder="Type community name to confirm"
              aria-label="Confirm community name"
            />
            <div>
              <label htmlFor="reason" className="block text-sm font-medium text-sky-200 mb-1">
                Reason (optional)
              </label>
              <input
                id="reason"
                type="text"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                className="input w-full"
              />
            </div>
          </div>
          <div className="p-6 border-t border-navy-700 flex justify-end gap-3">
            <button type="button" onClick={onClose} className="btn btn-secondary">
              Cancel
            </button>
            <button
              type="submit"
              disabled={deactivating || confirmName !== expectedName}
              className="btn btn-danger disabled:opacity-50"
            >
              {deactivating ? 'Deactivating...' : 'Deactivate Community'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
