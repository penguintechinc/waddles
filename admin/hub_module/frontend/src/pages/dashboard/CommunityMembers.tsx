import { useState, type FormEvent } from 'react';
import { useParams } from 'react-router-dom';
import { MemberRow } from '../../components/dashboard/MemberRow';
import { COMMUNITY_ROLE_FILTERS, isCommunityAdminRole } from '../../components/dashboard/communityRoles';
import { Button, ErrorState, LoadingState, NotAvailableYet, PageHeader, QueryBoundary, TextField, cx } from '../../components/ui';
import { useAuthView } from '../../contexts/authView';
import { useCommunityMembers } from '../../hooks/useCommunityPanels';
import { useMyCommunities } from '../../hooks/useMyCommunities';
import type { MemberPage } from '../../services/communityMembersApi';

const PAGE_SIZE = 25;

/** Roster for community admins/moderators; everyone else gets an honest "not available yet". */
function MembersView({ communityId }: { communityId: number }) {
  const { isSuperAdmin } = useAuthView();
  const mine = useMyCommunities();
  const membership = mine.data?.find((c) => c.id === communityId);
  const canView = isSuperAdmin || isCommunityAdminRole(membership?.role);

  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [role, setRole] = useState('');
  const [page, setPage] = useState(1);
  const query = useCommunityMembers(communityId, { page, limit: PAGE_SIZE, search, role }, canView);

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

  if (mine.isPending) return <LoadingState label="Checking your access" />;
  if (mine.isError) {
    return <ErrorState error={mine.error} title="We could not check your access" onRetry={() => void mine.refetch()} />;
  }
  if (!canView) {
    return (
      <div className="mx-auto max-w-4xl">
        {header}
        <NotAvailableYet
          feature="member-roster"
          description="The member list is only available to community owners, admins and moderators for now."
        />
      </div>
    );
  }

  const onSearch = (event: FormEvent) => {
    event.preventDefault();
    setSearch(searchInput.trim());
    setPage(1);
  };

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
          {COMMUNITY_ROLE_FILTERS.map((filter) => (
            <button
              key={filter.value}
              type="button"
              aria-pressed={role === filter.value}
              onClick={() => {
                setRole(filter.value);
                setPage(1);
              }}
              className={cx(
                'rounded-lg px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus',
                role === filter.value ? 'bg-accent text-fg-inverse' : 'bg-surface-raised text-fg-secondary hover:text-fg',
              )}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </div>
      <QueryBoundary
        query={query}
        loadingLabel="Loading members"
        errorTitle="We could not load members"
        isEmpty={(data: MemberPage) => data.members.length === 0}
        empty={
          <p className="card px-6 py-14 text-center text-fg-secondary" data-testid="members-empty">
            {search || role ? 'No members found matching your filters' : 'No members in this community yet'}
          </p>
        }
      >
        {(data) => (
          <>
            <ul className="grid gap-4 md:grid-cols-2" aria-label="Members">
              {data.members.map((member) => (
                <MemberRow key={member.id} member={member} />
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
  const communityId = Number(id);
  if (!Number.isInteger(communityId) || communityId <= 0) {
    return <ErrorState title="Unknown community" message="This members link is not valid." />;
  }
  return <MembersView communityId={communityId} />;
}
