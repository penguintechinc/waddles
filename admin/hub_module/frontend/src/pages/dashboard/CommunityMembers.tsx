import { useState, type FormEvent } from 'react';
import { useParams } from 'react-router-dom';
import { MemberRow } from '../../components/dashboard/MemberRow';
import { Button, ErrorState, LoadingState, PageHeader, QueryBoundary, TextField, cx } from '../../components/ui';
import { getErrorMessage } from '../../components/ui/errors';
import { useChangeMemberRole, useCommunityMembers, useCommunityRoles, useRemoveMember } from '../../hooks/useCommunityPanels';
import { useCommunityScopes } from '../../hooks/useCommunityScopes';
import { COMMUNITY_SCOPES } from '../../lib/communityScopes';
import { parseCommunityId } from '../../lib/communityId';
import type { MemberPage } from '../../services/communityMembersApi';

const PAGE_SIZE = 25;

/** Scope-gated roster: viewing needs `community.members:view`; role changes and removal have their own scopes. */
function MembersView({ communityId }: { communityId: string }) {
  const { has, isLoading, error: accessError, membership } = useCommunityScopes(communityId);
  const canView = has(COMMUNITY_SCOPES.membersView);
  const canChangeRole = has(COMMUNITY_SCOPES.membersRoles);
  const canRemove = has(COMMUNITY_SCOPES.membersRemove);

  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [roleId, setRoleId] = useState('');
  const [page, setPage] = useState(1);
  const query = useCommunityMembers(communityId, { page, limit: PAGE_SIZE, search, roleId }, canView);
  const roles = useCommunityRoles(communityId, canView);
  const roleList = roles.data ?? [];
  const changeRole = useChangeMemberRole(communityId);
  const remove = useRemoveMember(communityId);
  const actionError = changeRole.error ?? remove.error;

  const header = (
    <PageHeader
      title="Community Members"
      description={
        query.data ? `${query.data.total} ${query.data.total === 1 ? 'member' : 'members'} in this community` : undefined
      }
      breadcrumbs={[
        { label: 'Dashboard', to: '/dashboard' },
        { label: membership?.displayName ?? 'Community', to: `/dashboard/community/${communityId}` },
        { label: 'Members' },
      ]}
    />
  );

  if (isLoading) return <LoadingState label="Checking your access" />;
  if (accessError) return <ErrorState error={accessError} title="We could not check your access" />;
  if (!canView) {
    return (
      <div className="mx-auto max-w-4xl">
        {header}
        <p role="alert" data-testid="members-forbidden" className="card px-6 py-10 text-center text-fg-secondary">
          You do not have permission to view this community&apos;s member list.
        </p>
      </div>
    );
  }

  const onSearch = (event: FormEvent) => {
    event.preventDefault();
    setSearch(searchInput.trim());
    setPage(1);
  };
  const filters = [{ id: '', label: 'All Members' }, ...roleList.map((r) => ({ id: r.id, label: r.displayName }))];

  return (
    <div className="mx-auto max-w-4xl">
      {header}
      <div className="mb-6 flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <form onSubmit={onSearch} className="flex max-w-md flex-1 items-end gap-2" role="search">
          <TextField
            label="Search by username"
            hideLabel
            type="search"
            placeholder="Search by username..."
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            wrapperClassName="flex-1"
          />
          <Button type="submit" variant="primary">
            Search
          </Button>
        </form>
        <div role="group" aria-label="Filter by role" className="flex flex-wrap gap-2">
          {filters.map((filter) => (
            <button
              key={filter.id}
              type="button"
              aria-pressed={roleId === filter.id}
              onClick={() => {
                setRoleId(filter.id);
                setPage(1);
              }}
              className={cx(
                'rounded-lg px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus',
                roleId === filter.id ? 'bg-accent text-fg-inverse' : 'bg-surface-raised text-fg-secondary hover:text-fg',
              )}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </div>
      {roles.isError ? (
        <p role="alert" className="mb-4 text-danger-fg">
          {getErrorMessage(roles.error, 'We could not load the community roles')}
        </p>
      ) : null}
      {actionError ? (
        <p role="alert" data-testid="member-action-error" className="mb-4 text-danger-fg">
          {getErrorMessage(actionError, 'That change could not be saved')}
        </p>
      ) : null}
      <QueryBoundary
        query={query}
        loadingLabel="Loading members"
        errorTitle="We could not load members"
        isEmpty={(data: MemberPage) => data.members.length === 0}
        empty={
          <p className="card px-6 py-14 text-center text-fg-secondary" data-testid="members-empty">
            {search || roleId ? 'No members found matching your filters' : 'No members in this community yet'}
          </p>
        }
      >
        {(data) => (
          <>
            <ul className="grid gap-4 md:grid-cols-2" aria-label="Members">
              {data.members.map((member) => (
                <MemberRow
                  key={member.userId}
                  member={member}
                  roles={roleList}
                  busy={changeRole.isPending || remove.isPending}
                  {...(canChangeRole
                    ? {
                        onChangeRole: (newRoleId: string) => {
                          remove.reset();
                          changeRole.mutate({ userId: member.userId, roleId: newRoleId });
                        },
                      }
                    : {})}
                  {...(canRemove
                    ? {
                        onRemove: () => {
                          if (!window.confirm(`Remove ${member.username} from this community?`)) return;
                          changeRole.reset();
                          remove.mutate(member.userId);
                        },
                      }
                    : {})}
                />
              ))}
            </ul>
            {data.totalPages > 1 ? (
              <nav aria-label="Member pages" className="mt-6 flex items-center justify-between gap-3">
                <p className="text-sm text-fg-secondary">
                  Page {data.page} of {data.totalPages}
                </p>
                <div className="flex gap-2">
                  <Button disabled={data.page <= 1 || query.isPlaceholderData} onClick={() => setPage(data.page - 1)}>
                    Previous
                  </Button>
                  <Button disabled={data.page >= data.totalPages || query.isPlaceholderData} onClick={() => setPage(data.page + 1)}>
                    Next
                  </Button>
                </div>
              </nav>
            ) : null}
          </>
        )}
      </QueryBoundary>
    </div>
  );
}

/** Route wrapper: validates `:id` before any query runs. */
export default function CommunityMembers() {
  const { id } = useParams<{ id: string }>();
  const communityId = parseCommunityId(id);
  if (communityId === null) {
    return <ErrorState title="Unknown community" message="This members link is not valid." />;
  }
  return <MembersView communityId={communityId} />;
}
