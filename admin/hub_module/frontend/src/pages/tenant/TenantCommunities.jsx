import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { tenantApi } from '../../services/api';

const PAGE_SIZE = 20;

const TYPE_BADGE = {
  gaming: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
  professional: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  social: 'bg-green-500/20 text-green-300 border-green-500/30',
  education: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  workforce: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
};

function TenantCommunities() {
  const { tenantSlug } = useParams();
  const [communities, setCommunities] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [searchInput, setSearchInput] = useState('');

  useEffect(() => {
    loadCommunities();
  }, [tenantSlug, page, search]);

  const loadCommunities = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await tenantApi.getCommunities(tenantSlug, {
        page,
        limit: PAGE_SIZE,
        search: search || undefined,
      });
      setCommunities(res.data.communities || []);
      setTotal(res.data.total ?? res.data.communities?.length ?? 0);
    } catch (err) {
      setError(err?.response?.data?.error || 'Failed to load communities.');
    }
    setLoading(false);
  };

  const handleSearchSubmit = (e) => {
    e.preventDefault();
    setPage(1);
    setSearch(searchInput.trim());
  };

  const handleSearchClear = () => {
    setSearchInput('');
    setSearch('');
    setPage(1);
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const formatCount = (n) => {
    if (n === null || n === undefined) return '—';
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
    return String(n);
  };

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-sky-100 text-2xl font-bold">Communities</h1>
        <p className="text-sky-400 text-sm mt-1">
          All communities in this tenant
        </p>
      </div>

      {/* Search */}
      <form onSubmit={handleSearchSubmit} className="flex gap-2">
        <div className="relative flex-1">
          <svg
            className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-sky-500 pointer-events-none"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
            />
          </svg>
          <input
            aria-label="Search communities"
            type="text"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Search communities..."
            className="w-full bg-navy-800 border border-navy-700 rounded-lg pl-9 pr-3 py-2 text-sky-100 placeholder-sky-600 focus:border-gold-500 text-sm"
          />
        </div>
        <button
          type="submit"
          className="px-4 py-2 bg-gold-500 text-navy-900 rounded-lg font-semibold hover:bg-gold-400 transition-colors text-sm"
        >
          Search
        </button>
        {search && (
          <button
            type="button"
            onClick={handleSearchClear}
            className="px-3 py-2 bg-navy-700 text-sky-300 rounded-lg font-medium hover:bg-navy-600 transition-colors text-sm"
          >
            Clear
          </button>
        )}
      </form>

      {/* Error */}
      {error && (
        <div className="bg-red-900/30 border border-red-700 rounded-xl p-4 text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Table */}
      <div className="bg-navy-800 border border-navy-700 rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-navy-700 flex items-center justify-between">
          <h2 className="text-sky-100 font-semibold">
            {search
              ? `Results for "${search}"`
              : 'All Communities'}
          </h2>
          <span className="text-sky-400 text-sm">{total} total</span>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-16">
            <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-b-2 border-gold-400" />
          </div>
        ) : communities.length === 0 ? (
          <div className="py-16 text-center text-sky-400">
            {search ? 'No communities match your search.' : 'No communities found in this tenant.'}
          </div>
        ) : (
          <>
            {/* Desktop Table */}
            <div className="hidden md:block overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="text-left border-b border-navy-700">
                    <th className="px-5 py-3 text-sky-400 text-xs font-semibold uppercase tracking-wider">
                      Community
                    </th>
                    <th className="px-5 py-3 text-sky-400 text-xs font-semibold uppercase tracking-wider">
                      Type
                    </th>
                    <th className="px-5 py-3 text-sky-400 text-xs font-semibold uppercase tracking-wider">
                      Members
                    </th>
                    <th className="px-5 py-3 text-sky-400 text-xs font-semibold uppercase tracking-wider">
                      Status
                    </th>
                    <th className="px-5 py-3 text-sky-400 text-xs font-semibold uppercase tracking-wider">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-navy-700">
                  {communities.map((community) => (
                    <tr
                      key={community.id}
                      className="hover:bg-navy-750 transition-colors"
                    >
                      <td className="px-5 py-4">
                        <div className="flex items-center gap-3">
                          {community.logoUrl ? (
                            <img
                              src={community.logoUrl}
                              alt={community.name}
                              className="h-8 w-8 rounded-lg object-contain bg-navy-700 border border-navy-600 flex-shrink-0"
                              onError={(e) => { e.target.style.display = 'none'; }}
                            />
                          ) : (
                            <div className="h-8 w-8 rounded-lg bg-navy-700 border border-navy-600 flex items-center justify-center flex-shrink-0">
                              <span className="text-gold-400 text-xs font-bold">
                                {(community.name || '?')[0].toUpperCase()}
                              </span>
                            </div>
                          )}
                          <div className="min-w-0">
                            <p className="text-sky-100 font-medium truncate max-w-xs">
                              {community.name}
                            </p>
                            {community.slug && (
                              <p className="text-sky-500 text-xs font-mono truncate">
                                {community.slug}
                              </p>
                            )}
                          </div>
                        </div>
                      </td>
                      <td className="px-5 py-4">
                        {community.communityType ? (
                          <span
                            className={`text-xs px-2.5 py-0.5 border rounded-full font-medium capitalize ${
                              TYPE_BADGE[community.communityType] ||
                              'bg-navy-600 text-sky-300 border-navy-500'
                            }`}
                          >
                            {community.communityType}
                          </span>
                        ) : (
                          <span className="text-sky-600 text-xs">—</span>
                        )}
                      </td>
                      <td className="px-5 py-4">
                        <span className="text-sky-100 font-medium">
                          {formatCount(community.memberCount)}
                        </span>
                      </td>
                      <td className="px-5 py-4">
                        <span
                          className={`text-xs px-2.5 py-0.5 border rounded-full font-medium ${
                            community.isActive !== false
                              ? 'bg-green-500/20 text-green-300 border-green-500/30'
                              : 'bg-red-500/20 text-red-300 border-red-500/30'
                          }`}
                        >
                          {community.isActive !== false ? 'Active' : 'Inactive'}
                        </span>
                      </td>
                      <td className="px-5 py-4">
                        <Link
                          to={`/admin/${community.slug || community.id}`}
                          className="text-gold-400 hover:text-gold-300 text-sm font-medium transition-colors"
                        >
                          Manage
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile Cards */}
            <div className="md:hidden divide-y divide-navy-700">
              {communities.map((community) => (
                <div key={community.id} className="px-4 py-4">
                  <div className="flex items-start gap-3 mb-3">
                    {community.logoUrl ? (
                      <img
                        src={community.logoUrl}
                        alt={community.name}
                        className="h-10 w-10 rounded-lg object-contain bg-navy-700 border border-navy-600 flex-shrink-0"
                        onError={(e) => { e.target.style.display = 'none'; }}
                      />
                    ) : (
                      <div className="h-10 w-10 rounded-lg bg-navy-700 border border-navy-600 flex items-center justify-center flex-shrink-0">
                        <span className="text-gold-400 text-sm font-bold">
                          {(community.name || '?')[0].toUpperCase()}
                        </span>
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      <p className="text-sky-100 font-semibold truncate">{community.name}</p>
                      {community.slug && (
                        <p className="text-sky-500 text-xs font-mono">{community.slug}</p>
                      )}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 mb-3">
                    {community.communityType && (
                      <span
                        className={`text-xs px-2.5 py-0.5 border rounded-full font-medium capitalize ${
                          TYPE_BADGE[community.communityType] ||
                          'bg-navy-600 text-sky-300 border-navy-500'
                        }`}
                      >
                        {community.communityType}
                      </span>
                    )}
                    <span
                      className={`text-xs px-2.5 py-0.5 border rounded-full font-medium ${
                        community.isActive !== false
                          ? 'bg-green-500/20 text-green-300 border-green-500/30'
                          : 'bg-red-500/20 text-red-300 border-red-500/30'
                      }`}
                    >
                      {community.isActive !== false ? 'Active' : 'Inactive'}
                    </span>
                    <span className="text-sky-400 text-xs">
                      {formatCount(community.memberCount)} members
                    </span>
                  </div>
                  <Link
                    to={`/admin/${community.slug || community.id}`}
                    className="text-gold-400 hover:text-gold-300 text-sm font-medium transition-colors"
                  >
                    Manage community &rarr;
                  </Link>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-sky-400 text-sm">
            Page {page} of {totalPages}
          </p>
          <div className="flex gap-2">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page === 1 || loading}
              className="px-3 py-1.5 bg-navy-800 border border-navy-700 text-sky-300 rounded-lg text-sm font-medium hover:bg-navy-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Previous
            </button>
            {/* Page number pills */}
            {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
              let p;
              if (totalPages <= 5) {
                p = i + 1;
              } else if (page <= 3) {
                p = i + 1;
              } else if (page >= totalPages - 2) {
                p = totalPages - 4 + i;
              } else {
                p = page - 2 + i;
              }
              return (
                <button
                  key={p}
                  onClick={() => setPage(p)}
                  disabled={loading}
                  className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors disabled:cursor-not-allowed ${
                    p === page
                      ? 'bg-gold-500 text-navy-900'
                      : 'bg-navy-800 border border-navy-700 text-sky-300 hover:bg-navy-700'
                  }`}
                >
                  {p}
                </button>
              );
            })}
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page === totalPages || loading}
              className="px-3 py-1.5 bg-navy-800 border border-navy-700 text-sky-300 rounded-lg text-sm font-medium hover:bg-navy-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default TenantCommunities;
