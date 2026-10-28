import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { platformApi } from '../../services/api';
import { UserGroupIcon, HomeIcon, ServerIcon, ChartBarIcon } from '@heroicons/react/24/outline';
import { getAllPlatformOptions } from '../../utils/platformConfig';

function PlatformDashboard() {
  const [stats, setStats] = useState(null);
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchData() {
      try {
        const [statsRes, healthRes] = await Promise.all([
          platformApi.getStats(),
          platformApi.getHealth(),
        ]);
        setStats(statsRes.data.stats);
        setHealth(healthRes.data);
      } catch (err) {
        console.error('Failed to fetch data:', err);
      } finally {
        setLoading(false);
      }
    }
    fetchData();
  }, []);

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-waddle-orange"></div>
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Platform Dashboard</h1>

      {/* Health Status */}
      <div className="card p-4 mb-6">
        <div className="flex items-center space-x-3">
          <div className={`w-3 h-3 rounded-full ${health?.status === 'healthy' ? 'bg-green-500' : 'bg-red-500'}`}></div>
          <span className="font-medium">System Status: {health?.status || 'Unknown'}</span>
        </div>
      </div>

      {/* Stats Grid */}
      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <Link to="/platform/users" className="card p-6 hover:shadow-md transition-shadow">
          <UserGroupIcon className="w-8 h-8 text-blue-500 mb-3" />
          <div className="text-3xl font-bold">{stats?.users?.total || 0}</div>
          <div className="text-fg-muted">Total Users</div>
          <div className="text-sm text-green-600 mt-1">
            {stats?.users?.active7d || 0} active this week
          </div>
        </Link>

        <Link to="/platform/communities" className="card p-6 hover:shadow-md transition-shadow">
          <HomeIcon className="w-8 h-8 text-purple-500 mb-3" />
          <div className="text-3xl font-bold">{stats?.communities?.total || 0}</div>
          <div className="text-fg-muted">Communities</div>
          <div className="text-sm text-fg-muted mt-1">
            {stats?.communities?.public || 0} public
          </div>
        </Link>

        <div className="card p-6">
          <ServerIcon className="w-8 h-8 text-green-500 mb-3" />
          <div className="text-3xl font-bold">{stats?.sessions?.active || 0}</div>
          <div className="text-fg-muted">Active Sessions</div>
          <div className="text-sm text-fg-muted mt-1">
            {stats?.sessions?.last24h || 0} in 24h
          </div>
        </div>

        <div className="card p-6">
          <ChartBarIcon className="w-8 h-8 text-orange-500 mb-3" />
          <div className="text-3xl font-bold">
            {stats?.communities?.totalMembers?.toLocaleString() || 0}
          </div>
          <div className="text-fg-muted">Total Memberships</div>
        </div>
      </div>

      {/* Platform Breakdown */}
      <div className="card">
        <div className="card-header">
          <h2 className="font-semibold">Users by Platform</h2>
        </div>
        <div className="p-6">
          <div className="grid sm:grid-cols-3 lg:grid-cols-5 gap-6">
            {getAllPlatformOptions().map((p) => (
              <div key={p.id} className="flex items-center space-x-4">
                <div
                  className="w-12 h-12 rounded-lg flex items-center justify-center"
                  style={{ backgroundColor: `${p.hex}15` }}
                >
                  <span className="text-xl">{p.icon}</span>
                </div>
                <div>
                  <div className="text-2xl font-bold">{stats?.platforms?.[p.id] || 0}</div>
                  <div className="text-fg-muted">{p.label}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export default PlatformDashboard;
