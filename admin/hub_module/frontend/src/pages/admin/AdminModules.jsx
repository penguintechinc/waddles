import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  PuzzlePieceIcon,
  Cog6ToothIcon,
  XMarkIcon,
  CheckIcon,
  ExclamationTriangleIcon,
  MagnifyingGlassIcon,
  StarIcon,
  ArrowDownTrayIcon,
} from '@heroicons/react/24/outline';
import { adminApi, marketplaceApi } from '../../services/api';

// Module slug mapping for dedicated config pages
const MODULE_CONFIG_ROUTES = {
  lfg: 'lfg',
  'looking-for-group': 'lfg',
  clip: 'clip',
  alias: 'alias',
  memories: 'memories',
  'server-status': 'server-status',
  'server-manager': 'server-manager',
};

function AdminModules() {
  const { communityId } = useParams();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState('installed');

  // Installed modules state
  const [modules, setModules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [actionLoading, setActionLoading] = useState({});

  // Config modal state
  const [configModal, setConfigModal] = useState(null);
  const [configText, setConfigText] = useState('');
  const [configError, setConfigError] = useState(null);
  const [saving, setSaving] = useState(false);

  // Core module warning state
  const [showCoreWarning, setShowCoreWarning] = useState(false);
  const [pendingDisable, setPendingDisable] = useState(null);

  // Marketplace state
  const [marketplaceModules, setMarketplaceModules] = useState([]);
  const [marketplaceLoading, setMarketplaceLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [page, setPage] = useState(1);
  const [pagination, setPagination] = useState(null);

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
  }, [communityId]);

  useEffect(() => {
    if (activeTab === 'marketplace') {
      loadMarketplace();
    }
  }, [communityId, search, category, page, activeTab]);

  const loadModules = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await adminApi.getModules(communityId);
      setModules(response.data.modules || []);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load modules');
    } finally {
      setLoading(false);
    }
  };

  const loadMarketplace = async () => {
    try {
      setMarketplaceLoading(true);
      const response = await marketplaceApi.browseModules(communityId, {
        search,
        category,
        page,
        limit: 12,
      });
      setMarketplaceModules(response.data.modules);
      setPagination(response.data.pagination);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load marketplace');
    } finally {
      setMarketplaceLoading(false);
    }
  };

  const toggleModule = async (moduleId, newValue) => {
    try {
      setActionLoading({ ...actionLoading, [moduleId]: true });
      await adminApi.updateModuleConfig(communityId, moduleId, {
        isEnabled: newValue,
      });
      setMessage({ type: 'success', text: `Module ${newValue ? 'enabled' : 'disabled'} successfully` });
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to toggle module');
    } finally {
      setActionLoading({ ...actionLoading, [moduleId]: false });
    }
  };

  const handleToggleEnabled = (module) => {
    const newValue = !module.isEnabled;
    if (module.isCore && !newValue) {
      setPendingDisable({ module, newValue });
      setShowCoreWarning(true);
    } else {
      toggleModule(module.moduleId, newValue);
    }
  };

  const confirmDisableCore = async () => {
    if (pendingDisable) {
      await toggleModule(pendingDisable.module.moduleId, pendingDisable.newValue);
      setShowCoreWarning(false);
      setPendingDisable(null);
    }
  };

  const handleConfigure = (module) => {
    const slug = MODULE_CONFIG_ROUTES[module.name?.toLowerCase()] ||
                 MODULE_CONFIG_ROUTES[module.displayName?.toLowerCase()];
    if (slug) {
      navigate(`/admin/${communityId}/modules/${slug}/config`);
    } else {
      openConfigModal(module);
    }
  };

  const openConfigModal = (module) => {
    setConfigModal(module);
    setConfigText(JSON.stringify(module.config || {}, null, 2));
    setConfigError(null);
  };

  const closeConfigModal = () => {
    setConfigModal(null);
    setConfigText('');
    setConfigError(null);
  };

  const handleSaveConfig = async () => {
    let parsedConfig;
    try {
      parsedConfig = JSON.parse(configText);
    } catch (e) {
      setConfigError('Invalid JSON format');
      return;
    }

    try {
      setSaving(true);
      setConfigError(null);
      await adminApi.updateModuleConfig(communityId, configModal.moduleId, {
        config: parsedConfig,
      });
      setMessage({ type: 'success', text: 'Module configuration saved successfully' });
      closeConfigModal();
      loadModules();
    } catch (err) {
      setConfigError(err.response?.data?.error?.message || 'Failed to save configuration');
    } finally {
      setSaving(false);
    }
  };

  const handleInstall = async (moduleId) => {
    try {
      setActionLoading({ ...actionLoading, [moduleId]: 'installing' });
      await marketplaceApi.installModule(communityId, moduleId);
      setMessage({ type: 'success', text: 'Module installed successfully' });
      loadMarketplace();
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to install module');
    } finally {
      setActionLoading({ ...actionLoading, [moduleId]: null });
    }
  };

  const handleUninstall = async (moduleId) => {
    if (!confirm('Are you sure you want to uninstall this module?')) return;
    try {
      setActionLoading({ ...actionLoading, [moduleId]: 'uninstalling' });
      await marketplaceApi.uninstallModule(communityId, moduleId);
      setMessage({ type: 'success', text: 'Module uninstalled successfully' });
      loadMarketplace();
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to uninstall module');
    } finally {
      setActionLoading({ ...actionLoading, [moduleId]: null });
    }
  };

  const getCategoryColor = (cat) => {
    const colors = {
      general: 'bg-sky-500/20 text-sky-300 border-sky-500/30',
      moderation: 'bg-red-500/20 text-red-300 border-red-500/30',
      entertainment: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
      music: 'bg-pink-500/20 text-pink-300 border-pink-500/30',
      utility: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30',
      games: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
      ai: 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30',
    };
    return colors[cat?.toLowerCase()] || 'bg-navy-700 text-navy-300 border-navy-600';
  };

  const getPricingBadge = (module) => {
    if (!module.pricing || module.pricing === 'free') {
      return <span className="text-xs px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">Free</span>;
    }
    if (module.pricing === 'one-time') {
      return <span className="text-xs px-2 py-0.5 rounded bg-gold-500/20 text-gold-400 border border-gold-500/30">${module.price}</span>;
    }
    if (module.pricing === 'subscription') {
      return <span className="text-xs px-2 py-0.5 rounded bg-purple-500/20 text-purple-300 border border-purple-500/30">${module.price}/mo</span>;
    }
    if (module.pricing === 'per-seat') {
      return <span className="text-xs px-2 py-0.5 rounded bg-sky-500/20 text-sky-300 border border-sky-500/30">${module.price}/seat/mo</span>;
    }
    return null;
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-sky-100">Modules</h1>
        <p className="text-navy-400 mt-1">
          Manage installed modules and browse the marketplace
        </p>
      </div>

      {/* Messages */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-400" />
            <span className="text-red-400">{error}</span>
          </div>
          <button onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {message && (
        <div className={`rounded-lg p-4 flex items-center justify-between ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border border-red-500/30'
        }`}>
          <div className="flex items-center space-x-3">
            <CheckIcon className="w-5 h-5" />
            <span>{message.text}</span>
          </div>
          <button onClick={() => setMessage(null)} className="hover:opacity-75">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-navy-700">
        <nav className="flex space-x-8">
          <button
            onClick={() => setActiveTab('installed')}
            className={`pb-3 px-1 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'installed'
                ? 'border-gold-400 text-gold-400'
                : 'border-transparent text-navy-400 hover:text-sky-300'
            }`}
          >
            Installed Modules
          </button>
          <button
            onClick={() => setActiveTab('marketplace')}
            className={`pb-3 px-1 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'marketplace'
                ? 'border-gold-400 text-gold-400'
                : 'border-transparent text-navy-400 hover:text-sky-300'
            }`}
          >
            Browse Marketplace
          </button>
          <button
            onClick={() => setActiveTab('bundle-apps')}
            data-testid="admin-modules-bundle-apps-tab"
            className={`pb-3 px-1 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'bundle-apps'
                ? 'border-gold-400 text-gold-400'
                : 'border-transparent text-navy-400 hover:text-sky-300'
            }`}
          >
            Bundle Apps
          </button>
        </nav>
      </div>

      {/* Installed Tab */}
      {activeTab === 'installed' && (
        <>
          {loading ? (
            <div className="flex items-center justify-center h-64">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
            </div>
          ) : modules.length === 0 ? (
            <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
              <PuzzlePieceIcon className="w-12 h-12 text-navy-500 mx-auto mb-4" />
              <h3 className="text-lg font-semibold text-sky-100 mb-2">No Modules Installed</h3>
              <p className="text-navy-400 mb-4">
                Browse the marketplace to find and install modules for your community.
              </p>
              <button
                onClick={() => setActiveTab('marketplace')}
                className="px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg"
              >
                Browse Marketplace
              </button>
            </div>
          ) : (
            <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
              <table className="w-full">
                <thead className="bg-navy-900">
                  <tr>
                    <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Module</th>
                    <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Category</th>
                    <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Status</th>
                    <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Installed</th>
                    <th className="text-right py-3 px-4 text-navy-400 font-medium text-sm">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-navy-700">
                  {modules.map((module) => (
                    <tr key={module.installationId} className="hover:bg-navy-700/50">
                      <td className="py-4 px-4">
                        <div className="flex items-center space-x-3">
                          <div className="w-10 h-10 bg-navy-700 rounded-lg flex items-center justify-center">
                            <PuzzlePieceIcon className="w-5 h-5 text-gold-400" />
                          </div>
                          <div>
                            <div className="flex items-center">
                              <p className="font-medium text-sky-100">{module.displayName || module.name}</p>
                              {module.isCore && (
                                <span className="ml-2 px-2 py-0.5 text-xs rounded bg-gold-500/20 text-gold-400 border border-gold-500/30">
                                  Core
                                </span>
                              )}
                            </div>
                            <p className="text-sm text-navy-400 truncate max-w-xs">{module.description}</p>
                          </div>
                        </div>
                      </td>
                      <td className="py-4 px-4">
                        <span className={`px-2 py-1 rounded-full text-xs font-medium border ${getCategoryColor(module.category)}`}>
                          {module.category || 'General'}
                        </span>
                      </td>
                      <td className="py-4 px-4">
                        <button
                          onClick={() => handleToggleEnabled(module)}
                          disabled={actionLoading[module.moduleId]}
                          className={`px-3 py-1 rounded-full text-sm font-medium transition-colors ${
                            module.isEnabled
                              ? 'bg-green-500/20 text-green-400 border border-green-500/30 hover:bg-green-500/30'
                              : 'bg-red-500/20 text-red-400 border border-red-500/30 hover:bg-red-500/30'
                          } disabled:opacity-50`}
                        >
                          {actionLoading[module.moduleId] ? '...' : module.isEnabled ? 'Enabled' : 'Disabled'}
                        </button>
                      </td>
                      <td className="py-4 px-4 text-navy-400 text-sm">
                        {module.installedAt ? new Date(module.installedAt).toLocaleDateString() : 'N/A'}
                      </td>
                      <td className="py-4 px-4 text-right space-x-2">
                        <button
                          onClick={() => handleConfigure(module)}
                          className="inline-flex items-center space-x-2 px-3 py-2 bg-navy-700 hover:bg-navy-600 text-sky-100 rounded-lg transition-colors"
                        >
                          <Cog6ToothIcon className="w-4 h-4" />
                          <span>Configure</span>
                        </button>
                        {!module.isCore && (
                          <button
                            onClick={() => handleUninstall(module.moduleId)}
                            disabled={actionLoading[module.moduleId]}
                            className="inline-flex items-center px-3 py-2 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg transition-colors disabled:opacity-50"
                          >
                            Uninstall
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {/* Marketplace Tab */}
      {activeTab === 'marketplace' && (
        <>
          {/* Search and Filter */}
          <div className="flex gap-4">
            <div className="flex-1 relative">
              <MagnifyingGlassIcon className="absolute left-3 top-1/2 transform -translate-y-1/2 text-navy-400 w-5 h-5" />
              <input
                type="text"
                placeholder="Search modules..."
                value={search}
                onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                className="w-full pl-10 pr-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-sky-100 focus:outline-none focus:border-gold-500"
              />
            </div>
            <select
              value={category}
              onChange={(e) => { setCategory(e.target.value); setPage(1); }}
              className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-sky-100 focus:outline-none focus:border-gold-500"
            >
              {categories.map((cat) => (
                <option key={cat.value} value={cat.value}>{cat.label}</option>
              ))}
            </select>
          </div>

          {marketplaceLoading ? (
            <div className="flex items-center justify-center h-64">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {marketplaceModules.map((module) => (
                  <div
                    key={module.id}
                    className="bg-navy-800 border border-navy-700 rounded-lg p-6 hover:border-gold-500/50 transition-colors"
                  >
                    <div className="flex items-start justify-between mb-4">
                      <div className="flex items-center gap-3">
                        {module.iconUrl ? (
                          <img src={module.iconUrl} alt={module.displayName} className="w-12 h-12 rounded" />
                        ) : (
                          <div className="w-12 h-12 bg-navy-700 rounded flex items-center justify-center">
                            <PuzzlePieceIcon className="w-6 h-6 text-gold-400" />
                          </div>
                        )}
                        <div>
                          <h3 className="font-semibold text-sky-100">{module.displayName}</h3>
                          {module.isCore && (
                            <span className="text-xs text-gold-400">Core Module</span>
                          )}
                        </div>
                      </div>
                      {module.isInstalled && (
                        <CheckIcon className="w-5 h-5 text-green-400" />
                      )}
                    </div>

                    <p className="text-navy-400 text-sm mb-4 line-clamp-3">{module.description}</p>

                    <div className="flex items-center gap-4 text-sm text-navy-400 mb-4">
                      <div className="flex items-center gap-1">
                        <StarIcon className="w-4 h-4 text-gold-400" />
                        <span>{module.avgRating}</span>
                        <span>({module.reviewCount})</span>
                      </div>
                      <div className="flex items-center gap-1">
                        <ArrowDownTrayIcon className="w-4 h-4" />
                        <span>{module.installCount}</span>
                      </div>
                      {getPricingBadge(module)}
                    </div>

                    <div className="flex items-center gap-2 text-xs text-navy-500 mb-4">
                      <span className={`px-2 py-1 rounded-full border ${getCategoryColor(module.category)}`}>
                        {module.category}
                      </span>
                      <span>v{module.version}</span>
                      <span>by {module.author}</span>
                    </div>

                    <div className="flex gap-2">
                      {module.isInstalled ? (
                        <>
                          <span className="flex-1 px-4 py-2 bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 rounded-lg font-medium text-center text-sm">
                            Installed
                          </span>
                          {!module.isCore && (
                            <button
                              onClick={() => handleUninstall(module.id)}
                              disabled={actionLoading[module.id]}
                              className="px-4 py-2 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg transition-colors disabled:opacity-50 text-sm"
                            >
                              {actionLoading[module.id] === 'uninstalling' ? '...' : 'Remove'}
                            </button>
                          )}
                        </>
                      ) : (
                        <button
                          onClick={() => handleInstall(module.id)}
                          disabled={actionLoading[module.id]}
                          className="w-full px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 rounded-lg font-semibold transition-colors disabled:opacity-50 text-sm"
                        >
                          {actionLoading[module.id] === 'installing' ? 'Installing...' : 'Install'}
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              {pagination && pagination.totalPages > 1 && (
                <div className="flex justify-center gap-2">
                  <button
                    onClick={() => setPage(page - 1)}
                    disabled={page === 1}
                    className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg disabled:opacity-50 hover:border-gold-500/50 text-sky-100"
                  >
                    Previous
                  </button>
                  <span className="px-4 py-2 text-navy-400">
                    Page {page} of {pagination.totalPages}
                  </span>
                  <button
                    onClick={() => setPage(page + 1)}
                    disabled={page === pagination.totalPages}
                    className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg disabled:opacity-50 hover:border-gold-500/50 text-sky-100"
                  >
                    Next
                  </button>
                </div>
              )}
            </>
          )}
        </>
      )}

      {/* Bundle Apps Tab -- tier 3 follow-up placeholder (three-tier app
          lifecycle, Justin's 2026-09-27 ruling): a community admin should
          be able to activate/deactivate, for THIS community, any bundle
          version a tenant admin has made available (tier 2,
          TenantModules.jsx), once a global admin has installed it into
          the platform catalog (tier 1, SuperAdminBundleApprovals.jsx).
          No community-level activate/deactivate endpoint exists in
          hub-api yet -- this tab is deliberately a placeholder rather
          than a call against an invented endpoint. */}
      {activeTab === 'bundle-apps' && (
        <div
          className="bg-navy-800 border border-navy-700 border-dashed rounded-xl p-12 text-center"
          data-testid="community-bundle-apps-followup"
        >
          <PuzzlePieceIcon className="w-12 h-12 text-navy-500 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gold-400 mb-2">Bundle Apps (Coming Soon)</h3>
          <p className="text-navy-400">
            Activating or deactivating bundle-catalog apps made available to your tenant will
            appear here once the community-level activation backend endpoint ships.
          </p>
        </div>
      )}

      {/* Core Module Warning Modal */}
      {showCoreWarning && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-800 rounded-lg p-6 max-w-md border border-navy-700">
            <h3 className="text-lg font-bold text-sky-100 mb-4">Disable Core Module?</h3>
            <p className="text-navy-300 mb-4">
              You are about to disable <span className="text-gold-400">{pendingDisable?.module.displayName || pendingDisable?.module.name}</span>,
              a core Waddles module. This may affect community functionality.
            </p>
            <p className="text-navy-400 text-sm mb-6">
              You can use an external service instead, but some features may not work as expected.
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setShowCoreWarning(false)}
                className="px-4 py-2 text-navy-300 hover:bg-navy-700 rounded-lg"
              >
                Cancel
              </button>
              <button
                onClick={confirmDisableCore}
                className="px-4 py-2 bg-red-500/20 text-red-400 border border-red-500/30 rounded-lg hover:bg-red-500/30"
              >
                Disable Anyway
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Configuration Modal (JSON editor fallback) */}
      {configModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-sky-100">
                Configure: {configModal.displayName || configModal.name}
              </h3>
              <button
                onClick={closeConfigModal}
                className="text-navy-400 hover:text-sky-100"
              >
                <XMarkIcon className="w-6 h-6" />
              </button>
            </div>

            <p className="text-navy-400 text-sm mb-4">
              Edit the module configuration below. The configuration must be valid JSON.
            </p>

            {configError && (
              <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 mb-4 text-red-400 text-sm">
                {configError}
              </div>
            )}

            <textarea
              value={configText}
              onChange={(e) => setConfigText(e.target.value)}
              className="flex-1 w-full bg-navy-900 border border-navy-700 rounded-lg p-4 font-mono text-sm text-sky-100 focus:outline-none focus:border-gold-500 min-h-[300px] resize-none"
              placeholder="{}"
            />

            <div className="flex justify-end space-x-3 mt-4">
              <button
                onClick={closeConfigModal}
                className="px-4 py-2 text-navy-300 hover:text-sky-100"
              >
                Cancel
              </button>
              <button
                onClick={handleSaveConfig}
                disabled={saving}
                className="px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg disabled:opacity-50"
              >
                {saving ? 'Saving...' : 'Save Configuration'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminModules;
