import { useState, useEffect, useMemo } from 'react';
import { superAdminApi } from '../../services/api';
import { Search, Plus, Edit2, Trash2, Eye, EyeOff, Star, Download, Package } from 'lucide-react';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_GOLD_COLORS } from '../../theme/waddlebotTheme';

export default function SuperAdminModuleRegistry() {
  const [modules, setModules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [isPublished, setIsPublished] = useState('');
  const [page, setPage] = useState(1);
  const [pagination, setPagination] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingModule, setEditingModule] = useState(null);
  const [editInitialValues, setEditInitialValues] = useState({
    name: '',
    displayName: '',
    description: '',
    version: '1.0.0',
    author: 'Waddles',
    category: 'general',
    iconUrl: '',
    isCore: false,
  });

  const categories = [
    { value: '', label: 'All Categories' },
    { value: 'general', label: 'General' },
    { value: 'moderation', label: 'Moderation' },
    { value: 'entertainment', label: 'Entertainment' },
    { value: 'music', label: 'Music' },
    { value: 'utility', label: 'Utility' },
    { value: 'games', label: 'Games' },
    { value: 'ai', label: 'AI & Automation' },
  ];

  useEffect(() => {
    loadModules();
  }, [search, category, isPublished, page]);

  const loadModules = async () => {
    try {
      setLoading(true);
      const response = await superAdminApi.getAllModules({
        search,
        category,
        isPublished,
        page,
        limit: 25,
      });
      setModules(response.data.modules);
      setPagination(response.data.pagination);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load modules');
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = async (data) => {
    try {
      await superAdminApi.createModule(data);
      setSuccess('Module created successfully');
      setShowCreateModal(false);
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to create module');
      throw err; // Re-throw to prevent modal from closing
    }
  };

  const handleUpdate = async (data) => {
    if (!editingModule) return;
    try {
      await superAdminApi.updateModule(editingModule.id, data);
      setSuccess('Module updated successfully');
      setShowEditModal(false);
      setEditingModule(null);
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to update module');
      throw err; // Re-throw to prevent modal from closing
    }
  };

  const handlePublish = async (moduleId, currentStatus) => {
    try {
      await superAdminApi.publishModule(moduleId, !currentStatus);
      setSuccess(`Module ${!currentStatus ? 'published' : 'unpublished'} successfully`);
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to update module');
    }
  };

  const handleDelete = async (moduleId) => {
    if (!confirm('Are you sure you want to delete this module? This cannot be undone.')) return;

    try {
      await superAdminApi.deleteModule(moduleId);
      setSuccess('Module deleted successfully');
      loadModules();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to delete module');
    }
  };

  const openEditModal = (module) => {
    setEditingModule(module);
    setEditInitialValues({
      name: module.name,
      displayName: module.displayName,
      description: module.description,
      version: module.version,
      author: module.author,
      category: module.category,
      iconUrl: module.iconUrl || '',
      isCore: module.isCore,
    });
    setShowEditModal(true);
  };

  // Category options for select fields (filter out empty value for form selects)
  const categoryOptions = categories
    .filter(c => c.value)
    .map(c => ({ value: c.value, label: c.label }));

  // Field definitions for create modal
  const createModuleFields = useMemo(() => [
    {
      name: 'name',
      type: 'text',
      label: 'Module Name',
      required: true,
      placeholder: 'e.g., my-module',
    },
    {
      name: 'displayName',
      type: 'text',
      label: 'Display Name',
      required: true,
      placeholder: 'e.g., My Module',
    },
    {
      name: 'description',
      type: 'textarea',
      label: 'Description',
      required: true,
      placeholder: 'Describe what this module does...',
      rows: 4,
    },
    {
      name: 'version',
      type: 'text',
      label: 'Version',
      required: true,
      defaultValue: '1.0.0',
      placeholder: 'e.g., 1.0.0',
    },
    {
      name: 'author',
      type: 'text',
      label: 'Author',
      required: true,
      defaultValue: 'Waddles',
      placeholder: 'e.g., Waddles',
    },
    {
      name: 'category',
      type: 'select',
      label: 'Category',
      defaultValue: 'general',
      options: categoryOptions,
    },
    {
      name: 'iconUrl',
      type: 'text',
      label: 'Icon URL (optional)',
      placeholder: 'https://example.com/icon.png',
    },
    {
      name: 'isCore',
      type: 'checkbox',
      label: 'Mark as Core Module',
      defaultValue: false,
    },
  ], [categoryOptions]);

  // Field definitions for edit modal (with initial values)
  const editModuleFields = useMemo(() => [
    {
      name: 'name',
      type: 'text',
      label: 'Module Name',
      required: true,
      disabled: true,
      defaultValue: editInitialValues.name,
    },
    {
      name: 'displayName',
      type: 'text',
      label: 'Display Name',
      required: true,
      defaultValue: editInitialValues.displayName,
    },
    {
      name: 'description',
      type: 'textarea',
      label: 'Description',
      required: true,
      rows: 4,
      defaultValue: editInitialValues.description,
    },
    {
      name: 'version',
      type: 'text',
      label: 'Version',
      required: true,
      defaultValue: editInitialValues.version,
    },
    {
      name: 'author',
      type: 'text',
      label: 'Author',
      required: true,
      defaultValue: editInitialValues.author,
    },
    {
      name: 'category',
      type: 'select',
      label: 'Category',
      defaultValue: editInitialValues.category,
      options: categoryOptions,
    },
    {
      name: 'iconUrl',
      type: 'text',
      label: 'Icon URL (optional)',
      defaultValue: editInitialValues.iconUrl,
    },
    {
      name: 'isCore',
      type: 'checkbox',
      label: 'Mark as Core Module',
      defaultValue: editInitialValues.isCore,
    },
  ], [editInitialValues, categoryOptions]);

  return (
    <div className="min-h-screen bg-navy-900 text-white p-6">
      <div className="max-w-7xl mx-auto">
        {/* Header */}
        <div className="mb-8 flex justify-between items-center">
          <div>
            <h1 className="text-3xl font-bold text-gold-400 mb-2">Module Registry</h1>
            <p className="text-fg-muted">Manage all marketplace modules</p>
          </div>
          <button
            onClick={() => setShowCreateModal(true)}
            className="flex items-center gap-2 px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 rounded-lg font-semibold transition-colors"
          >
            <Plus size={20} />
            Create Module
          </button>
        </div>

        {/* Success/Error Messages */}
        {success && (
          <div className="mb-6 bg-green-900/20 border border-green-500 text-green-400 px-4 py-3 rounded">
            {success}
            <button onClick={() => setSuccess(null)} className="float-right font-bold">×</button>
          </div>
        )}
        {error && (
          <div className="mb-6 bg-red-900/20 border border-red-500 text-red-400 px-4 py-3 rounded">
            {error}
            <button onClick={() => setError(null)} className="float-right font-bold">×</button>
          </div>
        )}

        {/* Filters */}
        <div className="mb-6 flex gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-fg-muted" size={20} />
            <input
              aria-label="Search modules"
              type="text"
              placeholder="Search modules..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full pl-10 pr-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white focus:border-sky-500"
            />
          </div>
          <select
            aria-label="Filter by category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white focus:border-sky-500"
          >
            {categories.map((cat) => (
              <option key={cat.value} value={cat.value}>
                {cat.label}
              </option>
            ))}
          </select>
          <select
            aria-label="Filter by status"
            value={isPublished}
            onChange={(e) => setIsPublished(e.target.value)}
            className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white focus:border-sky-500"
          >
            <option value="">All Status</option>
            <option value="true">Published</option>
            <option value="false">Unpublished</option>
          </select>
        </div>

        {/* Module Table */}
        <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
          <table className="w-full">
            <thead className="bg-navy-700">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-fg-muted uppercase tracking-wider">Module</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-fg-muted uppercase tracking-wider">Category</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-fg-muted uppercase tracking-wider">Stats</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-fg-muted uppercase tracking-wider">Status</th>
                <th className="px-6 py-3 text-right text-xs font-medium text-fg-muted uppercase tracking-wider">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-navy-700">
              {modules.map((module) => (
                <tr key={module.id} className="hover:bg-navy-700/50">
                  <td className="px-6 py-4">
                    <div className="flex items-center gap-3">
                      {module.iconUrl ? (
                        <img src={module.iconUrl} alt={module.displayName} className="w-10 h-10 rounded" />
                      ) : (
                        <div className="w-10 h-10 bg-navy-700 rounded flex items-center justify-center">
                          <Package className="text-sky-400" size={20} />
                        </div>
                      )}
                      <div>
                        <div className="font-semibold text-white">{module.displayName}</div>
                        <div className="text-sm text-fg-muted">{module.name}</div>
                        {module.isCore && (
                          <span className="text-xs text-gold-400">Core Module</span>
                        )}
                      </div>
                    </div>
                  </td>
                  <td className="px-6 py-4">
                    <span className="bg-navy-700 px-2 py-1 rounded text-sm">{module.category}</span>
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex flex-col gap-1 text-sm">
                      <div className="flex items-center gap-2">
                        <Star className="text-gold-400" size={14} />
                        <span>{module.avgRating} ({module.reviewCount})</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <Download className="text-sky-400" size={14} />
                        <span>{module.installCount} installs</span>
                      </div>
                    </div>
                  </td>
                  <td className="px-6 py-4">
                    {module.isPublished ? (
                      <span className="flex items-center gap-1 text-green-400">
                        <Eye size={16} />
                        Published
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-fg-muted">
                        <EyeOff size={16} />
                        Unpublished
                      </span>
                    )}
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => handlePublish(module.id, module.isPublished)}
                        className="p-2 hover:bg-navy-600 rounded-lg transition-colors"
                        title={module.isPublished ? 'Unpublish' : 'Publish'}
                      >
                        {module.isPublished ? <EyeOff size={18} /> : <Eye size={18} />}
                      </button>
                      <button
                        onClick={() => openEditModal(module)}
                        className="p-2 hover:bg-navy-600 rounded-lg transition-colors"
                        title="Edit"
                      >
                        <Edit2 size={18} />
                      </button>
                      <button
                        onClick={() => handleDelete(module.id)}
                        className="p-2 hover:bg-red-900/50 rounded-lg transition-colors text-red-400"
                        title="Delete"
                      >
                        <Trash2 size={18} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {pagination && pagination.totalPages > 1 && (
          <div className="mt-6 flex justify-center gap-2">
            <button
              onClick={() => setPage(page - 1)}
              disabled={page === 1}
              className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg disabled:opacity-50 hover:border-sky-500"
            >
              Previous
            </button>
            <span className="px-4 py-2 text-fg-muted">
              Page {page} of {pagination.totalPages}
            </span>
            <button
              onClick={() => setPage(page + 1)}
              disabled={page === pagination.totalPages}
              className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg disabled:opacity-50 hover:border-sky-500"
            >
              Next
            </button>
          </div>
        )}

        {/* Create Module Modal */}
        <FormModalBuilder
          title="Create Module"
          fields={createModuleFields}
          isOpen={showCreateModal}
          onClose={() => setShowCreateModal(false)}
          onSubmit={handleCreate}
          submitButtonText="Create"
          cancelButtonText="Cancel"
          width="lg"
          themeMode="dark"
          colors={WADDLES_GOLD_COLORS}
        />

        {/* Edit Module Modal */}
        <FormModalBuilder
          title="Edit Module"
          fields={editModuleFields}
          isOpen={showEditModal && editingModule !== null}
          onClose={() => {
            setShowEditModal(false);
            setEditingModule(null);
          }}
          onSubmit={handleUpdate}
          submitButtonText="Update"
          cancelButtonText="Cancel"
          width="lg"
          themeMode="dark"
          colors={WADDLES_GOLD_COLORS}
        />
      </div>
    </div>
  );
}
