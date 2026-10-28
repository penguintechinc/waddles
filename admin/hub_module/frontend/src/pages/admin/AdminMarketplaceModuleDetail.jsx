import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  ArrowLeftIcon,
  StarIcon,
  ArrowDownTrayIcon,
  TagIcon,
  CheckCircleIcon,
  XCircleIcon,
  CommandLineIcon,
  BoltIcon,
  CursorArrowRaysIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { unifiedMarketplaceApi } from '../../services/api';

const PRICING_BADGES = {
  free: { label: 'Free', className: 'bg-green-900/40 border-green-600 text-green-400' },
  paid: { label: 'Paid', className: 'bg-yellow-900/40 border-yellow-600 text-yellow-400' },
  subscription: { label: 'Subscription', className: 'bg-purple-900/40 border-purple-600 text-purple-400' },
};

const INTEGRATION_ICONS = {
  command_handler: CommandLineIcon,
  action: BoltIcon,
  trigger: ArrowPathIcon,
  interaction: CursorArrowRaysIcon,
};

function StarRating({ rating, max = 5 }) {
  return (
    <div className="flex items-center gap-0.5">
      {Array.from({ length: max }).map((_, i) => (
        <StarIcon
          key={i}
          className={`w-4 h-4 ${i < Math.round(rating) ? 'text-yellow-400 fill-yellow-400' : 'text-fg-muted'}`}
        />
      ))}
    </div>
  );
}

export default function AdminMarketplaceModuleDetail() {
  const { communityId, source, id } = useParams();
  const navigate = useNavigate();

  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [module, setModule] = useState(null);

  useEffect(() => {
    loadModule();
  }, [communityId, source, id]);

  const loadModule = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await unifiedMarketplaceApi.getCatalogEntry(source, id, { communityId });
      setModule(response.data?.module || response.data);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load module details');
    } finally {
      setLoading(false);
    }
  };

  const handleInstall = async () => {
    if (!module) return;
    try {
      setActionLoading(true);
      setError(null);
      await unifiedMarketplaceApi.installModule(communityId, {
        source: module.source,
        moduleId: module.sourceId || module.id,
      });
      setSuccess('Module installed successfully.');
      await loadModule();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to install module');
    } finally {
      setActionLoading(false);
    }
  };

  const handleUninstall = async () => {
    if (!module) return;
    if (!confirm('Are you sure you want to uninstall this module?')) return;
    try {
      setActionLoading(true);
      setError(null);
      await unifiedMarketplaceApi.uninstallModule(
        communityId,
        module.sourceId || module.id,
        module.source
      );
      setSuccess('Module uninstalled successfully.');
      await loadModule();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to uninstall module');
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-surface">
        <div className="text-yellow-400">Loading module details...</div>
      </div>
    );
  }

  if (!module && !loading) {
    return (
      <div className="min-h-screen bg-surface text-white p-6 flex flex-col items-center justify-center gap-4">
        <XCircleIcon className="w-16 h-16 text-red-400" />
        <p className="text-fg-muted">{error || 'Module not found.'}</p>
        <button
          onClick={() => navigate(`/admin/${communityId}/marketplace`)}
          className="flex items-center gap-2 px-4 py-2 bg-navy-700 hover:bg-navy-600 rounded-lg transition-colors"
        >
          <ArrowLeftIcon className="w-4 h-4" />
          Back to Marketplace
        </button>
      </div>
    );
  }

  const pricingBadge = PRICING_BADGES[module.pricingType] || PRICING_BADGES.free;
  const IntegrationIcon = INTEGRATION_ICONS[module.integrationType] || CommandLineIcon;

  return (
    <div className="min-h-screen bg-surface text-white p-6">
      <div className="max-w-4xl mx-auto">
        {/* Back Link */}
        <button
          onClick={() => navigate(`/admin/${communityId}/marketplace`)}
          className="flex items-center gap-2 text-fg-muted hover:text-white transition-colors mb-6 text-sm"
        >
          <ArrowLeftIcon className="w-4 h-4" />
          Back to Marketplace
        </button>

        {/* Alerts */}
        {error && (
          <div className="mb-6 flex items-center gap-2 bg-red-900/20 border border-red-500 text-red-400 px-4 py-3 rounded-lg">
            <XCircleIcon className="w-5 h-5 flex-shrink-0" />
            <span>{error}</span>
            <button onClick={() => setError(null)} className="ml-auto font-bold">×</button>
          </div>
        )}
        {success && (
          <div className="mb-6 flex items-center gap-2 bg-green-900/20 border border-green-500 text-green-400 px-4 py-3 rounded-lg">
            <CheckCircleIcon className="w-5 h-5 flex-shrink-0" />
            <span>{success}</span>
            <button onClick={() => setSuccess(null)} className="ml-auto font-bold">×</button>
          </div>
        )}

        {/* Module Header */}
        <div className="bg-surface-raised border border-line rounded-xl p-6 mb-6">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-center gap-4">
              {module.iconUrl ? (
                <img src={module.iconUrl} alt={module.displayName} className="w-16 h-16 rounded-xl" />
              ) : (
                <div className="w-16 h-16 bg-navy-700 rounded-xl flex items-center justify-center">
                  <TagIcon className="w-8 h-8 text-yellow-400" />
                </div>
              )}
              <div>
                <h1 className="text-2xl font-bold text-white">{module.displayName || module.name}</h1>
                <p className="text-fg-muted text-sm">by {module.author || module.vendorName}</p>
              </div>
            </div>

            {/* Install / Uninstall Button */}
            <div className="flex-shrink-0">
              {module.isInstalled ? (
                <button
                  onClick={handleUninstall}
                  disabled={actionLoading}
                  className="px-5 py-2.5 bg-red-600 hover:bg-red-700 text-white rounded-lg font-semibold transition-colors disabled:opacity-50"
                >
                  {actionLoading ? 'Removing...' : 'Uninstall'}
                </button>
              ) : (
                <button
                  onClick={handleInstall}
                  disabled={actionLoading}
                  className="flex items-center gap-2 px-5 py-2.5 bg-yellow-500 hover:bg-yellow-400 text-gray-900 rounded-lg font-semibold transition-colors disabled:opacity-50"
                >
                  <ArrowDownTrayIcon className="w-4 h-4" />
                  {actionLoading ? 'Installing...' : 'Install'}
                </button>
              )}
            </div>
          </div>

          {/* Badges Row */}
          <div className="flex flex-wrap items-center gap-2 mt-4">
            <span className={`inline-block border text-xs px-2 py-1 rounded ${pricingBadge.className}`}>
              {pricingBadge.label}
            </span>
            <span className="inline-block bg-navy-700 border border-line-strong text-fg-secondary text-xs px-2 py-1 rounded">
              {module.category}
            </span>
            <span className="inline-block bg-navy-700 border border-line-strong text-fg-secondary text-xs px-2 py-1 rounded">
              v{module.version}
            </span>
            {module.source && (
              <span className="inline-block bg-blue-900/40 border border-blue-600 text-blue-400 text-xs px-2 py-1 rounded">
                {module.source}
              </span>
            )}
            {module.integrationType && (
              <span className="inline-flex items-center gap-1 bg-navy-700 border border-line-strong text-fg-secondary text-xs px-2 py-1 rounded">
                <IntegrationIcon className="w-3 h-3" />
                {module.integrationType.replace('_', ' ')}
              </span>
            )}
            {module.communicationModel && (
              <span className="inline-block bg-navy-700 border border-line-strong text-fg-secondary text-xs px-2 py-1 rounded">
                {module.communicationModel === 'webhook_push' ? 'Webhook Push' : 'REST Pull'}
              </span>
            )}
            {module.isInstalled && (
              <span className="inline-flex items-center gap-1 bg-green-900/40 border border-green-600 text-green-400 text-xs px-2 py-1 rounded">
                <CheckCircleIcon className="w-3 h-3" />
                Installed
              </span>
            )}
          </div>

          {/* Stats Row */}
          <div className="flex items-center gap-6 mt-4 text-sm text-fg-muted">
            <div className="flex items-center gap-2">
              <StarRating rating={module.avgRating || 0} />
              <span>{(module.avgRating || 0).toFixed(1)}</span>
              <span>({module.reviewCount || 0} reviews)</span>
            </div>
            <div className="flex items-center gap-1">
              <ArrowDownTrayIcon className="w-4 h-4" />
              <span>{module.installCount || 0} installs</span>
            </div>
          </div>
        </div>

        {/* Description */}
        <div className="bg-surface-raised border border-line rounded-xl p-6 mb-6">
          <h2 className="text-lg font-semibold mb-3">Description</h2>
          <p className="text-fg-secondary leading-relaxed whitespace-pre-wrap">
            {module.description || 'No description provided.'}
          </p>
        </div>

        {/* Pricing Info */}
        {module.pricingType && module.pricingType !== 'free' && (
          <div className="bg-surface-raised border border-line rounded-xl p-6 mb-6">
            <h2 className="text-lg font-semibold mb-3">Pricing</h2>
            <div className="flex items-center gap-4 text-sm">
              <span className={`border px-3 py-1 rounded ${pricingBadge.className}`}>
                {pricingBadge.label}
              </span>
              {module.priceCents != null && (
                <span className="text-white font-medium">
                  ${(module.priceCents / 100).toFixed(2)}
                  {module.pricingType === 'subscription' ? ' / mo' : ''}
                </span>
              )}
            </div>
          </div>
        )}

        {/* Trigger Commands */}
        {module.triggerCommands && module.triggerCommands.length > 0 && (
          <div className="bg-surface-raised border border-line rounded-xl p-6 mb-6">
            <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
              <CommandLineIcon className="w-5 h-5 text-yellow-400" />
              Commands
            </h2>
            <div className="flex flex-wrap gap-2">
              {module.triggerCommands.map((cmd, idx) => (
                <code
                  key={idx}
                  className="bg-surface border border-line text-yellow-300 text-sm px-3 py-1 rounded font-mono"
                >
                  {cmd}
                </code>
              ))}
            </div>
          </div>
        )}

        {/* Reviews */}
        {module.reviews && module.reviews.length > 0 && (
          <div className="bg-surface-raised border border-line rounded-xl p-6">
            <h2 className="text-lg font-semibold mb-4">Reviews</h2>
            <div className="space-y-4">
              {module.reviews.map((review, idx) => (
                <div key={idx} className="border-b border-line pb-4 last:border-0 last:pb-0">
                  <div className="flex items-center justify-between mb-1">
                    <span className="font-medium text-white text-sm">
                      {review.username || review.userId || 'Anonymous'}
                    </span>
                    <StarRating rating={review.rating || 0} />
                  </div>
                  {review.comment && (
                    <p className="text-fg-muted text-sm">{review.comment}</p>
                  )}
                  {review.createdAt && (
                    <p className="text-fg-muted text-xs mt-1">
                      {new Date(review.createdAt).toLocaleDateString()}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
