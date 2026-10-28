import { useState } from 'react';
import { Edit, Trash2, Lock } from 'lucide-react';

export default function CredentialTable({
  credentials,
  onEdit,
  onDelete,
  integrationType,
  showCommunityId,
  showUserId,
}) {
  const [sortBy, setSortBy] = useState('platform');

  const sorted = [...credentials].sort((a, b) => {
    if (sortBy === 'platform') return (a.platform || '').localeCompare(b.platform || '');
    if (sortBy === 'updated') return new Date(b.updatedAt || 0) - new Date(a.updatedAt || 0);
    return 0;
  });

  const getExpiryStatus = (expiresAt) => {
    if (!expiresAt) return 'none';
    const now = new Date();
    const expiry = new Date(expiresAt);
    const daysLeft = Math.ceil((expiry - now) / (1000 * 60 * 60 * 24));
    if (daysLeft < 0) return 'expired';
    if (daysLeft < 7) return 'expiring';
    return 'valid';
  };

  const getExpiryText = (expiresAt) => {
    if (!expiresAt) return 'No expiry';
    const status = getExpiryStatus(expiresAt);
    const expiry = new Date(expiresAt);
    const daysLeft = Math.ceil((expiry - new Date()) / (1000 * 60 * 60 * 24));

    if (status === 'expired') return 'Expired';
    if (status === 'expiring') return `${daysLeft}d left`;
    return expiry.toLocaleDateString();
  };

  return (
    <div className="card overflow-hidden">
      <div className="p-4 border-b border-navy-700 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <label htmlFor="credential-table-sort-by" className="text-sm font-medium text-sky-100">Sort by:</label>
          <select
            id="credential-table-sort-by"
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="px-3 py-1 bg-navy-800 border border-navy-600 rounded text-sm text-sky-100 focus:ring-2 focus:ring-gold-500"
          >
            <option value="platform">Platform</option>
            <option value="updated">Last Updated</option>
          </select>
        </div>
        <span className="text-sm text-navy-400">{sorted.length} credential(s)</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-navy-700 bg-navy-800/50">
              <th className="px-6 py-3 text-left font-semibold text-sky-100">Platform</th>
              {showCommunityId && (
                <th className="px-6 py-3 text-left font-semibold text-sky-100">Community</th>
              )}
              {showUserId && (
                <th className="px-6 py-3 text-left font-semibold text-sky-100">User</th>
              )}
              <th className="px-6 py-3 text-left font-semibold text-sky-100">Status</th>
              <th className="px-6 py-3 text-left font-semibold text-sky-100">Expires</th>
              <th className="px-6 py-3 text-left font-semibold text-sky-100">Updated</th>
              <th className="px-6 py-3 text-left font-semibold text-sky-100">Actions</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((cred) => {
              const expiryStatus = getExpiryStatus(cred.expiresAt);
              const statusClass = {
                valid: 'text-emerald-400',
                expiring: 'text-yellow-400',
                expired: 'text-red-400',
                none: 'text-sky-400',
              }[expiryStatus];

              return (
                <tr
                  key={cred.id}
                  className="border-b border-navy-700 hover:bg-navy-800/30 transition-colors"
                >
                  <td className="px-6 py-4">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-sky-100 uppercase">
                        {cred.platform}
                      </span>
                      {cred.isEncrypted && (
                        <Lock className="w-3 h-3 text-gold-400" title="Encrypted" />
                      )}
                    </div>
                  </td>
                  {showCommunityId && (
                    <td className="px-6 py-4 text-navy-400">
                      {cred.communityId || '-'}
                    </td>
                  )}
                  {showUserId && (
                    <td className="px-6 py-4 text-navy-400">
                      {cred.userId || '-'}
                    </td>
                  )}
                  <td className="px-6 py-4">
                    {cred.isActive ? (
                      <span className="inline-flex items-center gap-1 text-emerald-400">
                        <span className="w-2 h-2 bg-emerald-400 rounded-full"></span>
                        Active
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-navy-400">
                        <span className="w-2 h-2 bg-navy-400 rounded-full"></span>
                        Inactive
                      </span>
                    )}
                  </td>
                  <td className={`px-6 py-4 font-medium ${statusClass}`}>
                    {getExpiryText(cred.expiresAt)}
                  </td>
                  <td className="px-6 py-4 text-navy-400">
                    {cred.updatedAt
                      ? new Date(cred.updatedAt).toLocaleDateString()
                      : '-'}
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => onEdit(cred)}
                        className="p-2 text-navy-400 hover:text-gold-400 hover:bg-navy-700/50 rounded transition-colors"
                        title="Edit"
                      >
                        <Edit className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => onDelete(cred.id)}
                        className="p-2 text-navy-400 hover:text-red-400 hover:bg-navy-700/50 rounded transition-colors"
                        title="Delete"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {sorted.length === 0 && (
        <div className="p-12 text-center text-navy-400">
          No credentials found
        </div>
      )}
    </div>
  );
}
