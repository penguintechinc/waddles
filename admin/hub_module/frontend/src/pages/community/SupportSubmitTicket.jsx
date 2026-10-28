import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { TicketIcon, CheckCircleIcon } from '@heroicons/react/24/outline';
import { supportApi } from '../../services/api';

function SupportSubmitTicket() {
  const { communityId } = useParams();
  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(null);
  const [error, setError] = useState(null);
  const [form, setForm] = useState({
    category_id: '',
    subject: '',
    description: '',
    custom_fields: {},
  });

  useEffect(() => {
    loadCategories();
  }, [communityId]);

  const loadCategories = async () => {
    try {
      const res = await supportApi.getCategories(communityId);
      setCategories(res.data?.categories || []);
    } catch (err) {
      console.error('Failed to load categories:', err);
    } finally {
      setLoading(false);
    }
  };

  const selectedCategory = categories.find((c) => String(c.id) === String(form.category_id));
  const formFields = selectedCategory?.form_fields || [];

  const handleChange = (name, value) => {
    setForm((prev) => ({ ...prev, [name]: value }));
  };

  const handleCustomFieldChange = (fieldLabel, value) => {
    setForm((prev) => ({
      ...prev,
      custom_fields: { ...prev.custom_fields, [fieldLabel]: value },
    }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    try {
      setSubmitting(true);
      const res = await supportApi.submitTicket(communityId, form);
      setSubmitted(res.data?.ticket || { ticket_number: 'Submitted' });
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to submit ticket');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <div className="p-8 text-center text-navy-400">Loading...</div>;
  }

  if (submitted) {
    return (
      <div className="max-w-lg mx-auto mt-12 text-center space-y-4">
        <CheckCircleIcon className="w-16 h-16 text-green-400 mx-auto" />
        <h1 className="text-2xl font-bold text-sky-100">Ticket Submitted</h1>
        <p className="text-navy-300">
          Your ticket <span className="text-gold-400 font-mono">{submitted.ticket_number}</span> has been created.
          We will get back to you soon.
        </p>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <TicketIcon className="w-8 h-8 text-gold-400" />
        <h1 className="text-2xl font-bold text-sky-100">Submit a Support Ticket</h1>
      </div>

      {error && (
        <div className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="bg-navy-900 border border-navy-700 rounded-lg p-6 space-y-4">
        {/* Category */}
        <div>
          <label htmlFor="support-submit-ticket-category" className="block text-sm font-medium text-sky-200 mb-1">Category</label>
          <select
            id="support-submit-ticket-category"
            value={form.category_id}
            onChange={(e) => handleChange('category_id', e.target.value)}
            className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm"
          >
            <option value="">Select a category...</option>
            {categories.map((cat) => (
              <option key={cat.id} value={cat.id}>{cat.name}</option>
            ))}
          </select>
        </div>

        {/* Subject */}
        <div>
          <label htmlFor="support-submit-ticket-subject" className="block text-sm font-medium text-sky-200 mb-1">Subject *</label>
          <input
            id="support-submit-ticket-subject"
            type="text"
            value={form.subject}
            onChange={(e) => handleChange('subject', e.target.value)}
            required
            placeholder="Brief summary of your issue"
            className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm placeholder-navy-500"
          />
        </div>

        {/* Description */}
        <div>
          <label htmlFor="support-submit-ticket-description" className="block text-sm font-medium text-sky-200 mb-1">Description</label>
          <textarea
            id="support-submit-ticket-description"
            value={form.description}
            onChange={(e) => handleChange('description', e.target.value)}
            rows={5}
            placeholder="Describe your issue in detail..."
            className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm placeholder-navy-500"
          />
        </div>

        {/* Dynamic custom fields from category */}
        {formFields.slice(0, 8).map((field, idx) => (
          <div key={idx}>
            <label className="block text-sm font-medium text-sky-200 mb-1">
              {field.label}{field.required && ' *'}
            </label>
            {field.type === 'textarea' ? (
              <textarea
                aria-label={field.label}
                value={form.custom_fields[field.label] || ''}
                onChange={(e) => handleCustomFieldChange(field.label, e.target.value)}
                required={field.required}
                rows={3}
                className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm"
              />
            ) : field.type === 'select' ? (
              <select
                aria-label={field.label}
                value={form.custom_fields[field.label] || ''}
                onChange={(e) => handleCustomFieldChange(field.label, e.target.value)}
                required={field.required}
                className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm"
              >
                <option value="">Select...</option>
                {(field.options || []).map((opt, oi) => (
                  <option key={oi} value={opt}>{opt}</option>
                ))}
              </select>
            ) : (
              <input
                aria-label={field.label}
                type={field.type || 'text'}
                value={form.custom_fields[field.label] || ''}
                onChange={(e) => handleCustomFieldChange(field.label, e.target.value)}
                required={field.required}
                className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm"
              />
            )}
          </div>
        ))}

        <div className="pt-4">
          <button
            type="submit"
            disabled={submitting || !form.subject.trim()}
            className="w-full px-4 py-3 bg-gold-500 text-navy-900 font-semibold rounded-lg hover:bg-gold-400 disabled:opacity-50 transition-colors"
          >
            {submitting ? 'Submitting...' : 'Submit Ticket'}
          </button>
        </div>
      </form>
    </div>
  );
}

export default SupportSubmitTicket;
