import { useState, useEffect, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import {
  DocumentTextIcon,
  PlusIcon,
  TrashIcon,
  EyeIcon,
  ArrowDownTrayIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

const FIELD_TYPES = [
  { value: 'text', label: 'Text' },
  { value: 'textarea', label: 'Text Area' },
  { value: 'email', label: 'Email' },
  { value: 'number', label: 'Number' },
  { value: 'select', label: 'Dropdown' },
  { value: 'radio', label: 'Radio Buttons' },
  { value: 'checkbox', label: 'Checkboxes' },
  { value: 'date', label: 'Date' },
];

// Visibility options for form access control
const VISIBILITY_OPTIONS = [
  { value: 'public', label: 'Public' },
  { value: 'registered', label: 'Registered Users' },
  { value: 'community', label: 'Community Members' },
  { value: 'admins', label: 'Admins Only' },
];

const RESULTS_VISIBILITY_OPTIONS = [
  { value: 'community', label: 'All Community Members' },
  { value: 'registered', label: 'All Registered Users' },
  { value: 'submitter_and_admins', label: 'Submitter + Mods/Admins' },
  { value: 'admins', label: 'Mods/Admins Only' },
];

/**
 * Parse field definitions from multiline format.
 * Format: type|label|placeholder|required (one per line)
 * Example: "text|Name|Enter your name|true"
 * Simplified: just "label" will create a required text field
 */
function parseFieldDefinitions(lines) {
  return lines
    .filter((line) => line.trim())
    .map((line) => {
      const parts = line.split('|').map((p) => p.trim());
      if (parts.length === 1) {
        // Simple format: just the label
        return { type: 'text', label: parts[0], placeholder: '', required: true, options: [] };
      }
      // Full format: type|label|placeholder|required
      return {
        type: FIELD_TYPES.find((t) => t.value === parts[0])?.value || 'text',
        label: parts[1] || parts[0],
        placeholder: parts[2] || '',
        required: parts[3] === 'true' || parts[3] === '1',
        options: [],
      };
    });
}

function AdminForms() {
  const { communityId } = useParams();
  const [forms, setForms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedForm, setSelectedForm] = useState(null);
  const [submissions, setSubmissions] = useState([]);

  useEffect(() => {
    loadForms();
  }, [communityId]);

  const loadForms = async () => {
    try {
      setLoading(true);
      const response = await adminApi.getForms(communityId);
      setForms(response.data?.forms || []);
    } catch (err) {
      setError('Failed to load forms');
    } finally {
      setLoading(false);
    }
  };

  const createForm = async (data) => {
    // Parse field definitions from multiline input
    const fieldLines = data.field_definitions || [];
    const parsedFields = parseFieldDefinitions(fieldLines);

    if (parsedFields.length === 0) {
      setError('At least 1 field required');
      throw new Error('At least 1 field required');
    }

    try {
      await adminApi.createForm(communityId, {
        title: data.title?.trim(),
        description: data.description?.trim() || '',
        fields: parsedFields,
        view_visibility: data.view_visibility,
        submit_visibility: data.submit_visibility,
        results_visibility: data.results_visibility || 'submitter_and_admins',
        allow_anonymous: data.allow_anonymous || false,
        submit_once_per_user: data.submit_once_per_user !== false,
      });
      setMessage({ type: 'success', text: 'Form created' });
      setShowCreateModal(false);
      loadForms();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create form');
      throw err;
    }
  };

  const deleteForm = async (formId) => {
    if (!window.confirm('Delete this form and all submissions?')) return;
    try {
      await adminApi.deleteForm(communityId, formId);
      setMessage({ type: 'success', text: 'Form deleted' });
      loadForms();
      if (selectedForm?.id === formId) setSelectedForm(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to delete form');
    }
  };

  const loadFormSubmissions = async (formId) => {
    try {
      const [formRes, subsRes] = await Promise.all([
        adminApi.getForm(communityId, formId),
        adminApi.getFormSubmissions(communityId, formId),
      ]);
      setSelectedForm(formRes.data?.form);
      setSubmissions(subsRes.data?.submissions || []);
    } catch (err) {
      setError('Failed to load form details');
    }
  };

  const exportSubmissions = () => {
    if (!selectedForm || submissions.length === 0) return;
    const headers = ['Submitted At', 'User ID', ...selectedForm.fields.map(f => f.label)];
    const rows = submissions.map(sub => [
      sub.submitted_at,
      sub.user_id || 'Anonymous',
      ...selectedForm.fields.map(f => sub.values[f.id] || ''),
    ]);
    const csv = [headers, ...rows].map(row => row.join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${selectedForm.title}-submissions.csv`;
    a.click();
  };

  // Build fields for FormModalBuilder
  const formFields = useMemo(() => [
    {
      name: 'title',
      type: 'text',
      label: 'Title',
      required: true,
      placeholder: 'Feedback Form',
    },
    {
      name: 'description',
      type: 'textarea',
      label: 'Description (optional)',
      placeholder: 'Tell us what you think...',
      rows: 3,
    },
    {
      name: 'field_definitions',
      type: 'multiline',
      label: 'Form Fields',
      required: true,
      placeholder: 'Enter one field per line.\nSimple: just the label (creates text field)\nAdvanced: type|label|placeholder|required\nExample: text|Name|Enter your name|true',
      rows: 6,
      helpText: 'One field per line. Simple format: "Field Label" (text, required). Advanced: "type|label|placeholder|required". Types: text, textarea, email, number, select, radio, checkbox, date.',
    },
    {
      name: 'view_visibility',
      type: 'select',
      label: 'Who can view',
      defaultValue: 'community',
      options: VISIBILITY_OPTIONS,
    },
    {
      name: 'submit_visibility',
      type: 'select',
      label: 'Who can submit',
      defaultValue: 'community',
      options: VISIBILITY_OPTIONS,
    },
    {
      name: 'results_visibility',
      type: 'select',
      label: 'Who can see results',
      defaultValue: 'submitter_and_admins',
      options: RESULTS_VISIBILITY_OPTIONS,
    },
    {
      name: 'allow_anonymous',
      type: 'checkbox',
      label: 'Allow anonymous submissions',
      defaultValue: false,
    },
    {
      name: 'submit_once_per_user',
      type: 'checkbox',
      label: 'One submission per user',
      defaultValue: true,
    },
  ], []);

  if (loading) {
    return (
      <div className="flex justify-center items-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-sky-500"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white flex items-center gap-2">
          <DocumentTextIcon className="h-8 w-8 text-sky-500" />
          Community Forms
        </h1>
        <button onClick={() => setShowCreateModal(true)} className="btn btn-primary">
          <PlusIcon className="h-5 w-5 mr-2" />
          Create Form
        </button>
      </div>

      {error && (
        <div className="bg-red-500/20 border border-red-500 text-red-300 px-4 py-3 rounded">
          {error}
          <button onClick={() => setError(null)} className="float-right">&times;</button>
        </div>
      )}

      {message && (
        <div className={`px-4 py-3 rounded ${message.type === 'success' ? 'bg-green-500/20 text-green-300' : 'bg-red-500/20 text-red-300'}`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">&times;</button>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Form List */}
        <div className="space-y-4">
          {forms.length === 0 ? (
            <div className="card p-8 text-center">
              <DocumentTextIcon className="h-16 w-16 text-gray-500 mx-auto mb-4" />
              <p className="text-gray-400">No forms yet. Create your first form!</p>
            </div>
          ) : (
            forms.map((form) => (
              <div
                key={form.id}
                data-click-delegate
                className={`card p-4 cursor-pointer hover:ring-2 hover:ring-sky-500 transition ${
                  selectedForm?.id === form.id ? 'ring-2 ring-sky-500' : ''
                }`}
                onClick={() => loadFormSubmissions(form.id)}
              >
                <div className="flex justify-between items-start">
                  <div>
                    <h3 className="text-white font-medium"><button type="button" className="text-left hover:underline focus-visible:underline">{form.title}</button></h3>
                    {form.description && (
                      <p className="text-gray-400 text-sm mt-1 line-clamp-2">{form.description}</p>
                    )}
                    <div className="flex items-center gap-4 mt-2 text-xs text-gray-500">
                      <span className="flex items-center gap-1">
                        <EyeIcon className="h-3 w-3" />
                        {form.view_visibility}
                      </span>
                      <span>{form.results_visibility || 'submitter_and_admins'}</span>
                      <span>{form.fields?.length || 0} fields</span>
                    </div>
                  </div>
                  <button
                    aria-label={`Delete form ${form.title}`}
                    onClick={(e) => { e.stopPropagation(); deleteForm(form.id); }}
                    className="btn btn-sm btn-danger"
                  >
                    <TrashIcon className="h-4 w-4" />
                  </button>
                </div>
              </div>
            ))
          )}
        </div>

        {/* Submissions */}
        <div className="card p-6">
          {selectedForm ? (
            <>
              <div className="flex justify-between items-center mb-4">
                <h2 className="text-lg font-semibold text-white">{selectedForm.title}</h2>
                <button onClick={exportSubmissions} className="btn btn-sm btn-secondary">
                  <ArrowDownTrayIcon className="h-4 w-4 mr-1" />
                  Export CSV
                </button>
              </div>
              <p className="text-gray-500 text-sm mb-4">
                {submissions.length} submission{submissions.length !== 1 ? 's' : ''}
              </p>
              {submissions.length === 0 ? (
                <p className="text-gray-400 text-center py-8">No submissions yet</p>
              ) : (
                <div className="space-y-3 max-h-96 overflow-y-auto">
                  {submissions.map((sub) => (
                    <div key={sub.id} className="p-3 bg-gray-800 rounded">
                      <div className="flex justify-between text-xs text-gray-500 mb-2">
                        <span>{sub.user_id || 'Anonymous'}</span>
                        <span>{new Date(sub.submitted_at).toLocaleString()}</span>
                      </div>
                      {selectedForm.fields?.map((field) => (
                        <div key={field.id} className="text-sm mb-1">
                          <span className="text-gray-400">{field.label}:</span>
                          <span className="text-white ml-2">
                            {JSON.stringify(sub.values[field.id]) || '-'}
                          </span>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <p className="text-gray-400 text-center py-8">Select a form to view submissions</p>
          )}
        </div>
      </div>

      {/* Create Form Modal */}
      <FormModalBuilder
        title="Create Form"
        fields={formFields}
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={createForm}
        submitButtonText="Create Form"
        cancelButtonText="Cancel"
        width="lg"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />
    </div>
  );
}

export default AdminForms;
