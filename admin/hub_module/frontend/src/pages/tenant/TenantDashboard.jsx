import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { tenantApi } from '../../services/api';

function TenantDashboard() {
  const { tenantSlug } = useParams();
  const [tenant, setTenant] = useState(null);
  const [stats, setStats] = useState({ communityCount: 0, adminCount: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  useEffect(() => {
    loadTenant();
  }, [tenantSlug]);

  const loadTenant = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await tenantApi.getTenant(tenantSlug);
      const t = res.data.tenant;
      setTenant(t);
      setForm({
        displayName: t.displayName || '',
        description: t.description || '',
        logoUrl: t.logoUrl || '',
      });
      setStats({
        communityCount: res.data.communityCount ?? t.communityCount ?? 0,
        adminCount: res.data.adminCount ?? t.adminCount ?? 0,
      });
    } catch (err) {
      setError(err?.response?.data?.error || 'Failed to load tenant.');
    }
    setLoading(false);
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      await tenantApi.updateTenant(tenantSlug, form);
      await loadTenant();
      setEditing(false);
    } catch (err) {
      setSaveError(err?.response?.data?.error || 'Failed to save changes.');
    }
    setSaving(false);
  };

  const handleCancel = () => {
    if (tenant) {
      setForm({
        displayName: tenant.displayName || '',
        description: tenant.description || '',
        logoUrl: tenant.logoUrl || '',
      });
    }
    setEditing(false);
    setSaveError(null);
  };

  const quickLinks = [
    {
      label: 'Modules',
      description: 'Manage allowed modules for this tenant',
      to: `/tenant/${tenantSlug}/modules`,
      icon: (
        <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zm10 0a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zm10 0a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"
          />
        </svg>
      ),
    },
    {
      label: 'Admins',
      description: 'Add or remove tenant administrators',
      to: `/tenant/${tenantSlug}/admins`,
      icon: (
        <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"
          />
        </svg>
      ),
    },
    {
      label: 'Communities',
      description: 'View and manage communities in this tenant',
      to: `/tenant/${tenantSlug}/communities`,
      icon: (
        <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"
          />
        </svg>
      ),
    },
  ];

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-64">
        <div className="animate-spin rounded-full h-10 w-10 border-t-2 border-b-2 border-gold-400" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <div className="bg-red-900/30 border border-red-700 rounded-xl p-4 text-red-300">
          {error}
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-sky-100 text-2xl font-bold">
            {tenant?.displayName || tenantSlug}
          </h1>
          <p className="text-sky-400 text-sm mt-1">Tenant Dashboard</p>
        </div>
        {!editing && (
          <button
            onClick={() => setEditing(true)}
            className="flex items-center gap-2 px-4 py-2 bg-gold-500 text-navy-900 rounded-lg font-semibold hover:bg-gold-400 transition-colors text-sm"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
              />
            </svg>
            Edit
          </button>
        )}
      </div>

      {/* Tenant Info / Edit Form */}
      <div className="bg-navy-800 border border-navy-700 rounded-xl p-6">
        {editing ? (
          <div className="space-y-4">
            <h2 className="text-sky-100 text-lg font-semibold mb-4">Edit Tenant Info</h2>
            {saveError && (
              <div className="bg-red-900/30 border border-red-700 rounded-lg p-3 text-red-300 text-sm">
                {saveError}
              </div>
            )}
            <div>
              <label htmlFor="tenant-dashboard-display-name" className="block text-sky-300 text-sm font-medium mb-1">
                Display Name
              </label>
              <input
                id="tenant-dashboard-display-name"
                type="text"
                value={form.displayName}
                onChange={(e) => setForm((f) => ({ ...f, displayName: e.target.value }))}
                className="w-full bg-navy-700 border border-navy-600 rounded-lg px-3 py-2 text-sky-100 placeholder-sky-600 focus:border-gold-500 text-sm"
                placeholder="Tenant display name"
              />
            </div>
            <div>
              <label htmlFor="tenant-dashboard-description" className="block text-sky-300 text-sm font-medium mb-1">
                Description
              </label>
              <textarea
                id="tenant-dashboard-description"
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                rows={3}
                className="w-full bg-navy-700 border border-navy-600 rounded-lg px-3 py-2 text-sky-100 placeholder-sky-600 focus:border-gold-500 text-sm resize-none"
                placeholder="Brief description of this tenant"
              />
            </div>
            <div>
              <label htmlFor="tenant-dashboard-logo-url" className="block text-sky-300 text-sm font-medium mb-1">
                Logo URL
              </label>
              <input
                id="tenant-dashboard-logo-url"
                type="url"
                value={form.logoUrl}
                onChange={(e) => setForm((f) => ({ ...f, logoUrl: e.target.value }))}
                className="w-full bg-navy-700 border border-navy-600 rounded-lg px-3 py-2 text-sky-100 placeholder-sky-600 focus:border-gold-500 text-sm"
                placeholder="https://example.com/logo.png"
              />
              {form.logoUrl && (
                <div className="mt-2">
                  <img
                    src={form.logoUrl}
                    alt="Logo preview"
                    className="h-12 w-12 rounded-lg object-contain bg-navy-700 border border-navy-600"
                    onError={(e) => { e.target.style.display = 'none'; }}
                  />
                </div>
              )}
            </div>
            <div className="flex gap-3 pt-2">
              <button
                onClick={handleSave}
                disabled={saving}
                className="px-4 py-2 bg-gold-500 text-navy-900 rounded-lg font-semibold hover:bg-gold-400 transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {saving ? 'Saving...' : 'Save Changes'}
              </button>
              <button
                onClick={handleCancel}
                disabled={saving}
                className="px-4 py-2 bg-navy-700 text-sky-300 rounded-lg font-medium hover:bg-navy-600 transition-colors text-sm disabled:opacity-50"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className="flex items-start gap-5">
            {tenant?.logoUrl ? (
              <img
                src={tenant.logoUrl}
                alt={tenant.displayName}
                className="h-16 w-16 rounded-xl object-contain bg-navy-700 border border-navy-600 flex-shrink-0"
                onError={(e) => { e.target.style.display = 'none'; }}
              />
            ) : (
              <div className="h-16 w-16 rounded-xl bg-navy-700 border border-navy-600 flex items-center justify-center flex-shrink-0">
                <span className="text-gold-400 text-2xl font-bold">
                  {(tenant?.displayName || tenantSlug || '?')[0].toUpperCase()}
                </span>
              </div>
            )}
            <div className="flex-1 min-w-0">
              <h2 className="text-sky-100 text-xl font-bold truncate">
                {tenant?.displayName || tenantSlug}
              </h2>
              <p className="text-sky-400 text-sm mt-1 font-mono">{tenantSlug}</p>
              {tenant?.description && (
                <p className="text-sky-300 text-sm mt-2">{tenant.description}</p>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-4">
        <div className="bg-navy-800 border border-navy-700 rounded-xl p-5">
          <p className="text-sky-400 text-xs font-semibold uppercase tracking-wider mb-1">
            Communities
          </p>
          <p className="text-gold-400 text-3xl font-bold">{stats.communityCount}</p>
        </div>
        <div className="bg-navy-800 border border-navy-700 rounded-xl p-5">
          <p className="text-sky-400 text-xs font-semibold uppercase tracking-wider mb-1">
            Admins
          </p>
          <p className="text-gold-400 text-3xl font-bold">{stats.adminCount}</p>
        </div>
      </div>

      {/* Quick Links */}
      <div>
        <h2 className="text-sky-100 text-lg font-semibold mb-3">Quick Links</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          {quickLinks.map((link) => (
            <Link
              key={link.to}
              to={link.to}
              className="bg-navy-800 border border-navy-700 rounded-xl p-5 hover:border-gold-500 hover:bg-navy-750 transition-colors group"
            >
              <div className="flex items-center gap-3 mb-2">
                <span className="text-gold-400 group-hover:text-gold-300 transition-colors">
                  {link.icon}
                </span>
                <span className="text-sky-100 font-semibold">{link.label}</span>
              </div>
              <p className="text-sky-400 text-sm">{link.description}</p>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}

export default TenantDashboard;
