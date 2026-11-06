import React, { useState, useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { vendorPublicApi } from '../../services/api';
import './VendorSubmissionStatus.css';

export default function VendorSubmissionStatus() {
  const location = useLocation();
  const [email, setEmail] = useState('');
  const [submissionId, setSubmissionId] = useState(location.state?.submissionId || '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [submission, setSubmission] = useState(null);

  useEffect(() => {
    if (location.state?.submissionId) {
      // Auto-load if coming from successful submission
      setSubmissionId(location.state.submissionId);
    }
  }, [location.state]);

  const handleCheckStatus = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    try {
      if (!email || !submissionId) {
        throw new Error('Please provide both email and submission ID');
      }

      const response = await vendorPublicApi.getSubmissionStatus(submissionId, email);
      setSubmission(response.data.submission);
    } catch (err) {
      setError(err.response?.data?.message || err.message || 'Failed to fetch submission status');
      setSubmission(null);
    } finally {
      setLoading(false);
    }
  };

  const getStatusColor = (status) => {
    switch (status) {
      case 'pending':
        return 'warning';
      case 'under-review':
        return 'info';
      case 'approved':
        return 'success';
      case 'rejected':
        return 'danger';
      case 'suspended':
        return 'secondary';
      default:
        return 'default';
    }
  };

  const getStatusMessage = (status) => {
    switch (status) {
      case 'pending':
        return 'Your submission is in the queue. We review submissions in order of receipt.';
      case 'under-review':
        return 'Our team is actively reviewing your submission. We may request additional information.';
      case 'approved':
        return 'Congratulations! Your module has been approved and will be published shortly.';
      case 'rejected':
        return 'Unfortunately, your submission was not approved. Please review the feedback below.';
      case 'suspended':
        return 'Your module has been suspended. Please contact support for details.';
      default:
        return '';
    }
  };

  const getStatusIcon = (status) => {
    switch (status) {
      case 'pending':
        return '⏳';
      case 'under-review':
        return '🔍';
      case 'approved':
        return '✓';
      case 'rejected':
        return '✗';
      case 'suspended':
        return '⚠';
      default:
        return '•';
    }
  };

  return (
    <div className="vendor-submission-status">
      <div className="status-container">
        <div className="status-header">
          <h1>Submission Status</h1>
          <p>Check the status of your module submission</p>
        </div>

        {!submission ? (
          <div className="status-form-section">
            <h2>Look Up Your Submission</h2>
            <form onSubmit={handleCheckStatus}>
              <div className="form-group">
                <label htmlFor="email">Email Address</label>
                <input
                  type="email"
                  id="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="your@email.com"
                  required
                />
                <small>The email you used when submitting your module</small>
              </div>

              <div className="form-group">
                <label htmlFor="submissionId">Submission ID</label>
                <input
                  type="text"
                  id="submissionId"
                  value={submissionId}
                  onChange={(e) => setSubmissionId(e.target.value)}
                  placeholder="e.g., 550e8400-e29b-41d4-a716-446655440000"
                  required
                />
                <small>
                  You received this ID in the email confirmation after submitting your module
                </small>
              </div>

              {error && <div className="alert alert-error">{error}</div>}

              <button type="submit" disabled={loading} className="btn btn-primary btn-large">
                {loading ? 'Checking...' : 'Check Status'}
              </button>
            </form>
          </div>
        ) : (
          <>
            <div className={`status-display status-${getStatusColor(submission.status)}`}>
              <div className="status-icon">{getStatusIcon(submission.status)}</div>
              <div className="status-info">
                <div className="status-label">
                  {submission.status.toUpperCase().replace('-', ' ')}
                </div>
                <h2>{submission.module_name}</h2>
                <p className="status-message">{getStatusMessage(submission.status)}</p>
              </div>
            </div>

            <div className="status-details">
              <section className="detail-section">
                <h3>Submission Details</h3>
                <div className="detail-grid">
                  <div className="detail-item">
                    <label>Module Name</label>
                    <p>{submission.module_name}</p>
                  </div>
                  <div className="detail-item">
                    <label>Vendor Name</label>
                    <p>{submission.vendor_name}</p>
                  </div>
                  <div className="detail-item">
                    <label>Submitted</label>
                    <p>{new Date(submission.submitted_at).toLocaleDateString('en-US', {
                      year: 'numeric',
                      month: 'long',
                      day: 'numeric'
                    })}</p>
                  </div>
                  {submission.reviewed_at && (
                    <div className="detail-item">
                      <label>Last Reviewed</label>
                      <p>{new Date(submission.reviewed_at).toLocaleDateString('en-US', {
                        year: 'numeric',
                        month: 'long',
                        day: 'numeric'
                      })}</p>
                    </div>
                  )}
                </div>
              </section>

              {submission.rejection_reason && (
                <section className="detail-section alert-section alert-danger">
                  <h3>Rejection Reason</h3>
                  <p>{submission.rejection_reason}</p>
                </section>
              )}

              {submission.admin_notes && (
                <section className="detail-section alert-section alert-info">
                  <h3>Admin Notes</h3>
                  <p>{submission.admin_notes}</p>
                </section>
              )}

              {submission.requires_special_review && (
                <section className="detail-section alert-section alert-warning">
                  <h3>⚠ Special Review Required</h3>
                  <p>
                    Your submission requires special review due to the permissions it requests.
                    Please allow additional time for our team to assess the security implications.
                  </p>
                </section>
              )}

              <section className="detail-section">
                <h3>Review Timeline</h3>
                <div className="timeline">
                  {submission.reviews && submission.reviews.length > 0 ? (
                    submission.reviews.map((review, idx) => (
                      <div key={idx} className="timeline-item">
                        <div className="timeline-marker" />
                        <div className="timeline-content">
                          <div className="timeline-action">
                            {review.action.toUpperCase().replace('_', ' ')}
                          </div>
                          <div className="timeline-date">
                            {new Date(review.created_at).toLocaleDateString('en-US', {
                              month: 'short',
                              day: 'numeric',
                              year: 'numeric',
                              hour: '2-digit',
                              minute: '2-digit'
                            })}
                          </div>
                          {review.comments && (
                            <p className="timeline-message">{review.comments}</p>
                          )}
                        </div>
                      </div>
                    ))
                  ) : (
                    <p className="no-timeline">No review activities yet</p>
                  )}
                </div>
              </section>

              <section className="detail-section">
                <h3>What Happens Next?</h3>
                <div className="next-steps">
                  {submission.status === 'pending' && (
                    <div className="step">
                      <div className="step-number">1</div>
                      <div className="step-content">
                        <h4>Under Review</h4>
                        <p>Our team will review your submission, typically within 5-7 business days.</p>
                      </div>
                    </div>
                  )}

                  {(submission.status === 'pending' || submission.status === 'under-review') && (
                    <>
                      <div className="step">
                        <div className="step-number">
                          {submission.status === 'under-review' ? '↳' : '2'}
                        </div>
                        <div className="step-content">
                          <h4>Approval Decision</h4>
                          <p>
                            We'll either approve your module or request additional information.
                            You'll receive an email notification.
                          </p>
                        </div>
                      </div>

                      <div className="step">
                        <div className="step-number">3</div>
                        <div className="step-content">
                          <h4>Published to Marketplace</h4>
                          <p>Once approved, your module will be published and available for communities to install.</p>
                        </div>
                      </div>

                      <div className="step">
                        <div className="step-number">4</div>
                        <div className="step-content">
                          <h4>Start Earning</h4>
                          <p>Receive revenue from communities installing your module, minus processor fees.</p>
                        </div>
                      </div>
                    </>
                  )}

                  {submission.status === 'approved' && (
                    <>
                      <div className="step completed">
                        <div className="step-number">✓</div>
                        <div className="step-content">
                          <h4>Approved!</h4>
                          <p>Your module has been approved and is being prepared for publication.</p>
                        </div>
                      </div>

                      <div className="step">
                        <div className="step-number">→</div>
                        <div className="step-content">
                          <h4>Published to Marketplace</h4>
                          <p>Your module will be published within 1-2 business days.</p>
                        </div>
                      </div>

                      <div className="step">
                        <div className="step-number">→</div>
                        <div className="step-content">
                          <h4>Start Earning</h4>
                          <p>Communities can now install your module and you'll earn revenue.</p>
                        </div>
                      </div>
                    </>
                  )}

                  {submission.status === 'rejected' && (
                    <div className="step">
                      <div className="step-number">↻</div>
                      <div className="step-content">
                        <h4>Can You Resubmit?</h4>
                        <p>
                          Yes! You can address the feedback and resubmit your module.
                          Please review the rejection reason above for details on what needs to change.
                        </p>
                      </div>
                    </div>
                  )}
                </div>
              </section>
            </div>

            <div className="status-actions">
              <button
                onClick={() => {
                  setSubmission(null);
                  setEmail('');
                  setSubmissionId('');
                }}
                className="btn btn-secondary"
              >
                Check Another Submission
              </button>
            </div>
          </>
        )}

        <div className="help-section">
          <h3>Need Help?</h3>
          <p>
            If you have questions about your submission or need assistance, please contact our support team
            at <a href="mailto:vendor-support@waddlebot.com">vendor-support@waddlebot.com</a>.
          </p>
        </div>
      </div>
    </div>
  );
}
