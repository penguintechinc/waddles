import { useState } from 'react';
import { Link } from 'react-router-dom';
import { getAllPlatformOptions } from '../utils/platformConfig';

const communityTypes = [
  { value: 'creator', label: 'Creator', icon: '🎬', description: 'Content creators, streamers, YouTubers' },
  { value: 'gaming', label: 'Gaming', icon: '🎮', description: 'Gaming communities, esports teams' },
  { value: 'shared_interest_group', label: 'Shared Interest', icon: '👥', description: 'Hobby groups, fan communities' },
  { value: 'corporate', label: 'Corporate', icon: '🏢', description: 'Business, professional organizations' },
  { value: 'workforce', label: 'Workforce', icon: '🏗️', description: 'Teams, departments, organizational groups' },
  { value: 'support', label: 'Support Portal', icon: '🎫', description: 'Help desk, customer support, ticket management' },
  { value: 'other', label: 'Other', icon: '📌', description: 'Other community types' },
];

function CommunityForm({
  onSubmit,
  saving = false,
  error = null,
  showOwnerFields = false,
  backLink,
  title = 'Create Community',
}) {
  const [form, setForm] = useState({
    name: '',
    displayName: '',
    description: '',
    platform: 'discord',
    platformServerId: '',
    ownerId: '',
    ownerName: '',
    isPublic: true,
    communityType: 'creator',
  });

  const handleSubmit = async (e) => {
    e.preventDefault();
    await onSubmit(form);
  };

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    let finalValue = type === 'checkbox' ? checked : value;

    // Auto-format community name: lowercase, replace spaces with hyphens, remove invalid chars
    if (name === 'name') {
      finalValue = value
        .toLowerCase()
        .replace(/[^a-z0-9\s\-_]/g, '')
        .replace(/\s+/g, '-')
        .replace(/-+/g, '-')
        .slice(0, 100);
    }

    setForm(prev => ({
      ...prev,
      [name]: finalValue,
    }));
  };

  return (
    <div>
      <div className="flex items-center gap-4 mb-6">
        <Link to={backLink} className="text-navy-400 hover:text-sky-300 transition-colors">
          <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
        </Link>
        <h1 className="text-2xl font-bold gradient-text">{title}</h1>
      </div>

      {error && (
        <div
          className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 mb-6"
          data-testid="community-form-error"
        >
          {typeof error === 'string' ? error : JSON.stringify(error)}
        </div>
      )}

      <div className="card p-6 max-w-2xl">
        <form onSubmit={handleSubmit} className="space-y-6">
          {/* Basic Info */}
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-sky-100 border-b border-navy-700 pb-2">Basic Information</h2>

            <div>
              <label htmlFor="community-form-community-name-url-friendly-auto-formatt" className="block text-sm font-medium text-sky-200 mb-1">
                Community Name *
                <span className="text-navy-400 font-normal ml-2">
                  (URL-friendly, auto-formatted)
                </span>
              </label>
              <input
                id="community-form-community-name-url-friendly-auto-formatt"
                type="text"
                name="name"
                value={form.name}
                onChange={handleChange}
                className="input w-full"
                placeholder="my-community"
                required
                data-testid="community-name-input"
              />
            </div>

            <div>
              <label htmlFor="community-form-display-name" className="block text-sm font-medium text-sky-200 mb-1">Display Name</label>
              <input
                id="community-form-display-name"
                type="text"
                name="displayName"
                value={form.displayName}
                onChange={handleChange}
                className="input w-full"
                placeholder="My Awesome Community"
              />
            </div>

            <div>
              <label htmlFor="community-form-description" className="block text-sm font-medium text-sky-200 mb-1">Description</label>
              <textarea
                id="community-form-description"
                name="description"
                value={form.description}
                onChange={handleChange}
                className="input w-full"
                rows={3}
                placeholder="A brief description of this community..."
              />
            </div>

            <div>
              <label htmlFor="community-form-community-type-determines-available-feat" className="block text-sm font-medium text-sky-200 mb-1">
                Community Type *
                <span className="text-navy-400 font-normal ml-2">
                  (determines available features)
                </span>
              </label>
              <select
                id="community-form-community-type-determines-available-feat"
                name="communityType"
                value={form.communityType}
                onChange={handleChange}
                className="input w-full"
                required
              >
                {communityTypes.map(type => (
                  <option key={type.value} value={type.value}>
                    {type.icon} {type.label} - {type.description}
                  </option>
                ))}
              </select>
              <p className="text-sm text-navy-400 mt-1">
                Note: Shoutout features are only available for Creator and Gaming communities.
              </p>
            </div>
          </div>

          {/* Platform Info */}
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-sky-100 border-b border-navy-700 pb-2">Platform Configuration</h2>

            <div>
              <label htmlFor="community-form-platform" className="block text-sm font-medium text-sky-200 mb-1">Platform *</label>
              <select
                id="community-form-platform"
                name="platform"
                value={form.platform}
                onChange={handleChange}
                className="input w-full"
                required
              >
                {getAllPlatformOptions()
                  .filter((p) => p.id !== 'hub')
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.icon} {p.label}
                    </option>
                  ))}
              </select>
            </div>

            <div>
              <label htmlFor="community-form-platform-server-channel-id-optional" className="block text-sm font-medium text-sky-200 mb-1">
                Platform Server/Channel ID
                <span className="text-navy-400 font-normal ml-2">(optional)</span>
              </label>
              <input
                id="community-form-platform-server-channel-id-optional"
                type="text"
                name="platformServerId"
                value={form.platformServerId}
                onChange={handleChange}
                className="input w-full"
                placeholder="e.g., Discord Server ID or Twitch Channel ID"
              />
            </div>
          </div>

          {/* Owner Info (conditionally shown for superadmin) */}
          {showOwnerFields && (
            <div className="space-y-4">
              <h2 className="text-lg font-semibold text-sky-100 border-b border-navy-700 pb-2">Owner Information</h2>

              <div>
                <label htmlFor="community-form-owner-name-optional" className="block text-sm font-medium text-sky-200 mb-1">
                  Owner Name
                  <span className="text-navy-400 font-normal ml-2">(optional)</span>
                </label>
                <input
                  id="community-form-owner-name-optional"
                  type="text"
                  name="ownerName"
                  value={form.ownerName}
                  onChange={handleChange}
                  className="input w-full"
                  placeholder="Community owner's display name"
                />
              </div>

              <div>
                <label htmlFor="community-form-owner-platform-id-optional" className="block text-sm font-medium text-sky-200 mb-1">
                  Owner Platform ID
                  <span className="text-navy-400 font-normal ml-2">(optional)</span>
                </label>
                <input
                  id="community-form-owner-platform-id-optional"
                  type="text"
                  name="ownerId"
                  value={form.ownerId}
                  onChange={handleChange}
                  className="input w-full"
                  placeholder="Owner's platform user ID"
                />
              </div>
            </div>
          )}

          {/* Settings */}
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-sky-100 border-b border-navy-700 pb-2">Settings</h2>

            <label className="flex items-center gap-3 text-sky-200">
              <input
                type="checkbox"
                name="isPublic"
                checked={form.isPublic}
                onChange={handleChange}
                className="w-4 h-4 rounded bg-navy-800 border-navy-600"
              />
              <div>
                <span className="font-medium">Public Community</span>
                <p className="text-sm text-navy-400">
                  Public communities are visible in the community directory
                </p>
              </div>
            </label>
          </div>

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-4 border-t border-navy-700">
            <Link to={backLink} className="btn btn-secondary">
              Cancel
            </Link>
            <button
              type="submit"
              disabled={saving}
              className="btn btn-primary"
              data-testid="create-community-submit"
            >
              {saving ? 'Creating...' : 'Create Community'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default CommunityForm;
