import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import {
  GlobeAltIcon,
  PlusIcon,
  TrashIcon,
  CheckCircleIcon,
  ClockIcon,
  ClipboardDocumentIcon,
  ArrowPathIcon,
  XMarkIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

function AdminDomains() {
  const { communityId } = useParams();
  const [domains, setDomains] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);

  // Add domain form
  const [newDomain, setNewDomain] = useState('');
  const [adding, setAdding] = useState(false);

  // Action states
  const [verifying, setVerifying] = useState({});
  const [deleting, setDeleting] = useState({});
  const [copied, setCopied] = useState(null);

  // Delete confirmation
  const [deleteConfirm, setDeleteConfirm] = useState(null);

  useEffect(() => {
    loadDomains();
  }, [communityId]);

  const loadDomains = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await adminApi.getDomains(communityId);
      setDomains(response.data.domains || []);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load domains');
    } finally {
      setLoading(false);
    }
  };

  const handleAddDomain = async (e) => {
    e.preventDefault();
    if (!newDomain.trim()) return;

    try {
      setAdding(true);
      setError(null);
      await adminApi.addDomain(communityId, newDomain.trim().toLowerCase());
      setMessage({ type: 'success', text: 'Domain added successfully. Please add the DNS record to verify.' });
      setNewDomain('');
      loadDomains();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to add domain');
    } finally {
      setAdding(false);
    }
  };

  const handleVerify = async (domainId) => {
    try {
      setVerifying({ ...verifying, [domainId]: true });
      setError(null);
      const response = await adminApi.verifyDomain(communityId, domainId);
      if (response.data.success) {
        setMessage({ type: 'success', text: 'Domain verified successfully!' });
        loadDomains();
      } else {
        setError(response.data.message || 'Verification failed. Please check your DNS records.');
      }
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to verify domain');
    } finally {
      setVerifying({ ...verifying, [domainId]: false });
    }
  };

  const handleDelete = async (domainId) => {
    try {
      setDeleting({ ...deleting, [domainId]: true });
      setError(null);
      await adminApi.removeDomain(communityId, domainId);
      setMessage({ type: 'success', text: 'Domain removed successfully' });
      setDeleteConfirm(null);
      loadDomains();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to remove domain');
    } finally {
      setDeleting({ ...deleting, [domainId]: false });
    }
  };

  const copyToClipboard = (text, id) => {
    navigator.clipboard.writeText(text);
    setCopied(id);
    setTimeout(() => setCopied(null), 2000);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-sky-100">Custom Domains</h1>
        <p className="text-navy-400 mt-1">
          Add custom domains to access your community pages
        </p>
      </div>

      {/* Messages */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-400" />
            <span className="text-red-400">{error}</span>
          </div>
          <button aria-label="Close" onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {message && (
        <div className={`rounded-lg p-4 flex items-center justify-between ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border border-red-500/30'
        }`}>
          <div className="flex items-center space-x-3">
            <CheckCircleIcon className="w-5 h-5" />
            <span>{message.text}</span>
          </div>
          <button aria-label="Close" onClick={() => setMessage(null)} className="hover:opacity-75">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {/* Add Domain Form */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <h2 className="text-lg font-semibold text-sky-100 mb-4">Add Custom Domain</h2>
        <form onSubmit={handleAddDomain} className="flex gap-4">
          <div className="flex-1">
            <input
              aria-label="example.com"
              type="text"
              value={newDomain}
              onChange={(e) => setNewDomain(e.target.value)}
              placeholder="example.com"
              className="w-full bg-navy-900 border border-navy-700 rounded-lg px-4 py-2 text-sky-100 focus:border-gold-500"
            />
          </div>
          <button
            type="submit"
            disabled={adding || !newDomain.trim()}
            className="inline-flex items-center space-x-2 px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg disabled:opacity-50 transition-colors"
          >
            <PlusIcon className="w-5 h-5" />
            <span>{adding ? 'Adding...' : 'Add Domain'}</span>
          </button>
        </form>
      </div>

      {/* Domain List */}
      {domains.length === 0 ? (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <GlobeAltIcon className="w-12 h-12 text-navy-500 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-sky-100 mb-2">No Custom Domains</h3>
          <p className="text-navy-400">
            Add a custom domain above to get started.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {domains.map((domain) => (
            <div key={domain.id} className="bg-navy-800 border border-navy-700 rounded-lg p-6">
              <div className="flex items-start justify-between">
                <div className="flex items-center space-x-3">
                  <GlobeAltIcon className="w-6 h-6 text-gold-400" />
                  <div>
                    <h3 className="font-semibold text-sky-100">{domain.domain}</h3>
                    <div className="flex items-center space-x-2 mt-1">
                      {domain.isVerified ? (
                        <span className="inline-flex items-center space-x-1 px-2 py-0.5 rounded-full text-xs font-medium bg-green-500/20 text-green-400 border border-green-500/30">
                          <CheckCircleIcon className="w-3 h-3" />
                          <span>Verified</span>
                        </span>
                      ) : (
                        <span className="inline-flex items-center space-x-1 px-2 py-0.5 rounded-full text-xs font-medium bg-yellow-500/20 text-yellow-400 border border-yellow-500/30">
                          <ClockIcon className="w-3 h-3" />
                          <span>Pending Verification</span>
                        </span>
                      )}
                      {domain.verifiedAt && (
                        <span className="text-xs text-navy-400">
                          Verified {new Date(domain.verifiedAt).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="flex items-center space-x-2">
                  {!domain.isVerified && (
                    <button
                      onClick={() => handleVerify(domain.id)}
                      disabled={verifying[domain.id]}
                      className="inline-flex items-center space-x-2 px-3 py-2 bg-sky-600 hover:bg-sky-700 text-white rounded-lg transition-colors disabled:opacity-50"
                    >
                      <ArrowPathIcon className={`w-4 h-4 ${verifying[domain.id] ? 'animate-spin' : ''}`} />
                      <span>{verifying[domain.id] ? 'Verifying...' : 'Verify'}</span>
                    </button>
                  )}
                  <button
                    aria-label={`Delete domain ${domain.domain}`}
                    onClick={() => setDeleteConfirm(domain)}
                    className="p-2 text-red-400 hover:bg-red-500/20 rounded-lg transition-colors"
                  >
                    <TrashIcon className="w-5 h-5" />
                  </button>
                </div>
              </div>

              {/* DNS Instructions for unverified domains */}
              {!domain.isVerified && domain.verificationToken && (
                <div className="mt-4 p-4 bg-navy-900 rounded-lg">
                  <h4 className="text-sm font-medium text-sky-100 mb-3">DNS Verification Required</h4>
                  <p className="text-sm text-navy-400 mb-3">
                    Add the following TXT record to your DNS settings:
                  </p>
                  <div className="space-y-2">
                    <div className="flex items-center justify-between bg-navy-800 rounded p-3">
                      <div>
                        <span className="text-xs text-navy-400">Record Type:</span>
                        <p className="font-mono text-sm text-gold-400">TXT</p>
                      </div>
                    </div>
                    <div className="flex items-center justify-between bg-navy-800 rounded p-3">
                      <div className="flex-1 mr-4">
                        <span className="text-xs text-navy-400">Name/Host:</span>
                        <p className="font-mono text-sm text-gold-400 break-all">_waddlebot.{domain.domain}</p>
                      </div>
                      <button
                        aria-label={`Copy DNS record name for ${domain.domain}`}
                        onClick={() => copyToClipboard(`_waddlebot.${domain.domain}`, `name-${domain.id}`)}
                        className="p-2 text-navy-400 hover:text-sky-300 rounded"
                      >
                        {copied === `name-${domain.id}` ? (
                          <CheckCircleIcon className="w-5 h-5 text-green-400" />
                        ) : (
                          <ClipboardDocumentIcon className="w-5 h-5" />
                        )}
                      </button>
                    </div>
                    <div className="flex items-center justify-between bg-navy-800 rounded p-3">
                      <div className="flex-1 mr-4">
                        <span className="text-xs text-navy-400">Value:</span>
                        <p className="font-mono text-sm text-gold-400 break-all">{domain.verificationToken}</p>
                      </div>
                      <button
                        aria-label={`Copy verification value for ${domain.domain}`}
                        onClick={() => copyToClipboard(domain.verificationToken, `token-${domain.id}`)}
                        className="p-2 text-navy-400 hover:text-sky-300 rounded"
                      >
                        {copied === `token-${domain.id}` ? (
                          <CheckCircleIcon className="w-5 h-5 text-green-400" />
                        ) : (
                          <ClipboardDocumentIcon className="w-5 h-5" />
                        )}
                      </button>
                    </div>
                  </div>
                  <p className="text-xs text-navy-500 mt-3">
                    DNS changes can take up to 48 hours to propagate. Click "Verify" once the record is added.
                  </p>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {deleteConfirm && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div role="dialog" aria-modal="true" aria-labelledby="admin-domains-dialog-1-title" className="bg-navy-800 border border-navy-700 rounded-lg p-6 max-w-md mx-4">
            <h3 id="admin-domains-dialog-1-title" className="text-lg font-semibold text-sky-100 mb-2">Remove Domain?</h3>
            <p className="text-navy-300 mb-4">
              Are you sure you want to remove <span className="text-gold-400 font-medium">{deleteConfirm.domain}</span>?
              This action cannot be undone.
            </p>
            <div className="flex justify-end space-x-3">
              <button
                onClick={() => setDeleteConfirm(null)}
                className="px-4 py-2 text-navy-300 hover:text-sky-100"
              >
                Cancel
              </button>
              <button
                onClick={() => handleDelete(deleteConfirm.id)}
                disabled={deleting[deleteConfirm.id]}
                className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white font-medium rounded-lg disabled:opacity-50"
              >
                {deleting[deleteConfirm.id] ? 'Removing...' : 'Remove Domain'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminDomains;
