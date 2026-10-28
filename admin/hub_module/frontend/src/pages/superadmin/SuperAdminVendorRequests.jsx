/**
 * Super Admin Vendor Requests
 * Allows super admins to review and approve/reject vendor role requests
 */
import { useEffect, useState, useMemo } from 'react';
import {
  CheckCircleIcon,
  XCircleIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_GOLD_COLORS } from '../../theme/waddlebotTheme';

function SuperAdminVendorRequests() {
  const [requests, setRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedRequest, setSelectedRequest] = useState(null);
  const [showApproveModal, setShowApproveModal] = useState(false);
  const [showRejectModal, setShowRejectModal] = useState(false);
  const [filters, setFilters] = useState({
    status: 'pending',
  });

  useEffect(() => {
    loadRequests();
  }, [filters]);

  const loadRequests = async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams();
      params.append('status', filters.status);

      const response = await api.get(`/admin/vendor/requests?${params.toString()}`);
      setRequests(response.data?.requests || []);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load requests');
      setRequests([]);
    } finally {
      setLoading(false);
    }
  };

  const openApproveModal = (request) => {
    setSelectedRequest(request);
    setShowApproveModal(true);
  };

  const openRejectModal = (request) => {
    setSelectedRequest(request);
    setShowRejectModal(true);
  };

  const handleApprove = async (formData) => {
    if (!selectedRequest) return;

    try {
      await api.post(`/admin/vendor/requests/${selectedRequest.request_id}/approve`, {
        adminNotes: formData.adminNotes || '',
      });

      setError(null);
      await loadRequests();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to approve request');
      throw err; // Re-throw to prevent modal from closing
    }
  };

  const handleReject = async (formData) => {
    if (!selectedRequest) return;

    if (!formData.rejectionReason) {
      setError('Rejection reason is required');
      throw new Error('Rejection reason is required');
    }

    try {
      await api.post(`/admin/vendor/requests/${selectedRequest.request_id}/reject`, {
        rejectionReason: formData.rejectionReason,
        adminNotes: formData.adminNotes || '',
      });

      setError(null);
      await loadRequests();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to reject request');
      throw err; // Re-throw to prevent modal from closing
    }
  };

  // Form field definitions for approve modal
  const approveFields = useMemo(() => [
    {
      name: 'adminNotes',
      type: 'textarea',
      label: 'Admin Notes (Optional)',
      placeholder: 'Internal notes about this approval...',
      required: false,
    },
  ], []);

  // Form field definitions for reject modal
  const rejectFields = useMemo(() => [
    {
      name: 'rejectionReason',
      type: 'textarea',
      label: 'Rejection Reason',
      placeholder: 'Explain why you are rejecting this request...',
      required: true,
    },
    {
      name: 'adminNotes',
      type: 'textarea',
      label: 'Admin Notes (Optional)',
      placeholder: 'Internal notes about this decision...',
      required: false,
    },
  ], []);

  const getStatusIcon = (status) => {
    switch (status) {
      case 'approved':
        return <CheckCircleIcon className="w-5 h-5 text-emerald-400" />;
      case 'rejected':
        return <XCircleIcon className="w-5 h-5 text-red-400" />;
      case 'pending':
      default:
        return <ClockIcon className="w-5 h-5 text-orange-400" />;
    }
  };

  const getStatusColor = (status) => {
    switch (status) {
      case 'approved':
        return 'text-emerald-400 bg-emerald-500/10';
      case 'rejected':
        return 'text-red-400 bg-red-500/10';
      case 'pending':
      default:
        return 'text-orange-400 bg-orange-500/10';
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
      <div>
        <h1 className="text-3xl font-bold text-white">Vendor Role Requests</h1>
        <p className="text-navy-300 mt-1">Review and approve/reject vendor status requests from users</p>
      </div>

      {/* Filters */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-4">
        <label htmlFor="super-admin-vendor-requests-filter-by-status" className="block text-sm font-medium text-navy-300 mb-2">Filter by Status</label>
        <select
          id="super-admin-vendor-requests-filter-by-status"
          value={filters.status}
          onChange={(e) => setFilters({ ...filters, status: e.target.value })}
          className="w-full md:w-64 bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:border-gold-400"
        >
          <option value="pending">Pending</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
          <option value="all">All</option>
        </select>
      </div>

      {/* Error Message */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg">
          {error}
        </div>
      )}

      {/* Requests List */}
      {requests.length > 0 ? (
        <div className="space-y-4">
          {requests.map((request) => (
            <div
              key={request.id}
              className="bg-navy-800 border border-navy-700 rounded-lg p-6 hover:border-navy-600 transition-colors"
            >
              <div className="flex items-start justify-between mb-4">
                <div className="flex-1">
                  <div className="flex items-center space-x-3 mb-2">
                    <h3 className="text-xl font-bold text-white">{request.user_display_name || 'Unknown User'}</h3>
                    <div className={`flex items-center space-x-2 px-3 py-1 rounded-full text-sm font-medium ${getStatusColor(request.status)}`}>
                      {getStatusIcon(request.status)}
                      <span>{request.status.charAt(0).toUpperCase() + request.status.slice(1)}</span>
                    </div>
                  </div>
                  <p className="text-sm text-navy-400">{request.company_name}</p>
                  <p className="text-sm text-navy-400 mt-1">Email: {request.user_email}</p>
                </div>
              </div>

              <div className="space-y-3 mb-4 pt-4 border-t border-navy-700">
                <div>
                  <p className="text-xs text-navy-400 uppercase tracking-wider font-semibold mb-1">Business Description</p>
                  <p className="text-navy-300 text-sm">{request.business_description}</p>
                </div>

                {request.experience_summary && (
                  <div>
                    <p className="text-xs text-navy-400 uppercase tracking-wider font-semibold mb-1">Experience</p>
                    <p className="text-navy-300 text-sm">{request.experience_summary}</p>
                  </div>
                )}

                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div>
                    <p className="text-xs text-navy-400 uppercase tracking-wider">Contact Email</p>
                    <p className="text-white font-medium mt-1">{request.contact_email}</p>
                  </div>
                  <div>
                    <p className="text-xs text-navy-400 uppercase tracking-wider">Phone</p>
                    <p className="text-white font-medium mt-1">{request.contact_phone || 'N/A'}</p>
                  </div>
                  <div>
                    <p className="text-xs text-navy-400 uppercase tracking-wider">Submitted</p>
                    <p className="text-white font-medium mt-1">
                      {new Date(request.requested_at).toLocaleDateString()}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-navy-400 uppercase tracking-wider">Status</p>
                    <p className="text-white font-medium mt-1">{request.status}</p>
                  </div>
                </div>
              </div>

              {request.status === 'rejected' && request.rejection_reason && (
                <div className="bg-red-500/10 border border-red-500/20 text-red-300 px-3 py-2 rounded text-sm mb-4">
                  <p className="font-medium">Rejection Reason:</p>
                  <p>{request.rejection_reason}</p>
                </div>
              )}

              {request.admin_notes && (
                <div className="bg-navy-900 border border-navy-700 text-navy-300 px-3 py-2 rounded text-sm mb-4">
                  <p className="font-medium text-navy-200">Admin Notes:</p>
                  <p>{request.admin_notes}</p>
                </div>
              )}

              {/* Actions */}
              {request.status === 'pending' && (
                <div className="flex items-center space-x-3 pt-4 border-t border-navy-700">
                  <button
                    onClick={() => openApproveModal(request)}
                    className="flex items-center space-x-2 text-emerald-400 hover:text-emerald-300 transition-colors"
                  >
                    <CheckCircleIcon className="w-4 h-4" />
                    <span>Approve</span>
                  </button>
                  <button
                    onClick={() => openRejectModal(request)}
                    className="flex items-center space-x-2 text-red-400 hover:text-red-300 transition-colors ml-4 pl-4 border-l border-navy-700"
                  >
                    <XCircleIcon className="w-4 h-4" />
                    <span>Reject</span>
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <p className="text-navy-400 mb-2">
            {filters.status === 'pending' ? 'No pending vendor requests' : 'No vendor requests found'}
          </p>
        </div>
      )}

      {/* Approve Modal */}
      <FormModalBuilder
        title={`Approve Vendor Request${selectedRequest ? ` - ${selectedRequest.user_display_name}` : ''}`}
        description={selectedRequest ? `Company: ${selectedRequest.company_name}` : ''}
        fields={approveFields}
        isOpen={showApproveModal && selectedRequest !== null}
        onClose={() => setShowApproveModal(false)}
        onSubmit={handleApprove}
        submitButtonText="Approve"
        cancelButtonText="Cancel"
        themeMode="dark"
        colors={WADDLES_GOLD_COLORS}
      />

      {/* Reject Modal */}
      <FormModalBuilder
        title={`Reject Vendor Request${selectedRequest ? ` - ${selectedRequest.user_display_name}` : ''}`}
        description={selectedRequest ? `Company: ${selectedRequest.company_name}` : ''}
        fields={rejectFields}
        isOpen={showRejectModal && selectedRequest !== null}
        onClose={() => setShowRejectModal(false)}
        onSubmit={handleReject}
        submitButtonText="Reject"
        cancelButtonText="Cancel"
        themeMode="dark"
        colors={WADDLES_GOLD_COLORS}
      />
    </div>
  );
}

export default SuperAdminVendorRequests;
