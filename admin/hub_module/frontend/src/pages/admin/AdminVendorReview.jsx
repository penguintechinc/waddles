import React, { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import './AdminVendorReview.css';

const RISK_COLORS = {
  low: '#4caf50',
  medium: '#ff9800',
  high: '#f44336',
  critical: '#e91e63',
};

export default function AdminVendorReview() {
  const { submissionId } = useParams();
  const navigate = useNavigate();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [submission, setSubmission] = useState(null);
  const [action, setAction] = useState(null); // 'approve', 'reject', 'request_info'
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [publishImmediate, setPublishImmediate] = useState(false);

  useEffect(() => {
    fetchSubmission();
  }, [submissionId]);

  const fetchSubmission = async () => {
    try {
      setLoading(true);
      // SECURITY (security.md C4): the session JWT lives only in the
      // HttpOnly `wb_session` cookie now, never localStorage -- `fetch()`'s
      // default `credentials: 'same-origin'` already sends it on this
      // same-origin request, nothing to attach by hand.
      const response = await fetch(`/api/v1/admin/vendor/submissions/${submissionId}`);

      if (!response.ok) {
        throw new Error('Failed to fetch submission');
      }

      const data = await response.json();
      setSubmission(data.submission);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleAction = async (actionType) => {
    setAction(actionType);
  };

  const handleSubmitAction = async () => {
    if (!message.trim()) {
      alert('Please provide a message');
      return;
    }

    setSubmitting(true);
    try {
      let endpoint, body;

      if (action === 'approve') {
        endpoint = `/api/v1/admin/vendor/submissions/${submissionId}/approve`;
        body = {
          adminNotes: message,
          publishImmediately: publishImmediate,
        };
      } else if (action === 'reject') {
        endpoint = `/api/v1/admin/vendor/submissions/${submissionId}/reject`;
        body = {
          rejectionReason: message,
        };
      } else if (action === 'request_info') {
        endpoint = `/api/v1/admin/vendor/submissions/${submissionId}/request-info`;
        body = {
          message,
        };
      }

      const response = await fetch(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      });

      if (!response.ok) {
        throw new Error('Action failed');
      }

      setAction(null);
      setMessage('');
      setPublishImmediate(false);
      await fetchSubmission();
      alert('Action completed successfully');
    } catch (err) {
      alert(`Error: ${err.message}`);
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <div className="admin-vendor-review loading">Loading submission...</div>;
  }

  if (error) {
    return (
      <div className="admin-vendor-review error">
        <div className="error-message">
          <h2>Error</h2>
          <p>{error}</p>
          <button onClick={() => navigate('/admin/vendor-submissions')} className="btn btn-primary">
            Back to Submissions
          </button>
        </div>
      </div>
    );
  }

  if (!submission) {
    return <div className="admin-vendor-review">Submission not found</div>;
  }

  const riskLevel = (scopes) => {
    if (!scopes || scopes.length === 0) return 'low';
    const risks = scopes.map(s => s.risk_level);
    if (risks.includes('critical')) return 'critical';
    if (risks.includes('high')) return 'high';
    if (risks.includes('medium')) return 'medium';
    return 'low';
  };

  return (
    <div className="admin-vendor-review">
      <div className="review-header">
        <h1>Vendor Module Submission Review</h1>
        <div className="status-badge" data-status={submission.status}>
          {submission.status.toUpperCase()}
        </div>
      </div>

      <div className="review-container">
        {/* Vendor Information */}
        <section className="review-section">
          <h2>Vendor Information</h2>
          <div className="info-grid">
            <div className="info-item">
              <label>Vendor Name</label>
              <p>{submission.vendor_name}</p>
            </div>
            <div className="info-item">
              <label>Email</label>
              <p>{submission.vendor_email}</p>
            </div>
            <div className="info-item">
              <label>Company</label>
              <p>{submission.company_name || 'N/A'}</p>
            </div>
            <div className="info-item">
              <label>Phone</label>
              <p>{submission.contact_phone || 'N/A'}</p>
            </div>
            {submission.website_url && (
              <div className="info-item">
                <label>Website</label>
                <p><a href={submission.website_url} target="_blank" rel="noopener noreferrer">
                  {submission.website_url}
                </a></p>
              </div>
            )}
          </div>
        </section>

        {/* Module Information */}
        <section className="review-section">
          <h2>Module Information</h2>
          <div className="info-grid">
            <div className="info-item full-width">
              <label>Module Name</label>
              <h3>{submission.module_name}</h3>
            </div>
            <div className="info-item full-width">
              <label>Description</label>
              <p className="description">{submission.module_description}</p>
            </div>
            <div className="info-item">
              <label>Category</label>
              <p>{submission.module_category}</p>
            </div>
            <div className="info-item">
              <label>Version</label>
              <p>{submission.module_version}</p>
            </div>
            {submission.repository_url && (
              <div className="info-item">
                <label>Repository</label>
                <p><a href={submission.repository_url} target="_blank" rel="noopener noreferrer">
                  {submission.repository_url}
                </a></p>
              </div>
            )}
          </div>
        </section>

        {/* Permissions/Scopes */}
        <section className="review-section">
          <h2>Requested Permissions</h2>
          <div className="risk-overview">
            <div className={`risk-badge risk-${riskLevel(submission.scopes)}`}>
              Maximum Risk: {riskLevel(submission.scopes).toUpperCase()}
            </div>
          </div>

          <div className="scopes-list">
            {submission.scopes && submission.scopes.length > 0 ? (
              submission.scopes.map((scope) => (
                <div key={scope.id} className="scope-item" style={{borderLeftColor: RISK_COLORS[scope.risk_level]}}>
                  <div className="scope-header">
                    <h4>{scope.scope_name}</h4>
                    <span className={`risk-badge risk-${scope.risk_level}`}>
                      {scope.risk_level.toUpperCase()}
                    </span>
                  </div>
                  <p className="scope-description">{scope.description}</p>
                </div>
              ))
            ) : (
              <p>No scopes requested</p>
            )}
          </div>

          {submission.scope_justification && (
            <div className="justification-box">
              <label>Scope Justification</label>
              <p>{submission.scope_justification}</p>
            </div>
          )}
        </section>

        {/* Webhook Configuration */}
        <section className="review-section">
          <h2>Webhook Configuration</h2>
          <div className="info-grid">
            <div className="info-item full-width">
              <label>Webhook URL</label>
              <code>{submission.webhook_url}</code>
            </div>
            <div className="info-item">
              <label>Per-Community Webhooks</label>
              <p>{submission.webhook_per_community ? '✓ Yes' : '✗ No'}</p>
            </div>
            <div className="info-item">
              <label>Webhook Secret</label>
              <p>{submission.webhook_secret ? '✓ Provided' : '✗ Not provided'}</p>
            </div>
          </div>
        </section>

        {/* Pricing & Payment */}
        <section className="review-section">
          <h2>Pricing & Payment Information</h2>
          <div className="pricing-box">
            <div className="pricing-item">
              <label>Pricing Model</label>
              <p>{submission.pricing_model === 'flat-rate' ? 'Flat Rate' : 'Per Seat'}</p>
            </div>
            <div className="pricing-item">
              <label>Price</label>
              <p>${submission.pricing_amount} {submission.pricing_currency}</p>
            </div>
          </div>

          <div className="payment-info">
            <h4>Payment Method: {submission.payment_method.toUpperCase()}</h4>
            <div className="payment-details">
              {JSON.stringify(submission.payment_details, null, 2)}
            </div>
          </div>

          <div className="processor-fee-note">
            <strong>Processing Fees Notice:</strong>
            <p>{submission.processor_fee_disclaimer}</p>
          </div>
        </section>

        {/* Support Information */}
        <section className="review-section">
          <h2>Support & Documentation</h2>
          <div className="info-grid">
            {submission.documentation_url && (
              <div className="info-item">
                <label>Documentation</label>
                <p><a href={submission.documentation_url} target="_blank" rel="noopener noreferrer">
                  {submission.documentation_url}
                </a></p>
              </div>
            )}
            {submission.support_email && (
              <div className="info-item">
                <label>Support Email</label>
                <p><a href={`mailto:${submission.support_email}`}>{submission.support_email}</a></p>
              </div>
            )}
            {submission.support_contact_url && (
              <div className="info-item">
                <label>Support Portal</label>
                <p><a href={submission.support_contact_url} target="_blank" rel="noopener noreferrer">
                  {submission.support_contact_url}
                </a></p>
              </div>
            )}
          </div>
        </section>

        {/* Review History */}
        <section className="review-section">
          <h2>Review History</h2>
          <div className="timeline">
            {submission.reviews && submission.reviews.length > 0 ? (
              submission.reviews.map((review, idx) => (
                <div key={idx} className="timeline-item">
                  <div className="timeline-date">
                    {new Date(review.created_at).toLocaleString()}
                  </div>
                  <div className="timeline-content">
                    <div className="timeline-action">{review.action.toUpperCase()}</div>
                    {review.reviewer_name && (
                      <div className="timeline-reviewer">by {review.reviewer_name}</div>
                    )}
                    {review.comments && (
                      <p className="timeline-message">{review.comments}</p>
                    )}
                  </div>
                </div>
              ))
            ) : (
              <p className="no-reviews">No reviews yet</p>
            )}
          </div>
        </section>

        {/* Admin Actions */}
        {submission.status !== 'approved' && submission.status !== 'rejected' && (
          <section className="review-section actions-section">
            <h2>Admin Actions</h2>

            {!action ? (
              <div className="action-buttons">
                <button
                  onClick={() => handleAction('approve')}
                  className="btn btn-success"
                >
                  ✓ Approve Submission
                </button>
                <button
                  onClick={() => handleAction('request_info')}
                  className="btn btn-info"
                >
                  ⓘ Request More Info
                </button>
                <button
                  onClick={() => handleAction('reject')}
                  className="btn btn-danger"
                >
                  ✗ Reject Submission
                </button>
              </div>
            ) : (
              <div className="action-form">
                <h3>
                  {action === 'approve' && 'Approve Submission'}
                  {action === 'reject' && 'Reject Submission'}
                  {action === 'request_info' && 'Request More Information'}
                </h3>

                {action === 'approve' && (
                  <div className="form-group">
                    <label>
                      <input
                        type="checkbox"
                        checked={publishImmediate}
                        onChange={(e) => setPublishImmediate(e.target.checked)}
                      />
                      Publish to marketplace immediately after approval
                    </label>
                  </div>
                )}

                <div className="form-group">
                  <label htmlFor="admin-vendor-review-message-notes">Message/Notes</label>
                  <textarea
                    id="admin-vendor-review-message-notes"
                    value={message}
                    onChange={(e) => setMessage(e.target.value)}
                    rows="4"
                    placeholder={
                      action === 'approve'
                        ? 'Optional approval notes for your records...'
                        : action === 'reject'
                        ? 'Please explain why this submission is being rejected...'
                        : 'Specify what additional information you need...'
                    }
                  />
                </div>

                <div className="action-buttons">
                  <button
                    onClick={handleSubmitAction}
                    disabled={submitting}
                    className={`btn btn-${action === 'reject' ? 'danger' : action === 'approve' ? 'success' : 'info'}`}
                  >
                    {submitting ? 'Processing...' : 'Submit'}
                  </button>
                  <button
                    onClick={() => {
                      setAction(null);
                      setMessage('');
                      setPublishImmediate(false);
                    }}
                    className="btn btn-secondary"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </section>
        )}
      </div>

      <div className="review-footer">
        <button
          onClick={() => navigate('/admin/vendor-submissions')}
          className="btn btn-secondary"
        >
          ← Back to Submissions
        </button>
      </div>
    </div>
  );
}
