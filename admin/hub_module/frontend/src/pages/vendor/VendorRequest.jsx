/**
 * Vendor Request Form
 * Allows users to request global vendor role
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import api from '../../services/api';

function VendorRequest() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState(null);
  const [existingRequest, setExistingRequest] = useState(null);
  const [formData, setFormData] = useState({
    companyName: '',
    companyWebsite: '',
    businessDescription: '',
    experienceSummary: '',
    contactEmail: user?.email || '',
    contactPhone: '',
  });

  useEffect(() => {
    // Check if user already has a pending/approved request
    checkExistingRequest();
  }, []);

  const checkExistingRequest = async () => {
    try {
      const response = await api.get('/vendor/request/status');
      if (response.data?.request) {
        setExistingRequest(response.data.request);
      }
    } catch (err) {
      // No existing request, proceed with form
    }
  };

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: value,
    }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const response = await api.post('/vendor/request', {
        ...formData,
        userEmail: user?.email,
        userDisplayName: user?.username,
      });

      setSuccess(true);
      setExistingRequest(response.data?.request);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to submit vendor request');
    } finally {
      setLoading(false);
    }
  };

  // If user is already a vendor, redirect
  if (user?.isVendor) {
    navigate('/vendor/dashboard');
    return null;
  }

  // If already has a pending/approved request
  if (existingRequest) {
    return (
      <div className="max-w-2xl mx-auto py-12">
        <div className={`border rounded-lg p-8 ${
          existingRequest.status === 'approved'
            ? 'bg-emerald-500/10 border-emerald-500/20'
            : existingRequest.status === 'pending'
            ? 'bg-yellow-500/10 border-yellow-500/20'
            : 'bg-red-500/10 border-red-500/20'
        }`}>
          <h1 className="text-3xl font-bold text-white mb-4">Your Request Status</h1>

          {existingRequest.status === 'approved' && (
            <div>
              <p className="text-emerald-400 text-lg font-semibold mb-4">
                ✓ Approved! Your vendor status is now active.
              </p>
              <p className="text-navy-300 mb-6">
                You can now submit modules to the marketplace. Visit your vendor dashboard to get started.
              </p>
              <a href="/vendor/dashboard" className="bg-emerald-600 hover:bg-emerald-700 text-white px-6 py-2 rounded-lg inline-block">
                Go to Vendor Dashboard
              </a>
            </div>
          )}

          {existingRequest.status === 'pending' && (
            <div>
              <p className="text-yellow-400 text-lg font-semibold mb-4">
                ⏳ Your request is pending review
              </p>
              <p className="text-navy-300 mb-2">
                Submitted on: {new Date(existingRequest.requestedAt).toLocaleDateString()}
              </p>
              <p className="text-navy-400 text-sm">
                Our admin team will review your request and get back to you shortly.
              </p>
            </div>
          )}

          {existingRequest.status === 'rejected' && (
            <div>
              <p className="text-red-400 text-lg font-semibold mb-4">
                ✗ Your request was not approved
              </p>
              {existingRequest.rejectionReason && (
                <div className="bg-red-500/20 border border-red-500/30 rounded p-4 mb-6">
                  <p className="font-medium text-red-300 mb-2">Reason:</p>
                  <p className="text-red-200">{existingRequest.rejectionReason}</p>
                </div>
              )}
              <p className="text-navy-300 mb-6">
                You can submit another request if you've addressed the concerns.
              </p>
              <button
                onClick={() => setExistingRequest(null)}
                className="bg-blue-600 hover:bg-blue-700 text-white px-6 py-2 rounded-lg"
              >
                Submit New Request
              </button>
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto py-8">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-white mb-2">Request Vendor Status</h1>
        <p className="text-navy-300">
          Become a vendor and submit your modules to the Waddles marketplace. Our team will review your request within 48 hours.
        </p>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg mb-6">
          {error}
        </div>
      )}

      {success && (
        <div className="bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 px-4 py-3 rounded-lg mb-6">
          ✓ Your vendor request has been submitted! Our team will review it and get back to you shortly.
        </div>
      )}

      <form onSubmit={handleSubmit} className="bg-navy-800 border border-navy-700 rounded-lg p-8 space-y-6">
        {/* Company Information */}
        <div>
          <h2 className="text-xl font-bold text-white mb-4">Company Information</h2>

          <div className="space-y-4">
            <div>
              <label htmlFor="vendor-request-company-name" className="block text-sm font-medium text-navy-300 mb-2">
                Company Name *
              </label>
              <input
                id="vendor-request-company-name"
                type="text"
                name="companyName"
                value={formData.companyName}
                onChange={handleChange}
                required
                className="w-full bg-navy-900 border border-navy-600 rounded px-4 py-2 text-white focus:border-gold-400"
                placeholder="Your company name"
              />
            </div>

            <div>
              <label htmlFor="vendor-request-company-website" className="block text-sm font-medium text-navy-300 mb-2">
                Company Website
              </label>
              <input
                id="vendor-request-company-website"
                type="url"
                name="companyWebsite"
                value={formData.companyWebsite}
                onChange={handleChange}
                className="w-full bg-navy-900 border border-navy-600 rounded px-4 py-2 text-white focus:border-gold-400"
                placeholder="https://example.com"
              />
            </div>

            <div>
              <label htmlFor="vendor-request-business-description" className="block text-sm font-medium text-navy-300 mb-2">
                Business Description *
              </label>
              <textarea
                id="vendor-request-business-description"
                name="businessDescription"
                value={formData.businessDescription}
                onChange={handleChange}
                required
                rows="4"
                className="w-full bg-navy-900 border border-navy-600 rounded px-4 py-2 text-white focus:border-gold-400"
                placeholder="Tell us about your business and why you want to submit modules to Waddles"
              />
            </div>

            <div>
              <label htmlFor="vendor-request-experience-expertise" className="block text-sm font-medium text-navy-300 mb-2">
                Experience & Expertise
              </label>
              <textarea
                id="vendor-request-experience-expertise"
                name="experienceSummary"
                value={formData.experienceSummary}
                onChange={handleChange}
                rows="4"
                className="w-full bg-navy-900 border border-navy-600 rounded px-4 py-2 text-white focus:border-gold-400"
                placeholder="Describe your experience with bot development, integrations, or relevant technology"
              />
            </div>
          </div>
        </div>

        {/* Contact Information */}
        <div>
          <h2 className="text-xl font-bold text-white mb-4">Contact Information</h2>

          <div className="space-y-4">
            <div>
              <label htmlFor="vendor-request-email-address" className="block text-sm font-medium text-navy-300 mb-2">
                Email Address *
              </label>
              <input
                id="vendor-request-email-address"
                type="email"
                name="contactEmail"
                value={formData.contactEmail}
                onChange={handleChange}
                required
                className="w-full bg-navy-900 border border-navy-600 rounded px-4 py-2 text-white focus:border-gold-400"
              />
            </div>

            <div>
              <label htmlFor="vendor-request-phone-number" className="block text-sm font-medium text-navy-300 mb-2">
                Phone Number
              </label>
              <input
                id="vendor-request-phone-number"
                type="tel"
                name="contactPhone"
                value={formData.contactPhone}
                onChange={handleChange}
                className="w-full bg-navy-900 border border-navy-600 rounded px-4 py-2 text-white focus:border-gold-400"
                placeholder="+1 (555) 000-0000"
              />
            </div>
          </div>
        </div>

        {/* Submit Button */}
        <div className="flex items-center space-x-4 pt-4 border-t border-navy-700">
          <button
            type="submit"
            disabled={loading}
            className="flex-1 bg-emerald-600 hover:bg-emerald-700 disabled:bg-navy-600 disabled:cursor-not-allowed text-white font-medium py-2 rounded-lg transition-colors"
          >
            {loading ? 'Submitting...' : 'Submit Request'}
          </button>
          <button
            type="button"
            onClick={() => navigate('/dashboard')}
            className="px-6 py-2 text-navy-300 hover:text-white transition-colors"
          >
            Cancel
          </button>
        </div>
      </form>

      <div className="mt-8 bg-navy-800 border border-navy-700 rounded-lg p-6">
        <h3 className="text-lg font-bold text-white mb-4">What happens next?</h3>
        <ol className="space-y-3 text-navy-300">
          <li className="flex items-start space-x-3">
            <span className="font-bold text-gold-400">1.</span>
            <span>You submit your vendor request</span>
          </li>
          <li className="flex items-start space-x-3">
            <span className="font-bold text-gold-400">2.</span>
            <span>Our admin team reviews your information (usually within 48 hours)</span>
          </li>
          <li className="flex items-start space-x-3">
            <span className="font-bold text-gold-400">3.</span>
            <span>You'll receive an approval or rejection notification</span>
          </li>
          <li className="flex items-start space-x-3">
            <span className="font-bold text-gold-400">4.</span>
            <span>If approved, you can start submitting modules to the marketplace</span>
          </li>
        </ol>
      </div>
    </div>
  );
}

export default VendorRequest;
