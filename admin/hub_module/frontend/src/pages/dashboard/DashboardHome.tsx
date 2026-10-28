import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useAuthView } from '../../contexts/authView';
import { useCommunitySearch, useMyCommunities, usePublicCommunityDetails } from '../../hooks/useMyCommunities';
import { MyCommunityCard } from '../../components/dashboard/MyCommunityCard';
import { EmptyState, QueryBoundary, TextField } from '../../components/ui';
import type { MyCommunity, PublicCommunity } from '../../services/myCommunitiesApi';
import { getPlatformIcon, getPlatformLabel } from '../../utils/platformConfig';

const SEARCH_DEBOUNCE_MS = 300;

/** Debounces a fast-changing value (keystrokes) before it reaches a query key. */
function useDebounced(value: string, delayMs: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

interface MyCommunitiesGridProps {
  communities: MyCommunity[];
  searchTerm: string;
}

/** The user's own communities, filtered client-side by the search term, with best-effort public details. */
function MyCommunitiesGrid({ communities, searchTerm }: MyCommunitiesGridProps) {
  const details = usePublicCommunityDetails(useMemo(() => communities.map((c) => c.id), [communities]));
  const term = searchTerm.trim().toLowerCase();
  const visible = term
    ? communities.filter(
        (c) =>
          c.displayName.toLowerCase().includes(term) ||
          (details.get(c.id)?.description ?? '').toLowerCase().includes(term),
      )
    : communities;

  if (visible.length === 0) {
    return (
      <div className="card mb-6 p-6 text-center" data-testid="no-matching-communities">
        <p className="text-fg-secondary">No matching communities in your list.</p>
      </div>
    );
  }
  return (
    <>
      {term ? <h2 className="mb-4 text-lg font-semibold text-fg">My Communities</h2> : null}
      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3" data-testid="communities-grid">
        {visible.map((community) => (
          <MyCommunityCard key={community.id} community={community} details={details.get(community.id)} />
        ))}
      </div>
    </>
  );
}

interface DiscoverResultsProps {
  term: string;
  joinedIds: ReadonlySet<number>;
}

/** Public communities matching the search that the user has not joined yet. */
function DiscoverResults({ term, joinedIds }: DiscoverResultsProps) {
  const search = useCommunitySearch(term);
  return (
    <section className="mt-8" aria-labelledby="discover-heading">
      <h2 id="discover-heading" className="mb-4 text-lg font-semibold text-fg">
        Discover Communities
      </h2>
      <QueryBoundary
        query={search}
        loadingLabel="Searching communities"
        errorTitle="We could not search communities"
        isEmpty={(rows: PublicCommunity[]) => rows.filter((c) => !joinedIds.has(c.id)).length === 0}
        empty={<EmptyState title={`No public communities found for "${term}"`} headingLevel={3} />}
      >
        {(rows) => (
          <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3" data-testid="discover-grid">
            {rows
              .filter((c) => !joinedIds.has(c.id))
              .map((community) => (
                <Link
                  key={community.id}
                  to={`/communities/${community.id}`}
                  className="card card-interactive block p-4"
                  data-testid="discover-card"
                >
                  <div className="mb-2 flex items-center gap-2">
                    {community.platform ? (
                      <span title={getPlatformLabel(community.platform)}>{getPlatformIcon(community.platform)}</span>
                    ) : null}
                    <h3 className="truncate font-semibold text-fg">{community.displayName}</h3>
                  </div>
                  <p className="line-clamp-2 text-sm text-fg-secondary">{community.description ?? 'No description'}</p>
                  <p className="mt-3 flex justify-between text-xs text-fg-muted">
                    <span>{community.memberCount.toLocaleString()} members</span>
                    <span className="text-success-fg">Join</span>
                  </p>
                </Link>
              ))}
          </div>
        )}
      </QueryBoundary>
    </section>
  );
}

/**
 * Post-login landing ("My Communities"). Memberships come from `/auth/me` (hub-api has no
 * `/communities/my`), so loading, failure and "you have none yet" are real, distinct states
 * rather than a silently empty page.
 */
export default function DashboardHome() {
  const { username, isSuperAdmin, refreshUser } = useAuthView();
  const [searchTerm, setSearchTerm] = useState('');
  const debouncedSearch = useDebounced(searchTerm, SEARCH_DEBOUNCE_MS);
  const mine = useMyCommunities();
  const joinedIds = useMemo(() => new Set((mine.data ?? []).map((c) => c.id)), [mine.data]);

  // Refresh role flags (e.g. isSuperAdmin) in the auth context when the landing mounts.
  useEffect(() => {
    void refreshUser();
  }, [refreshUser]);

  return (
    <div>
      {isSuperAdmin ? (
        <div className="mb-6 flex items-center justify-between rounded-lg border border-gold-500/30 bg-gold-500/10 p-4">
          <div>
            <div className="font-semibold text-fg">Super Admin Access</div>
            <div className="text-sm text-fg-secondary">You have global administrative privileges</div>
          </div>
          <Link to="/superadmin" className="btn btn-secondary">
            Open Control Panel
          </Link>
        </div>
      ) : null}

      <div className="mb-8">
        <h1 className="text-2xl font-bold text-fg" data-testid="dashboard-welcome">
          Welcome back{username ? `, ${username}` : ''}!
        </h1>
        <p className="text-fg-secondary">Manage your communities and explore activity</p>
      </div>

      <div className="mb-6">
        <TextField
          label="Search your communities or discover new ones"
          hideLabel
          type="search"
          placeholder="Search your communities or discover new ones..."
          value={searchTerm}
          onChange={(event) => setSearchTerm(event.target.value)}
          data-testid="dashboard-search"
        />
      </div>

      <QueryBoundary
        query={mine}
        loadingLabel="Loading your communities"
        errorTitle="We could not load your communities"
        isEmpty={(communities: MyCommunity[]) => communities.length === 0}
        empty={
          <div data-testid="no-communities">
            <EmptyState
              title="No Communities Yet"
              description="You haven't joined any communities yet. Browse available communities to get started."
              icon={<img src="/waddlebot-logo.png" alt="" className="h-8 w-8" />}
              action={
                <Link to="/communities" className="btn btn-primary" data-testid="browse-communities-btn">
                  Browse Communities
                </Link>
              }
            />
          </div>
        }
      >
        {(communities) => <MyCommunitiesGrid communities={communities} searchTerm={debouncedSearch} />}
      </QueryBoundary>

      {debouncedSearch.trim() !== '' ? <DiscoverResults term={debouncedSearch} joinedIds={joinedIds} /> : null}
    </div>
  );
}
