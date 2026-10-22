import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { marketplaceApi, unifiedMarketplaceApi } from '../../services/api';
import { Search, Star, Package, Check, Download, Settings, Filter } from 'lucide-react';

export default function AdminMarketplace() {
  const { communityId } = useParams();
  const navigate = useNavigate();
  const [modules, setModules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [page, setPage] = useState(1);
  const [pagination, setPagination] = useState(null);
  const [actionLoading, setActionLoading] = useState({});

  const categories = [
    { value: '', label: 'All Categories' },
    { value: 'general', label: 'General' },
    { value: 'moderation', label: 'Moderation' },
    { value: 'entertainment', label: 'Entertainment' },
    { value: 'music', label: 'Music' },
    { value: 'utility', label: 'Utility' },
    { value: 'games', label: 'Games' },
    { value: 'ai', label: 'AI & Automation' },
  ];

  useEffect(() => {
    loadModules();
  }, [communityId, search, category, page]);

  const loadModules = async () => {
    try {
      setLoading(true);
      const response = await unifiedMarketplaceApi.getCatalog({
        communityId,
        search,
        category,
        page,
        limit: 12,
      });
      setModules(response.data.modules);
      setPagination(response.data.pagination);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load modules');
    } finally {
      setLoading(false);
    }
  };

  const handleInstall = async (module) => {
    const moduleId = module.sourceId || module.id;
    try {
      setActionLoading({ ...actionLoading, [moduleId]: 'installing' });
      await unifiedMarketplaceApi.installModule(communityId, { source: module.source, moduleId });
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to install module');
    } finally {
      setActionLoading({ ...actionLoading, [moduleId]: null });
    }
  };

  const handleUninstall = async (module) => {
    if (!confirm('Are you sure you want to uninstall this module?')) return;

    const moduleId = module.sourceId || module.id;
    try {
      setActionLoading({ ...actionLoading, [moduleId]: 'uninstalling' });
      await unifiedMarketplaceApi.uninstallModule(communityId, moduleId, module.source);
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to uninstall module');
    } finally {
      setActionLoading({ ...actionLoading, [moduleId]: null });
    }
  };

  const handleToggleEnabled = async (moduleId, currentEnabled) => {
    try {
      setActionLoading({ ...actionLoading, [moduleId]: 'toggling' });
      await marketplaceApi.configureModule(communityId, moduleId, {
        isEnabled: !currentEnabled,
      });
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to toggle module');
    } finally {
      setActionLoading({ ...actionLoading, [moduleId]: null });
    }
  };

  if (loading && modules.length === 0) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-sky-400">Loading marketplace...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-navy-900 text-white p-6">
      <div className="max-w-7xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-gold-400 mb-2">Module Marketplace</h1>
          <p className="text-gray-400">Browse and install modules for your community</p>
        </div>

        {/* Error Message */}
        {error && (
          <div className="mb-6 bg-red-900/20 border border-red-500 text-red-400 px-4 py-3 rounded">
            {error}
            <button onClick={() => setError(null)} className="float-right font-bold">×</button>
          </div>
        )}

        {/* Search and Filter Bar */}
        <div className="mb-6 flex gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-400" size={20} />
            <input
              aria-label="Search modules"
              type="text"
              placeholder="Search modules..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full pl-10 pr-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white focus:border-sky-500"
            />
          </div>
          <select
            aria-label="Filter by category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white focus:border-sky-500"
          >
            {categories.map((cat) => (
              <option key={cat.value} value={cat.value}>
                {cat.label}
              </option>
            ))}
          </select>
        </div>

        {/* Module Grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {modules.map((module) => (
            <div
              key={module.id}
              className="bg-navy-800 border border-navy-700 rounded-lg p-6 hover:border-sky-500 transition-colors"
            >
              {/* Module Header */}
              <div className="flex items-start justify-between mb-4">
                <div className="flex items-center gap-3">
                  {module.iconUrl ? (
                    <img src={module.iconUrl} alt={module.displayName} className="w-12 h-12 rounded" />
                  ) : (
                    <div className="w-12 h-12 bg-navy-700 rounded flex items-center justify-center">
                      <Package className="text-sky-400" size={24} />
                    </div>
                  )}
                  <div>
                    <h3 className="font-semibold text-white">{module.displayName}</h3>
                    {module.isCore && (
                      <span className="text-xs text-gold-400">Core Module</span>
                    )}
                  </div>
                </div>
                {module.isInstalled && (
                  <Check className="text-green-400" size={20} />
                )}
              </div>

              {/* Module Info */}
              <p className="text-gray-400 text-sm mb-4 line-clamp-3">{module.description}</p>

              {/* Module Meta */}
              <div className="flex items-center gap-4 text-sm text-gray-400 mb-4">
                <div className="flex items-center gap-1">
                  <Star className="text-gold-400" size={14} />
                  <span>{module.avgRating}</span>
                  <span>({module.reviewCount})</span>
                </div>
                <div className="flex items-center gap-1">
                  <Download size={14} />
                  <span>{module.installCount}</span>
                </div>
              </div>

              {/* Module Details */}
              <div className="flex items-center gap-2 text-xs text-gray-500 mb-3">
                <span className="bg-navy-700 px-2 py-1 rounded">{module.category}</span>
                <span>v{module.version}</span>
                <span>by {module.author}</span>
              </div>

              {/* Pricing Badge */}
              <div className="mb-4">
                {module.pricingType === 'paid' && (
                  <span className="inline-block bg-yellow-900/40 border border-yellow-600 text-yellow-400 text-xs px-2 py-1 rounded">
                    Paid
                  </span>
                )}
                {module.pricingType === 'subscription' && (
                  <span className="inline-block bg-purple-900/40 border border-purple-600 text-purple-400 text-xs px-2 py-1 rounded">
                    Subscription
                  </span>
                )}
                {(!module.pricingType || module.pricingType === 'free') && (
                  <span className="inline-block bg-green-900/40 border border-green-600 text-green-400 text-xs px-2 py-1 rounded">
                    Free
                  </span>
                )}
              </div>

              {/* Actions */}
              <div className="flex gap-2">
                {module.isInstalled ? (
                  <>
                    <button
                      onClick={() => handleToggleEnabled(module.sourceId || module.id, module.isEnabled)}
                      disabled={actionLoading[module.sourceId || module.id]}
                      className={`flex-1 px-4 py-2 rounded-lg font-semibold transition-colors ${
                        module.isEnabled
                          ? 'bg-gold-500 hover:bg-gold-600 text-navy-900'
                          : 'bg-navy-700 hover:bg-navy-600 text-white'
                      } disabled:opacity-50`}
                    >
                      {actionLoading[module.sourceId || module.id] === 'toggling' ? '...' : module.isEnabled ? 'Enabled' : 'Disabled'}
                    </button>
                    <button
                      onClick={() => navigate(`/admin/${communityId}/marketplace/${module.source}/${module.sourceId || module.id}`)}
                      className="px-4 py-2 bg-sky-600 hover:bg-sky-700 text-white rounded-lg transition-colors"
                    >
                      <Settings size={18} />
                    </button>
                    <button
                      onClick={() => handleUninstall(module)}
                      disabled={actionLoading[module.sourceId || module.id]}
                      className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors disabled:opacity-50"
                    >
                      {actionLoading[module.sourceId || module.id] === 'uninstalling' ? '...' : 'Remove'}
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      onClick={() => handleInstall(module)}
                      disabled={actionLoading[module.sourceId || module.id]}
                      className="flex-1 px-4 py-2 bg-sky-600 hover:bg-sky-700 text-white rounded-lg font-semibold transition-colors disabled:opacity-50"
                    >
                      {actionLoading[module.sourceId || module.id] === 'installing' ? 'Installing...' : 'Install'}
                    </button>
                    <button
                      onClick={() => navigate(`/admin/${communityId}/marketplace/${module.source}/${module.sourceId || module.id}`)}
                      className="px-4 py-2 bg-navy-700 hover:bg-navy-600 text-white rounded-lg transition-colors"
                    >
                      Details
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Pagination */}
        {pagination && pagination.totalPages > 1 && (
          <div className="mt-8 flex justify-center gap-2">
            <button
              onClick={() => setPage(page - 1)}
              disabled={page === 1}
              className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg disabled:opacity-50 hover:border-sky-500"
            >
              Previous
            </button>
            <span className="px-4 py-2 text-gray-400">
              Page {page} of {pagination.totalPages}
            </span>
            <button
              onClick={() => setPage(page + 1)}
              disabled={page === pagination.totalPages}
              className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg disabled:opacity-50 hover:border-sky-500"
            >
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
