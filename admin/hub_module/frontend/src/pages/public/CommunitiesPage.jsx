import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { publicApi } from '../../services/api';
import CommunityTypeBadge, { communityTypeConfig } from '../../components/CommunityTypeBadge';
import { getPlatformIcon, getPlatformLabel, getAllPlatformOptions } from '../../utils/platformConfig';

function CommunitiesPage() {
  const { isAuthenticated, loading: authLoading } = useAuth();
  const [communities, setCommunities] = useState([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [pagination, setPagination] = useState(null);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [platformFilter, setPlatformFilter] = useState('');

  // Debounce search
  const [debouncedSearch, setDebouncedSearch] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    async function fetchCommunities() {
      setLoading(true);
      try {
        const params = { page, limit: 12 };
        if (debouncedSearch) params.search = debouncedSearch;
        if (typeFilter) params.type = typeFilter;
        if (platformFilter) params.platform = platformFilter;
        const response = await publicApi.getCommunities(params);
        setCommunities(response.data.communities);
        setPagination(response.data.pagination);
      } catch (err) {
        console.error('Failed to fetch communities:', err);
      } finally {
        setLoading(false);
      }
    }
    fetchCommunities();
  }, [page, debouncedSearch, typeFilter, platformFilter]);

  // Reset page when filters change
  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, typeFilter, platformFilter]);

  const platformFilterOptions = [
    { value: '', label: 'All Platforms' },
    ...getAllPlatformOptions().map((p) => ({
      value: p.id,
      label: `${p.icon} ${p.label}`,
    })),
  ];

  const typeFilterOptions = [
    { value: '', label: 'All Types' },
    ...Object.entries(communityTypeConfig).map(([value, config]) => ({
      value,
      label: `${config.icon} ${config.label}`,
    })),
  ];

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
      <div className="text-center mb-8">
        <h1 className="text-3xl font-bold mb-4 gradient-text">Discover Communities</h1>
        <p className="text-navy-400">Browse public communities using Waddles</p>
        {!authLoading && (
          isAuthenticated ? (
            <Link
              to="/communities/create"
              className="btn btn-primary mt-4 inline-block"
              data-testid="create-community-btn"
            >
              + Create a Community
            </Link>
          ) : (
            <Link
              to="/login"
              className="btn btn-secondary mt-4 inline-block"
            >
              Sign in to Create a Community
            </Link>
          )
        )}
      </div>

      {/* Search and Filter */}
      <div className="flex flex-col sm:flex-row gap-4 mb-8 max-w-2xl mx-auto">
        <div className="flex-1">
          <input
            aria-label="Search communities"
            type="text"
            placeholder="Search communities..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input w-full"
          />
        </div>
        <div className="sm:w-48">
          <select
            aria-label="Filter by platform"
            value={platformFilter}
            onChange={(e) => setPlatformFilter(e.target.value)}
            className="input w-full"
          >
            {platformFilterOptions.map(option => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="sm:w-48">
          <select
            aria-label="Filter by type"
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="input w-full"
          >
            {typeFilterOptions.map(option => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {loading ? (
        <div className="flex justify-center py-12">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400"></div>
        </div>
      ) : (
        <>
          <div className="grid md:grid-cols-3 lg:grid-cols-4 gap-6">
            {communities.map((community) => (
              <Link
                key={community.id}
                to={`/communities/${community.id}`}
                className="card hover:border-sky-500 transition-all overflow-hidden group"
              >
                <div className="aspect-video bg-gradient-to-br from-navy-700 to-navy-800 flex items-center justify-center group-hover:from-sky-900 group-hover:to-navy-800 transition-all">
                  {community.logoUrl ? (
                    <img
                      src={community.logoUrl}
                      alt={community.displayName}
                      className="w-full h-full object-cover"
                    />
                  ) : (
                    <img src="/waddlebot-logo.png" alt="Community Logo" className="w-16 h-16" />
                  )}
                </div>
                <div className="p-4">
                  <div className="flex items-center justify-between mb-1">
                    <div className="flex items-center space-x-2 min-w-0">
                      <span title={getPlatformLabel(community.primaryPlatform)}>{getPlatformIcon(community.primaryPlatform)}</span>
                      <h3 className="font-semibold truncate text-sky-100">{community.displayName}</h3>
                    </div>
                    {community.communityType && (
                      <CommunityTypeBadge type={community.communityType} size="sm" showLabel={false} />
                    )}
                  </div>
                  <p className="text-sm text-navy-400 line-clamp-2">
                    {community.description || 'No description'}
                  </p>
                  <div className="mt-3 flex items-center justify-between text-xs text-navy-500">
                    <span>{community.memberCount} members</span>
                    {community.communityType && (
                      <span className="text-navy-400">{communityTypeConfig[community.communityType]?.label}</span>
                    )}
                  </div>
                </div>
              </Link>
            ))}
          </div>

          {/* Pagination */}
          {pagination && pagination.totalPages > 1 && (
            <div className="flex justify-center mt-8 space-x-2">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                className="btn btn-secondary disabled:opacity-50"
              >
                Previous
              </button>
              <span className="px-4 py-2 text-navy-400">
                Page {page} of {pagination.totalPages}
              </span>
              <button
                onClick={() => setPage(p => Math.min(pagination.totalPages, p + 1))}
                disabled={page === pagination.totalPages}
                className="btn btn-secondary disabled:opacity-50"
              >
                Next
              </button>
            </div>
          )}

          {communities.length === 0 && (
            <div className="text-center py-12 text-navy-400">
              No public communities found
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default CommunitiesPage;
