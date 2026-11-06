/**
 * Vendor Modules
 * Lists the vendor's published and draft modules with status, install count, and rating
 */
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  CubeIcon,
  PlusIcon,
  StarIcon,
  ArrowDownTrayIcon,
  CheckCircleIcon,
  ClockIcon,
  XCircleIcon,
  PencilSquareIcon,
} from '@heroicons/react/24/outline';
import { vendorApi } from '../../services/api';

function VendorModules() {
  const [modules, setModules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('all');

  useEffect(() => {
    loadModules();
  }, []);

  const loadModules = async () => {
    try {
      setLoading(true);
      const response = await vendorApi.getModules();
      setModules(response.data?.modules || []);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load modules');
      setModules([]);
    } finally {
      setLoading(false);
    }
  };

  const getStatusConfig = (status) => {
    switch (status) {
      case 'published':
        return {
          label: 'Published',
          icon: <CheckCircleIcon className="w-4 h-4" />,
          classes: 'text-emerald-400 bg-emerald-500/10 border border-emerald-500/20',
        };
      case 'pending':
        return {
          label: 'Pending Review',
          icon: <ClockIcon className="w-4 h-4" />,
          classes: 'text-orange-400 bg-orange-500/10 border border-orange-500/20',
        };
      case 'rejected':
        return {
          label: 'Rejected',
          icon: <XCircleIcon className="w-4 h-4" />,
          classes: 'text-red-400 bg-red-500/10 border border-red-500/20',
        };
      case 'draft':
      default:
        return {
          label: 'Draft',
          icon: <PencilSquareIcon className="w-4 h-4" />,
          classes: 'text-navy-300 bg-navy-700/50 border border-navy-600',
        };
    }
  };

  const filteredModules = filter === 'all'
    ? modules
    : modules.filter((m) => m.status === filter);

  const statusCounts = modules.reduce((acc, m) => {
    acc[m.status] = (acc[m.status] || 0) + 1;
    return acc;
  }, {});

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-white">My Modules</h1>
          <p className="text-navy-300 mt-1">Manage your published and draft modules</p>
        </div>
        <Link
          to="/vendor/submit"
          className="flex items-center space-x-2 bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 rounded-lg transition-colors"
        >
          <PlusIcon className="w-5 h-5" />
          <span>Submit New Module</span>
        </Link>
      </div>

      {/* Error */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg">
          {error}
        </div>
      )}

      {/* Filter tabs */}
      <div className="flex space-x-2 border-b border-navy-700 pb-0">
        {[
          { key: 'all', label: 'All', count: modules.length },
          { key: 'published', label: 'Published', count: statusCounts.published || 0 },
          { key: 'pending', label: 'Pending', count: statusCounts.pending || 0 },
          { key: 'draft', label: 'Draft', count: statusCounts.draft || 0 },
          { key: 'rejected', label: 'Rejected', count: statusCounts.rejected || 0 },
        ].map((tab) => (
          <button
            key={tab.key}
            onClick={() => setFilter(tab.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors -mb-px ${
              filter === tab.key
                ? 'border-sky-400 text-sky-400'
                : 'border-transparent text-navy-400 hover:text-navy-200'
            }`}
          >
            {tab.label}
            {tab.count > 0 && (
              <span className="ml-2 text-xs bg-navy-700 text-navy-300 px-1.5 py-0.5 rounded-full">
                {tab.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Module grid */}
      {filteredModules.length > 0 ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {filteredModules.map((mod) => {
            const statusConfig = getStatusConfig(mod.status);
            return (
              <div
                key={mod.id}
                className="bg-navy-800 border border-navy-700 rounded-lg p-6 hover:border-navy-600 transition-colors flex flex-col"
              >
                {/* Module icon + name */}
                <div className="flex items-start justify-between mb-4">
                  <div className="flex items-center space-x-3 flex-1 min-w-0">
                    <div className="w-10 h-10 rounded-lg bg-sky-500/10 border border-sky-500/20 flex items-center justify-center flex-shrink-0">
                      <CubeIcon className="w-5 h-5 text-sky-400" />
                    </div>
                    <div className="min-w-0">
                      <h3 className="text-white font-semibold truncate">{mod.name}</h3>
                      <p className="text-xs text-navy-400 mt-0.5">v{mod.version || '1.0.0'}</p>
                    </div>
                  </div>
                  <span className={`flex items-center space-x-1 px-2 py-1 rounded-full text-xs font-medium ml-2 flex-shrink-0 ${statusConfig.classes}`}>
                    {statusConfig.icon}
                    <span>{statusConfig.label}</span>
                  </span>
                </div>

                {/* Description */}
                {mod.description && (
                  <p className="text-sm text-navy-300 mb-4 line-clamp-2 flex-1">{mod.description}</p>
                )}

                {/* Stats */}
                <div className="flex items-center space-x-4 pt-4 border-t border-navy-700 mt-auto">
                  <div className="flex items-center space-x-1 text-navy-400 text-sm">
                    <ArrowDownTrayIcon className="w-4 h-4" />
                    <span>{(mod.installCount || 0).toLocaleString()}</span>
                  </div>
                  {mod.rating != null && (
                    <div className="flex items-center space-x-1 text-navy-400 text-sm">
                      <StarIcon className="w-4 h-4 text-gold-400" />
                      <span className="text-gold-400">{mod.rating.toFixed(1)}</span>
                      {mod.reviewCount != null && (
                        <span className="text-navy-500">({mod.reviewCount})</span>
                      )}
                    </div>
                  )}
                  <div className="ml-auto">
                    <Link
                      to={`/vendor/submissions`}
                      className="text-sky-400 hover:text-sky-300 text-sm transition-colors"
                    >
                      View Details
                    </Link>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <CubeIcon className="w-12 h-12 text-navy-600 mx-auto mb-4" />
          <p className="text-navy-400 mb-4">
            {filter === 'all' ? 'No modules yet.' : `No ${filter} modules.`}
          </p>
          {filter === 'all' && (
            <Link
              to="/vendor/submit"
              className="inline-block bg-emerald-600 hover:bg-emerald-700 text-white px-6 py-2 rounded-lg transition-colors"
            >
              Submit Your First Module
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

export default VendorModules;
