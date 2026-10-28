import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { UserGroupIcon, PuzzlePieceIcon, ComputerDesktopIcon, GlobeAltIcon } from '@heroicons/react/24/outline';

function AdminHome() {
  const { communityId } = useParams();
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchStats() {
      try {
        const [members, modules, sources, domains] = await Promise.all([
          adminApi.getMembers(communityId, { limit: 1 }),
          adminApi.getModules(communityId),
          adminApi.getBrowserSources(communityId),
          adminApi.getDomains(communityId),
        ]);
        setStats({
          memberCount: members.data.pagination?.total || 0,
          moduleCount: modules.data.modules?.length || 0,
          sourceCount: sources.data.sources?.length || 0,
          domainCount: domains.data.domains?.length || 0,
        });
      } catch (err) {
        console.error('Failed to fetch stats:', err);
      } finally {
        setLoading(false);
      }
    }
    fetchStats();
  }, [communityId]);

  const cards = [
    { to: `/admin/${communityId}/members`, icon: UserGroupIcon, label: 'Members', value: stats?.memberCount, color: 'bg-blue-500' },
    { to: `/admin/${communityId}/modules`, icon: PuzzlePieceIcon, label: 'Modules', value: stats?.moduleCount, color: 'bg-purple-500' },
    { to: `/admin/${communityId}/browser-sources`, icon: ComputerDesktopIcon, label: 'Browser Sources', value: stats?.sourceCount, color: 'bg-green-500' },
    { to: `/admin/${communityId}/domains`, icon: GlobeAltIcon, label: 'Custom Domains', value: stats?.domainCount, color: 'bg-orange-500' },
  ];

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Community Admin</h1>

      {loading ? (
        <div className="flex justify-center py-12">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-waddle-orange"></div>
        </div>
      ) : (
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-6">
          {cards.map((card) => (
            <Link key={card.to} to={card.to} className="card hover:shadow-md transition-shadow">
              <div className="p-6">
                <div className={`w-12 h-12 rounded-lg ${card.color} flex items-center justify-center mb-4`}>
                  <card.icon className="w-6 h-6 text-white" />
                </div>
                <div className="text-3xl font-bold">{card.value ?? '-'}</div>
                <div className="text-fg-muted">{card.label}</div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

export default AdminHome;
