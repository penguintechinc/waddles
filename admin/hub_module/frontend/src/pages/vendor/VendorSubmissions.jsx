/**
 * Vendor Submissions List
 * Shows all vendor's module submissions with status and actions
 */
import { useEffect, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import {
  CheckCircleIcon,
  ClockIcon,
  XCircleIcon,
  EyeIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';

function VendorSubmissions() {
  const navigate = useNavigate();
  const [submissions, setSubmissions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filters, setFilters] = useState({
    status: 'all',
    sortBy: 'newest',
  });

  useEffect(() => {
    loadSubmissions();
  }, [filters]);

  const loadSubmissions = async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams();
      if (filters.status !== 'all') params.append('status', filters.status);
      params.append('sortBy', filters.sortBy);

      const response = await api.get(`/vendor/submissions?${params.toString()}`);
      setSubmissions(response.data?.submissions || []);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load submissions');
      setSubmissions([]);
    } finally {
      setLoading(false);
    }
  };

  const getStatusIcon = (status) => {
    switch (status) {
      case 'approved':
        return <CheckCircleIcon className="w-5 h-5 text-emerald-400" />;
      case 'pending':
        return <ClockIcon className="w-5 h-5 text-orange-400" />;
      case 'rejected':
        return <XCircleIcon className="w-5 h-5 text-red-400" />;
      case 'under-review':
        return <ClockIcon className="w-5 h-5 text-yellow-400" />;
      default:
        return <ClockIcon className="w-5 h-5 text-navy-400" />;
    }
  };

  const getStatusColor = (status) => {
    switch (status) {
      case 'approved':
        return 'text-emerald-400 bg-emerald-500/10';
      case 'pending':
        return 'text-orange-400 bg-orange-500/10';
      case 'rejected':
        return 'text-red-400 bg-red-500/10';
      case 'under-review':
        return 'text-yellow-400 bg-yellow-500/10';
      default:
        return 'text-navy-400 bg-navy-500/10';
    }
  };

  const getStatusLabel = (status) => {
    switch (status) {
      case 'approved':
        return 'Approved';
      case 'pending':
        return 'Pending';
      case 'rejected':
        return 'Rejected';
      case 'under-review':
        return 'Under Review';
      default:
        return status.charAt(0).toUpperCase() + status.slice(1);
    }
  };

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
          <h1 className="text-3xl font-bold text-white">My Submissions</h1>
          <p className="text-navy-300 mt-1">View and manage your module submissions</p>
        </div>
        <Link
          to="/vendor/submit"
          className="bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 rounded-lg transition-colors"
        >
          Submit New Module
        </Link>
      </div>

      {/* Filters */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-4">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label htmlFor="vendor-submissions-status" className="block text-sm font-medium text-navy-300 mb-2">Status</label>
            <select
              id="vendor-submissions-status"
              value={filters.status}
              onChange={(e) => setFilters({ ...filters, status: e.target.value })}
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:border-gold-400"
            >
              <option value="all">All Statuses</option>
              <option value="pending">Pending</option>
              <option value="under-review">Under Review</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
            </select>
          </div>
          <div>
            <label htmlFor="vendor-submissions-sort-by" className="block text-sm font-medium text-navy-300 mb-2">Sort By</label>
            <select
              id="vendor-submissions-sort-by"
              value={filters.sortBy}
              onChange={(e) => setFilters({ ...filters, sortBy: e.target.value })}
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:border-gold-400"
            >
              <option value="newest">Newest First</option>
              <option value="oldest">Oldest First</option>
              <option value="name">Module Name (A-Z)</option>
            </select>
          </div>
        </div>
      </div>

      {/* Error Message */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg">
          {error}
        </div>
      )}

      {/* Submissions List */}
      {submissions.length > 0 ? (
        <div className="space-y-4">
          {submissions.map((submission) => (
            <div
              key={submission.id}
              className="bg-navy-800 border border-navy-700 rounded-lg p-6 hover:border-navy-600 transition-colors"
            >
              <div className="flex items-start justify-between mb-4">
                <div className="flex-1">
                  <h3 className="text-xl font-bold text-white">{submission.moduleName}</h3>
                  <p className="text-sm text-navy-400 mt-1">{submission.moduleDescription}</p>
                </div>
                <div className={`flex items-center space-x-2 px-3 py-1 rounded-full text-sm font-medium ${getStatusColor(submission.status)}`}>
                  {getStatusIcon(submission.status)}
                  <span>{getStatusLabel(submission.status)}</span>
                </div>
              </div>

              <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4 pt-4 border-t border-navy-700">
                <div>
                  <p className="text-xs text-navy-400 uppercase tracking-wider">Version</p>
                  <p className="text-white font-medium mt-1">{submission.moduleVersion || 'N/A'}</p>
                </div>
                <div>
                  <p className="text-xs text-navy-400 uppercase tracking-wider">Category</p>
                  <p className="text-white font-medium mt-1 capitalize">{submission.moduleCategory || 'N/A'}</p>
                </div>
                <div>
                  <p className="text-xs text-navy-400 uppercase tracking-wider">Submitted</p>
                  <p className="text-white font-medium mt-1">
                    {new Date(submission.submittedAt).toLocaleDateString()}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-navy-400 uppercase tracking-wider">Scopes</p>
                  <p className="text-white font-medium mt-1">{submission.scopeCount || 0}</p>
                </div>
              </div>

              {/* Status-specific Messages */}
              {submission.status === 'rejected' && submission.rejectionReason && (
                <div className="bg-red-500/10 border border-red-500/20 text-red-300 px-3 py-2 rounded text-sm mb-4">
                  <p className="font-medium">Rejection Reason:</p>
                  <p>{submission.rejectionReason}</p>
                </div>
              )}

              {submission.status === 'under-review' && submission.adminNotes && (
                <div className="bg-yellow-500/10 border border-yellow-500/20 text-yellow-300 px-3 py-2 rounded text-sm mb-4">
                  <p className="font-medium">Admin Notes:</p>
                  <p>{submission.adminNotes}</p>
                </div>
              )}

              {/* Actions */}
              <div className="flex items-center space-x-3 pt-4 border-t border-navy-700">
                <button
                  onClick={() => navigate(`/vendor/submissions/${submission.id}`)}
                  className="flex items-center space-x-2 text-sky-400 hover:text-sky-300 transition-colors"
                >
                  <EyeIcon className="w-4 h-4" />
                  <span>View Details</span>
                </button>
                {submission.status === 'rejected' && (
                  <Link
                    to="/vendor/submit"
                    className="flex items-center space-x-2 text-emerald-400 hover:text-emerald-300 transition-colors ml-4 pl-4 border-l border-navy-700"
                  >
                    <span>Resubmit</span>
                  </Link>
                )}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <p className="text-navy-400 mb-4">No submissions found</p>
          <Link
            to="/vendor/submit"
            className="inline-block bg-emerald-600 hover:bg-emerald-700 text-white px-6 py-2 rounded-lg transition-colors"
          >
            Submit Your First Module
          </Link>
        </div>
      )}
    </div>
  );
}

export default VendorSubmissions;
