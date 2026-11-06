/**
 * Vendor Dashboard
 * Shows vendor account overview, submissions, revenue, and key metrics
 */
import { useEffect, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import {
  CheckCircleIcon,
  ClockIcon,
  XCircleIcon,
  CurrencyDollarIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';
import { vendorApi } from '../../services/api';

function VendorDashboard() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [dashboard, setDashboard] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    loadDashboard();
  }, []);

  const loadDashboard = async () => {
    try {
      setLoading(true);
      // Fetch vendor dashboard data
      const response = await vendorApi.getDashboard();
      setDashboard(response.data);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load dashboard');
      setDashboard({
        stats: {
          totalSubmissions: 0,
          approvedModules: 0,
          pendingReview: 0,
          totalRevenue: 0,
          expectedRevenue: 0,
        },
        recentSubmissions: [],
      });
    } finally {
      setLoading(false);
    }
  };

  const formatCurrency = (amount) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
    }).format(amount);
  };

  const StatCard = ({ icon: Icon, label, value, color = 'blue' }) => {
    const colors = {
      blue: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
      green: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
      orange: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
      red: 'bg-red-500/10 text-red-400 border-red-500/20',
    };

    return (
      <div className={`${colors[color]} border rounded-lg p-6`}>
        <div className="flex items-start justify-between">
          <div>
            <p className="text-sm font-medium text-navy-300">{label}</p>
            <p className="text-3xl font-bold mt-2">{value}</p>
          </div>
          <Icon className="w-8 h-8 opacity-50" />
        </div>
      </div>
    );
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  const stats = dashboard?.stats || {};

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-white">Vendor Dashboard</h1>
          <p className="text-navy-300 mt-1">Manage your module submissions and track revenue</p>
        </div>
        <Link
          to="/vendor/submit"
          className="flex items-center space-x-2 bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 rounded-lg transition-colors"
        >
          <PlusIcon className="w-5 h-5" />
          <span>Submit New Module</span>
        </Link>
      </div>

      {/* Error Message */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg">
          {error}
        </div>
      )}

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <StatCard
          icon={CheckCircleIcon}
          label="Approved Modules"
          value={stats.approvedModules || 0}
          color="green"
        />
        <StatCard
          icon={ClockIcon}
          label="Pending Review"
          value={stats.pendingReview || 0}
          color="orange"
        />
        <StatCard
          icon={CurrencyDollarIcon}
          label="Total Revenue"
          value={formatCurrency(stats.totalRevenue || 0)}
          color="blue"
        />
        <StatCard
          icon={CurrencyDollarIcon}
          label="Expected Revenue"
          value={formatCurrency(stats.expectedRevenue || 0)}
          color="blue"
        />
      </div>

      {/* Revenue Breakdown */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4">Revenue Breakdown</h2>

        {dashboard?.revenueBreakdown && dashboard.revenueBreakdown.length > 0 ? (
          <div className="space-y-4">
            {dashboard.revenueBreakdown.map((item, idx) => (
              <div key={idx} className="flex items-center justify-between p-4 bg-navy-900 rounded-lg">
                <div>
                  <p className="text-white font-medium">{item.moduleName}</p>
                  <p className="text-sm text-navy-400">
                    {item.subscriptions} subscription{item.subscriptions !== 1 ? 's' : ''}
                  </p>
                </div>
                <div className="text-right">
                  <p className="text-emerald-400 font-bold">{formatCurrency(item.monthlyRevenue)}/mo</p>
                  <p className="text-xs text-navy-400">{formatCurrency(item.totalRevenue)} total</p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-navy-400 text-center py-8">
            No approved modules yet. Submit one to start earning revenue!
          </p>
        )}
      </div>

      {/* Recent Submissions */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4">Recent Submissions</h2>

        {dashboard?.recentSubmissions && dashboard.recentSubmissions.length > 0 ? (
          <div className="space-y-3">
            {dashboard.recentSubmissions.map((submission) => (
              <Link
                key={submission.id}
                to={`/vendor/submissions`}
                className="flex items-center justify-between p-4 bg-navy-900 hover:bg-navy-850 rounded-lg transition-colors"
              >
                <div className="flex-1">
                  <p className="text-white font-medium">{submission.moduleName}</p>
                  <p className="text-xs text-navy-400">
                    Submitted {new Date(submission.submittedAt).toLocaleDateString()}
                  </p>
                </div>
                <div className="flex items-center space-x-4">
                  <span className="text-sm">
                    {submission.status === 'approved' && (
                      <span className="flex items-center space-x-1 text-emerald-400">
                        <CheckCircleIcon className="w-4 h-4" />
                        <span>Approved</span>
                      </span>
                    )}
                    {submission.status === 'pending' && (
                      <span className="flex items-center space-x-1 text-orange-400">
                        <ClockIcon className="w-4 h-4" />
                        <span>Pending</span>
                      </span>
                    )}
                    {submission.status === 'rejected' && (
                      <span className="flex items-center space-x-1 text-red-400">
                        <XCircleIcon className="w-4 h-4" />
                        <span>Rejected</span>
                      </span>
                    )}
                    {submission.status === 'under-review' && (
                      <span className="flex items-center space-x-1 text-yellow-400">
                        <ClockIcon className="w-4 h-4" />
                        <span>Under Review</span>
                      </span>
                    )}
                  </span>
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <p className="text-navy-400 text-center py-8">
            No submissions yet. <Link to="/vendor/submit" className="text-emerald-400 hover:underline">Submit your first module!</Link>
          </p>
        )}
      </div>

      {/* Quick Links */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <Link
          to="/vendor/submit"
          className="bg-emerald-500/10 border border-emerald-500/20 hover:bg-emerald-500/20 rounded-lg p-6 transition-colors"
        >
          <h3 className="text-emerald-400 font-bold mb-2">Submit Module</h3>
          <p className="text-sm text-navy-300">Submit a new vendor module for review and approval</p>
        </Link>

        <Link
          to="/vendor/submissions"
          className="bg-blue-500/10 border border-blue-500/20 hover:bg-blue-500/20 rounded-lg p-6 transition-colors"
        >
          <h3 className="text-blue-400 font-bold mb-2">My Submissions</h3>
          <p className="text-sm text-navy-300">View and manage all your module submissions</p>
        </Link>

        <div className="bg-purple-500/10 border border-purple-500/20 rounded-lg p-6">
          <h3 className="text-purple-400 font-bold mb-2">Documentation</h3>
          <p className="text-sm text-navy-300">Learn how to create and submit vendor modules</p>
        </div>
      </div>
    </div>
  );
}

export default VendorDashboard;
