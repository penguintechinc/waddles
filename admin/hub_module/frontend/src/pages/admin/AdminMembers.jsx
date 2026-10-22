import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { KeyIcon, ClipboardDocumentIcon } from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

function AdminMembers() {
  const { communityId } = useParams();
  const [members, setMembers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [pagination, setPagination] = useState(null);
  const [search, setSearch] = useState('');

  // Reset password modal state
  const [resetModal, setResetModal] = useState(null); // { member }
  const [tempPassword, setTempPassword] = useState('');
  const [resetLoading, setResetLoading] = useState(false);
  const [resetError, setResetError] = useState('');
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    async function fetchMembers() {
      setLoading(true);
      try {
        const response = await adminApi.getMembers(communityId, { page, limit: 25, search });
        setMembers(response.data.members);
        setPagination(response.data.pagination);
      } catch (err) {
        console.error('Failed to fetch members:', err);
      } finally {
        setLoading(false);
      }
    }
    fetchMembers();
  }, [communityId, page, search]);

  const roleColor = (role) => {
    switch (role) {
      case 'community-owner': return 'badge-gold';
      case 'community-admin': return 'bg-purple-500/20 text-purple-300 border border-purple-500/30';
      case 'moderator': return 'badge-sky';
      case 'vip': return 'bg-amber-500/20 text-amber-300 border border-amber-500/30';
      default: return 'bg-navy-700 text-navy-300 border border-navy-600';
    }
  };

  const handleResetPassword = async (member) => {
    setResetModal({ member });
    setTempPassword('');
    setResetError('');
    setCopied(false);
    setResetLoading(true);
    try {
      const res = await adminApi.generateTempPassword(communityId, { userId: member.id });
      setTempPassword(res.data?.temp_password || res.data?.password || '');
    } catch (err) {
      setResetError(err?.response?.data?.error || 'Failed to generate temporary password.');
    } finally {
      setResetLoading(false);
    }
  };

  const handleCopyPassword = () => {
    navigator.clipboard.writeText(tempPassword);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-sky-100">Members</h1>
        <input
          aria-label="Search members"
          type="search"
          placeholder="Search members..."
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
          className="input w-64"
        />
      </div>

      <div className="card overflow-hidden">
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>Role</th>
              <th>Rep</th>
              <th>Joined</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan="4" className="p-12 text-center">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400 mx-auto"></div>
                </td>
              </tr>
            ) : members.length === 0 ? (
              <tr>
                <td colSpan="4" className="p-12 text-center text-navy-400">
                  No members found
                </td>
              </tr>
            ) : (
              members.map((member) => (
                <tr key={member.id}>
                  <td>
                    <div className="flex items-center space-x-3">
                      {member.avatarUrl ? (
                        <img src={member.avatarUrl} alt="" className="w-8 h-8 rounded-full" />
                      ) : (
                        <div className="w-8 h-8 rounded-full bg-navy-700 flex items-center justify-center text-sm">
                          {member.username?.[0]?.toUpperCase() || '?'}
                        </div>
                      )}
                      <div>
                        <div className="font-medium text-sky-100">{member.username || 'Unknown'}</div>
                        <div className="text-xs text-navy-500">{member.email}</div>
                      </div>
                    </div>
                  </td>
                  <td>
                    <span className={`badge ${roleColor(member.role)}`}>
                      {member.role.replace('community-', '')}
                    </span>
                  </td>
                  <td>
                    <div className="text-gold-400 font-medium">{member.reputation?.score || 600}</div>
                    <div className="text-xs text-navy-500 capitalize">{member.reputation?.label || 'Fair'}</div>
                  </td>
                  <td className="text-sm text-navy-400">
                    {new Date(member.joinedAt).toLocaleDateString()}
                  </td>
                  <td>
                    <button
                      onClick={() => handleResetPassword(member)}
                      title="Reset Password"
                      className="flex items-center gap-1.5 px-2.5 py-1.5 bg-navy-800 border border-navy-600 text-sky-200 rounded-lg hover:bg-navy-700 text-xs transition-colors"
                    >
                      <KeyIcon className="w-3.5 h-3.5" />
                      Reset Password
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>

        {pagination && pagination.totalPages > 1 && (
          <div className="flex justify-between items-center p-4 border-t border-navy-700">
            <span className="text-sm text-navy-400">
              {pagination.total} total members
            </span>
            <div className="flex space-x-2">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Previous
              </button>
              <button
                onClick={() => setPage(p => Math.min(pagination.totalPages, p + 1))}
                disabled={page === pagination.totalPages}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
      {/* Reset Password Modal */}
      {resetModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div role="dialog" aria-modal="true" aria-labelledby="admin-members-dialog-1-title" className="bg-navy-900 border border-navy-700 rounded-xl p-6 w-full max-w-md mx-4">
            <h2 id="admin-members-dialog-1-title" className="text-lg font-semibold text-sky-100 mb-1">Reset Password</h2>
            <p className="text-sm text-navy-400 mb-4">
              Generating a temporary password for <span className="text-sky-200">{resetModal.member.username}</span>.
              Share this with them to allow a one-time login.
            </p>

            {resetLoading && (
              <div className="flex items-center justify-center py-6">
                <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-gold-400" />
              </div>
            )}

            {tempPassword && !resetLoading && (
              <div className="space-y-3">
                <div className="flex items-center gap-2 bg-navy-800 border border-navy-600 rounded-lg px-3 py-2">
                  <span className="font-mono text-gold-400 flex-1 text-sm break-all">{tempPassword}</span>
                  <button
                    onClick={handleCopyPassword}
                    className="shrink-0 p-1.5 text-sky-400 hover:text-sky-200 transition-colors"
                    title="Copy to clipboard"
                  >
                    <ClipboardDocumentIcon className="w-4 h-4" />
                  </button>
                </div>
                {copied && <p className="text-green-400 text-xs">Copied to clipboard!</p>}
                <p className="text-xs text-navy-500">
                  This password is valid for a single login. The user should change it immediately.
                </p>
              </div>
            )}

            {resetError && <p className="text-red-400 text-sm mt-2">{resetError}</p>}

            <div className="flex justify-end mt-6">
              <button
                onClick={() => setResetModal(null)}
                className="px-4 py-2 bg-navy-800 border border-navy-600 text-sky-200 rounded-lg hover:bg-navy-700 transition-colors text-sm"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminMembers;
