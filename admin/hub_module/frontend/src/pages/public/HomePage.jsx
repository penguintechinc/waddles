import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { publicApi } from '../../services/api';
import { getAllPlatformOptions, getPlatformConfig } from '../../utils/platformConfig';

const COMMUNITY_TYPE_ICONS = {
  creator: '🎨',
  gaming: '🎮',
  corporate: '🏢',
  shared_interest_group: '🤝',
  workforce: '🏗️',
  support: '🎧',
  other: '🌐',
};

/** Render an ISO timestamp as a coarse "N min ago" freshness label. */
function formatAge(iso) {
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  return mins < 1 ? 'just now' : `${mins} min ago`;
}

function HomePage() {
  const [stats, setStats] = useState(null);
  const [spotlighted, setSpotlighted] = useState([]);
  const [loading, setLoading] = useState(true);
  const [computedAt, setComputedAt] = useState(null);
  const [warming, setWarming] = useState(false);

  useEffect(() => {
    async function fetchData() {
      try {
        const [statsRes, spotlightRes] = await Promise.all([
          publicApi.getStats().catch((err) => {
            // 503 {"status":"warming"}: stats snapshot not yet computed by the CronJob.
            if (err?.response?.status === 503) {
              setWarming(true);
              return { data: {} };
            }
            throw err;
          }),
          publicApi.getSpotlightedCommunities().catch(() => ({ data: { communities: [] } })),
        ]);
        setStats(statsRes.data.stats || null);
        setComputedAt(statsRes.data.computedAt || null);
        setSpotlighted(spotlightRes.data?.communities || []);
      } catch (err) {
        console.error('Failed to fetch data:', err);
      } finally {
        setLoading(false);
      }
    }
    fetchData();
  }, []);

  return (
    <div>
      {/* Hero section */}
      <section className="bg-gradient-to-br from-navy-900 via-navy-800 to-navy-900 py-20">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
          <h1 className="text-4xl md:text-5xl font-bold mb-6 gradient-text">
            Unite Your Workforce &amp; Communities
          </h1>
          <p className="text-xl text-navy-300 max-w-2xl mx-auto mb-8">
            Waddles brings your teams and communities together across every platform with
            powerful tools for engagement, moderation, and growth.
          </p>
          <div className="flex flex-col sm:flex-row justify-center gap-4">
            <Link to="/login" className="btn btn-primary px-8 py-3">
              Get Started
            </Link>
            <Link to="/communities" className="btn btn-outline px-8 py-3">
              Browse Communities
            </Link>
          </div>
        </div>
      </section>

      {/* Stats section — dynamic platforms */}
      <section className="py-16 bg-navy-900 border-t border-navy-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-8 text-center">
            <div>
              <div className="text-4xl font-bold text-gold-400">
                {loading ? '...' : stats?.communities || 0}
              </div>
              <div className="text-navy-400 mt-1">Communities</div>
            </div>
            {getAllPlatformOptions().slice(0, 5).map((p) => {
              const platformStats = stats?.[p.id];
              const count = typeof platformStats === 'object'
                ? (platformStats.servers || platformStats.channels || 0)
                : (platformStats || 0);
              return (
                <div key={p.id}>
                  <div className="text-4xl font-bold" style={{ color: p.hex }}>
                    {loading ? '...' : count}
                  </div>
                  <div className="text-navy-400 mt-1">{p.icon} {p.label}</div>
                </div>
              );
            })}
          </div>
          {warming && (
            <p className="text-center text-xs text-navy-500 mt-6">Stats are warming up...</p>
          )}
          {computedAt && (
            <p className="text-center text-xs text-navy-500 mt-6">
              Updated {formatAge(computedAt)}
            </p>
          )}
        </div>
      </section>

      {/* Features section */}
      <section className="py-20 bg-navy-950">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <h2 className="text-3xl font-bold text-center mb-12 text-sky-100">
            Everything You Need to Build &amp; Manage Your Community or Workforce
          </h2>
          <div className="grid md:grid-cols-3 gap-8">
            <div className="card p-6">
              <div className="text-3xl mb-4">🎮</div>
              <h3 className="text-xl font-semibold mb-2 text-sky-100">Multi-Platform</h3>
              <p className="text-navy-400">
                Connect Discord, Twitch, Slack, YouTube, Kick, Telegram, and more under one unified system.
              </p>
            </div>
            <div className="card p-6">
              <div className="text-3xl mb-4">🏆</div>
              <h3 className="text-xl font-semibold mb-2 text-sky-100">Reputation System</h3>
              <p className="text-navy-400">
                Track engagement and reward your most active community members.
              </p>
            </div>
            <div className="card p-6">
              <div className="text-3xl mb-4">🔧</div>
              <h3 className="text-xl font-semibold mb-2 text-sky-100">Modular Design</h3>
              <p className="text-navy-400">
                Add features with app bundles - from AI chat to music integration.
              </p>
            </div>
            <div className="card p-6">
              <div className="text-3xl mb-4">📺</div>
              <h3 className="text-xl font-semibold mb-2 text-sky-100">Browser Sources</h3>
              <p className="text-navy-400">
                Integrated OBS overlays for streams - tickers, alerts, and media.
              </p>
            </div>
            <div className="card p-6">
              <div className="text-3xl mb-4">📊</div>
              <h3 className="text-xl font-semibold mb-2 text-sky-100">Analytics</h3>
              <p className="text-navy-400">
                Understand your community with detailed engagement metrics.
              </p>
            </div>
            <div className="card p-6">
              <div className="text-3xl mb-4">🔐</div>
              <h3 className="text-xl font-semibold mb-2 text-sky-100">Role Management</h3>
              <p className="text-navy-400">
                Flexible permissions with labels, roles, and identity linking.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Spotlighted Communities */}
      {spotlighted.length > 0 && (
        <section className="py-20 bg-navy-900 border-t border-navy-700">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <h2 className="text-3xl font-bold text-center mb-12 text-sky-100">
              Spotlighted Communities
            </h2>
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-6">
              {spotlighted.map((c) => (
                <Link
                  key={c.id}
                  to={`/communities/${c.id}`}
                  className="card p-6 hover:ring-2 hover:ring-gold-500/50 transition"
                >
                  <div className="flex items-center gap-3 mb-3">
                    <span className="text-2xl">
                      {COMMUNITY_TYPE_ICONS[c.communityType] || '🌐'}
                    </span>
                    <h3 className="text-lg font-semibold text-sky-100 truncate">
                      {c.displayName}
                    </h3>
                  </div>
                  {c.description && (
                    <p className="text-navy-400 text-sm line-clamp-2 mb-3">{c.description}</p>
                  )}
                  <div className="flex items-center justify-between text-xs text-navy-500">
                    <span>{c.memberCount.toLocaleString()} members</span>
                    {c.platform && (
                      <span className="flex items-center gap-1">
                        {getPlatformConfig(c.platform).icon} {getPlatformConfig(c.platform).label}
                      </span>
                    )}
                  </div>
                </Link>
              ))}
            </div>
          </div>
        </section>
      )}

      {/* CTA section */}
      <section className="py-20 bg-gradient-to-r from-navy-900 via-navy-800 to-navy-900 border-t border-navy-700">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
          <h2 className="text-3xl font-bold mb-4 text-sky-100">Ready to Grow Your Community or Workforce?</h2>
          <p className="text-navy-400 mb-8">
            Join hundreds of communities already using Waddles to engage their audience.
          </p>
          <Link to="/login" className="btn btn-primary px-8 py-3">
            Start Free Today
          </Link>
        </div>
      </section>
    </div>
  );
}

export default HomePage;
