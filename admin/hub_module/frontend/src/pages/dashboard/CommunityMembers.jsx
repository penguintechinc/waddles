import { useState, useEffect } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { communityApi } from '../../services/api';
import { onActivate } from '../../components/ui/a11y';

const ROLE_BADGES = {
  'community-owner': { label: 'Owner', color: 'bg-gold-500 text-navy-900' },
  'community-admin': { label: 'Admin', color: 'bg-purple-500 text-white' },
  'moderator': { label: 'Moderator', color: 'bg-sky-500 text-white' },
  'vip': { label: 'VIP', color: 'bg-amber-500 text-navy-900' },
  'member': { label: 'Member', color: 'bg-navy-600 text-navy-300' },
};

const ROLE_FILTERS = [
  { value: '', label: 'All Members' },
  { value: 'community-owner,community-admin', label: 'Owners & Admins' },
  { value: 'moderator', label: 'Moderators' },
  { value: 'vip', label: 'VIPs' },
  { value: 'member', label: 'Members' },
];

function CommunityMembers() {
  const { id } = useParams();
  const navigate = useNavigate();

  const [members, setMembers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [community, setCommunity] = useState(null);

  const [search, setSearch] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [pagination, setPagination] = useState({ page: 1, limit: 25, total: 0, pages: 0 });

  useEffect(() => {
    loadCommunity();
  }, [id]);

  useEffect(() => {
    loadMembers();
  }, [id, roleFilter, pagination.page]);

  const loadCommunity = async () => {
    try {
      const response = await communityApi.getDashboard(id);
      if (response.data.success) {
        setCommunity(response.data.community);
      }
    } catch (err) {
      console.error('Failed to load community:', err);
    }
  };

  const loadMembers = async () => {
    try {
      setLoading(true);
      setError(null);

      const params = {
        page: pagination.page,
        limit: pagination.limit,
        ...(search && { search }),
        ...(roleFilter && { role: roleFilter }),
      };

      const response = await communityApi.getMembers(id, params);

      if (response.data.success) {
        setMembers(response.data.members);
        setPagination(prev => ({ ...prev, ...response.data.pagination }));
      }
    } catch (err) {
      setError('Failed to load members');
      console.error('Members error:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = (e) => {
    e.preventDefault();
    setPagination(prev => ({ ...prev, page: 1 }));
    loadMembers();
  };

  const handleRoleFilterChange = (filter) => {
    setRoleFilter(filter);
    setPagination(prev => ({ ...prev, page: 1 }));
  };

  const handlePageChange = (newPage) => {
    if (newPage >= 1 && newPage <= pagination.pages) {
      setPagination(prev => ({ ...prev, page: newPage }));
    }
  };

  const getDefaultAvatar = (username) => {
    return username ? username.charAt(0).toUpperCase() : '?';
  };

  const formatDate = (dateString) => {
    if (!dateString) return 'Unknown';
    return new Date(dateString).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  };

  return (
    <div className="min-h-screen bg-navy-950 p-6">
      <div className="max-w-4xl mx-auto">
        {/* Breadcrumb */}
        <div className="mb-6">
          <Link
            to={`/dashboard/community/${id}`}
            className="text-sky-400 hover:text-sky-300 text-sm flex items-center gap-2"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            Back to {community?.displayName || 'Community'}
          </Link>
        </div>

        {/* Header */}
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-sky-100">Community Members</h1>
          <p className="text-navy-400 mt-2">
            {pagination.total} {pagination.total === 1 ? 'member' : 'members'} in this community
          </p>
        </div>

        {/* Search and Filters */}
        <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-4 mb-6">
          {/* Search */}
          <form onSubmit={handleSearch} className="flex gap-2 flex-1 max-w-md">
            <input
              aria-label="Search by username"
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by username..."
              className="flex-1 px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500"
            />
            <button
              type="submit"
              className="px-4 py-2 bg-sky-600 hover:bg-sky-500 text-white rounded-lg transition-colors"
            >
              Search
            </button>
          </form>

          {/* Role filter tabs */}
          <div className="flex gap-2 flex-wrap">
            {ROLE_FILTERS.map((filter) => (
              <button
                key={filter.value}
                onClick={() => handleRoleFilterChange(filter.value)}
                className={`px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  roleFilter === filter.value
                    ? 'bg-gold-500 text-navy-900'
                    : 'bg-navy-800 text-navy-400 hover:text-sky-300'
                }`}
              >
                {filter.label}
              </button>
            ))}
          </div>
        </div>

        {/* Members Grid */}
        <div className="card bg-navy-900 rounded-xl border border-navy-700 overflow-hidden">
          {loading ? (
            <div className="flex items-center justify-center py-16">
              <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-b-2 border-sky-400"></div>
            </div>
          ) : error ? (
            <div className="text-center py-16 text-red-400">{error}</div>
          ) : members.length === 0 ? (
            <div className="text-center py-16 text-navy-400">
              {search || roleFilter ? 'No members found matching your filters' : 'No members in this community yet'}
            </div>
          ) : (
            <>
              <div className="grid gap-4 p-4 md:grid-cols-2">
                {members.map((member) => {
                  const badge = ROLE_BADGES[member.role] || ROLE_BADGES.member;
                  return (
                    <div
                      role="button"
                      tabIndex={0}
                      onKeyDown={onActivate(() => navigate(`/users/${member.userId}`))}
                      key={member.userId}
                      onClick={() => navigate(`/users/${member.userId}`)}
                      className="flex items-center gap-4 p-4 bg-navy-800 hover:bg-navy-700 rounded-lg cursor-pointer transition-colors"
                    >
                      {/* Avatar */}
                      {member.avatarUrl ? (
                        <img
                          src={member.avatarUrl}
                          alt={member.username}
                          className="w-12 h-12 rounded-full object-cover"
                        />
                      ) : (
                        <div className="w-12 h-12 rounded-full bg-navy-700 flex items-center justify-center text-sky-300 font-semibold text-lg">
                          {getDefaultAvatar(member.username)}
                        </div>
                      )}

                      {/* Info */}
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sky-100 font-medium truncate">{member.username}</span>
                          <span className={`px-2 py-0.5 text-xs font-semibold rounded ${badge.color}`}>
                            {badge.label}
                          </span>
                        </div>
                        <div className="text-sm text-navy-400 mt-1">
                          Joined {formatDate(member.joinedAt)}
                        </div>
                      </div>

                      {/* Arrow */}
                      <svg className="w-5 h-5 text-navy-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                      </svg>
                    </div>
                  );
                })}
              </div>

              {/* Pagination */}
              {pagination.pages > 1 && (
                <div className="px-4 py-3 bg-navy-800 border-t border-navy-700 flex items-center justify-between">
                  <div className="text-sm text-navy-400">
                    Showing {(pagination.page - 1) * pagination.limit + 1} to{' '}
                    {Math.min(pagination.page * pagination.limit, pagination.total)} of {pagination.total}
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => handlePageChange(pagination.page - 1)}
                      disabled={pagination.page === 1}
                      className="px-3 py-1 bg-navy-700 text-sky-300 rounded disabled:opacity-50 disabled:cursor-not-allowed hover:bg-navy-600 transition-colors"
                    >
                      Previous
                    </button>
                    <span className="px-3 py-1 text-navy-400">
                      Page {pagination.page} of {pagination.pages}
                    </span>
                    <button
                      onClick={() => handlePageChange(pagination.page + 1)}
                      disabled={pagination.page >= pagination.pages}
                      className="px-3 py-1 bg-navy-700 text-sky-300 rounded disabled:opacity-50 disabled:cursor-not-allowed hover:bg-navy-600 transition-colors"
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default CommunityMembers;
