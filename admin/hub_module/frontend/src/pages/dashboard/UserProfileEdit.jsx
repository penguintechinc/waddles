import { useState, useEffect, useRef } from 'react';
import { useAuth } from '../../contexts/AuthContext';
import { userApi } from '../../services/api';
import {
  UserCircleIcon,
  PhotoIcon,
  MapPinIcon,
  LinkIcon,
  EyeIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';

const VISIBILITY_OPTIONS = [
  { value: 'public', label: 'Public', description: 'Anyone can view your profile' },
  { value: 'registered', label: 'Registered Users', description: 'Only verified logged-in users can view' },
  { value: 'shared_communities', label: 'Shared Communities', description: 'Only users in your communities can view' },
  { value: 'community_leaders', label: 'Community Leaders', description: 'Only admins/mods of your communities can view' },
];

const PLATFORM_ICONS = {
  twitch: { color: '#9146FF', name: 'Twitch' },
  discord: { color: '#5865F2', name: 'Discord' },
  slack: { color: '#4A154B', name: 'Slack' },
  youtube: { color: '#FF0000', name: 'YouTube' },
  twitter: { color: '#1DA1F2', name: 'Twitter/X' },
  kick: { color: '#53FC18', name: 'Kick' },
};

function UserProfileEdit() {
  const { user, refreshUser } = useAuth();
  const fileInputRef = useRef(null);

  const [profile, setProfile] = useState({
    displayName: '',
    bio: '',
    locationCity: '',
    locationState: '',
    locationCountry: '',
    visibility: 'shared_communities',
    showActivity: true,
    showCommunities: true,
  });
  const [linkedPlatforms, setLinkedPlatforms] = useState([]);
  const [avatarPreview, setAvatarPreview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uploadingAvatar, setUploadingAvatar] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    loadProfile();
  }, []);

  async function loadProfile() {
    try {
      setLoading(true);
      const [profileRes, platformsRes] = await Promise.all([
        userApi.getMyProfile(),
        userApi.getLinkedPlatforms(),
      ]);
      if (profileRes.data.success) {
        const p = profileRes.data.profile;
        setProfile({
          displayName: p.displayName || '',
          bio: p.bio || '',
          locationCity: p.locationCity || '',
          locationState: p.locationState || '',
          locationCountry: p.locationCountry || '',
          visibility: p.visibility || 'shared_communities',
          showActivity: p.showActivity !== false,
          showCommunities: p.showCommunities !== false,
        });
        setAvatarPreview(p.avatarUrl);
      }
      if (platformsRes.data.success) {
        setLinkedPlatforms(platformsRes.data.platforms || []);
      }
    } catch (err) {
      console.error('Failed to load profile:', err);
      setMessage({ type: 'error', text: 'Failed to load profile' });
    } finally {
      setLoading(false);
    }
  }

  async function handleSave(e) {
    e.preventDefault();
    setSaving(true);
    setMessage(null);

    try {
      await userApi.updateProfile(profile);
      setMessage({ type: 'success', text: 'Profile saved successfully' });
      if (refreshUser) refreshUser();
    } catch (err) {
      console.error('Failed to save profile:', err);
      setMessage({
        type: 'error',
        text: err.response?.data?.error?.message || 'Failed to save profile',
      });
    } finally {
      setSaving(false);
    }
  }

  async function handleAvatarSelect(e) {
    const file = e.target.files?.[0];
    if (!file) return;

    // Validate file type
    if (!['image/jpeg', 'image/png', 'image/gif', 'image/webp'].includes(file.type)) {
      setMessage({ type: 'error', text: 'Please select a valid image file (JPEG, PNG, GIF, WebP)' });
      return;
    }

    // Validate file size (5MB)
    if (file.size > 5 * 1024 * 1024) {
      setMessage({ type: 'error', text: 'Image must be less than 5MB' });
      return;
    }

    // Show preview
    const reader = new FileReader();
    reader.onload = () => setAvatarPreview(reader.result);
    reader.readAsDataURL(file);

    // Upload
    setUploadingAvatar(true);
    setMessage(null);

    try {
      const response = await userApi.uploadAvatar(file);
      if (response.data.success) {
        setAvatarPreview(response.data.avatarUrl);
        setMessage({ type: 'success', text: 'Avatar uploaded successfully' });
        if (refreshUser) refreshUser();
      }
    } catch (err) {
      console.error('Failed to upload avatar:', err);
      setMessage({
        type: 'error',
        text: err.response?.data?.error?.message || 'Failed to upload avatar',
      });
    } finally {
      setUploadingAvatar(false);
    }
  }

  async function handleDeleteAvatar() {
    if (!confirm('Are you sure you want to delete your avatar?')) return;

    setUploadingAvatar(true);
    setMessage(null);

    try {
      const response = await userApi.deleteAvatar();
      if (response.data.success) {
        setAvatarPreview(response.data.avatarUrl);
        setMessage({ type: 'success', text: 'Avatar deleted' });
        if (refreshUser) refreshUser();
      }
    } catch (err) {
      console.error('Failed to delete avatar:', err);
      setMessage({ type: 'error', text: 'Failed to delete avatar' });
    } finally {
      setUploadingAvatar(false);
    }
  }

  function updateField(field, value) {
    setProfile((prev) => ({ ...prev, [field]: value }));
  }

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="max-w-3xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-sky-100">Edit Profile</h1>
        <p className="text-navy-400 mt-1">Customize your public profile and privacy settings</p>
      </div>

      {message && (
        <div
          className={`mb-6 p-4 rounded-lg border ${
            message.type === 'success'
              ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
              : 'bg-red-500/20 text-red-300 border-red-500/30'
          }`}
        >
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">
            ×
          </button>
        </div>
      )}

      <form onSubmit={handleSave} className="space-y-6">
        {/* Avatar Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <PhotoIcon className="w-5 h-5" />
            Profile Picture
          </h2>
          <div className="flex items-center gap-6">
            <div className="relative">
              {avatarPreview ? (
                <img
                  src={avatarPreview}
                  alt="Profile"
                  className="w-24 h-24 rounded-full object-cover border-2 border-navy-600"
                />
              ) : (
                <div className="w-24 h-24 rounded-full bg-navy-700 flex items-center justify-center border-2 border-navy-600">
                  <UserCircleIcon className="w-16 h-16 text-navy-500" />
                </div>
              )}
              {uploadingAvatar && (
                <div className="absolute inset-0 bg-navy-900/80 rounded-full flex items-center justify-center">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
                </div>
              )}
            </div>
            <div className="space-y-2">
              <input
                aria-label="Upload avatar image"
                ref={fileInputRef}
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp"
                onChange={handleAvatarSelect}
                className="hidden"
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploadingAvatar}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Upload New Picture
              </button>
              {avatarPreview && (
                <button
                  aria-label="Delete profile picture"
                  type="button"
                  onClick={handleDeleteAvatar}
                  disabled={uploadingAvatar}
                  className="btn bg-red-500/20 text-red-300 hover:bg-red-500/30 text-sm ml-2 disabled:opacity-50"
                >
                  <TrashIcon className="w-4 h-4" />
                </button>
              )}
              <p className="text-xs text-navy-500">JPEG, PNG, GIF, or WebP. Max 5MB.</p>
            </div>
          </div>
        </div>

        {/* Basic Info Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <UserCircleIcon className="w-5 h-5" />
            Basic Information
          </h2>
          <div className="space-y-4">
            <div>
              <label htmlFor="user-profile-edit-display-name" className="block text-sm font-medium text-navy-300 mb-2">Display Name</label>
              <input
                id="user-profile-edit-display-name"
                type="text"
                value={profile.displayName}
                onChange={(e) => updateField('displayName', e.target.value)}
                maxLength={100}
                placeholder={user?.username || 'Your display name'}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
            </div>

            <div>
              <label htmlFor="user-profile-edit-bio" className="block text-sm font-medium text-navy-300 mb-2">Bio</label>
              <textarea
                id="user-profile-edit-bio"
                value={profile.bio}
                onChange={(e) => updateField('bio', e.target.value)}
                maxLength={2000}
                rows={4}
                placeholder="Tell others about yourself..."
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500 resize-none"
              />
              <p className="text-xs text-navy-500 mt-1">{profile.bio.length}/2000 characters</p>
            </div>

            <div>
              <label htmlFor="user-profile-edit-input" className="block text-sm font-medium text-navy-300 mb-2">
                <MapPinIcon className="w-4 h-4 inline mr-1" />
                Location
              </label>
              <div className="grid md:grid-cols-3 gap-4">
                <input
                  id="user-profile-edit-input"
                  type="text"
                  value={profile.locationCity}
                  onChange={(e) => updateField('locationCity', e.target.value)}
                  maxLength={100}
                  placeholder="City"
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
                <input
                  aria-label="State/Province"
                  type="text"
                  value={profile.locationState}
                  onChange={(e) => updateField('locationState', e.target.value)}
                  maxLength={100}
                  placeholder="State/Province"
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
                <input
                  aria-label="Country"
                  type="text"
                  value={profile.locationCountry}
                  onChange={(e) => updateField('locationCountry', e.target.value)}
                  maxLength={100}
                  placeholder="Country"
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
              </div>
            </div>
          </div>
        </div>

        {/* Linked Platforms Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <LinkIcon className="w-5 h-5" />
            Linked Platforms
          </h2>
          <p className="text-navy-400 text-sm mb-4">
            Your linked platform accounts will be shown on your public profile.
            Manage your linked accounts in Account Settings.
          </p>
          {linkedPlatforms.length > 0 ? (
            <div className="grid md:grid-cols-2 gap-3">
              {linkedPlatforms.map((platform) => {
                const info = PLATFORM_ICONS[platform.platform] || { color: '#6B7280', name: platform.platform };
                return (
                  <div
                    key={platform.platform}
                    className="flex items-center gap-3 p-3 bg-navy-800 rounded-lg border border-navy-700"
                  >
                    <div
                      className="w-10 h-10 rounded-full flex items-center justify-center text-white font-bold text-sm"
                      style={{ backgroundColor: info.color }}
                    >
                      {info.name.charAt(0).toUpperCase()}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="font-medium text-sky-100">{info.name}</div>
                      <div className="text-sm text-navy-400 truncate">{platform.platformUsername}</div>
                    </div>
                    <div className="text-emerald-400 text-xs">Connected</div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-center py-6 text-navy-400">
              <LinkIcon className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <p>No platforms linked yet</p>
              <p className="text-sm mt-1">Link your accounts in Account Settings</p>
            </div>
          )}
        </div>

        {/* Privacy Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <EyeIcon className="w-5 h-5" />
            Privacy Settings
          </h2>

          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-navy-300 mb-3">
                Who can see your profile?
              </label>
              <div className="space-y-2">
                {VISIBILITY_OPTIONS.map((option) => (
                  <label
                    key={option.value}
                    className={`flex items-center p-4 rounded-lg cursor-pointer transition-colors ${
                      profile.visibility === option.value
                        ? 'bg-sky-500/20 border border-sky-500/30'
                        : 'bg-navy-800 border border-navy-700 hover:border-navy-600'
                    }`}
                  >
                    <input
                      type="radio"
                      name="visibility"
                      value={option.value}
                      checked={profile.visibility === option.value}
                      onChange={(e) => updateField('visibility', e.target.value)}
                      className="sr-only"
                    />
                    <div className="flex-1">
                      <div className="font-medium text-sky-100">{option.label}</div>
                      <div className="text-sm text-navy-400">{option.description}</div>
                    </div>
                    {profile.visibility === option.value && (
                      <div className="w-5 h-5 bg-sky-500 rounded-full flex items-center justify-center">
                        <svg className="w-3 h-3 text-white" fill="currentColor" viewBox="0 0 20 20">
                          <path
                            fillRule="evenodd"
                            d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                            clipRule="evenodd"
                          />
                        </svg>
                      </div>
                    )}
                  </label>
                ))}
              </div>
            </div>

            <div className="border-t border-navy-700 pt-4 space-y-3">
              <label className="flex items-center justify-between p-3 bg-navy-800 rounded-lg cursor-pointer">
                <div>
                  <div className="font-medium text-sky-100">Show Activity Stats</div>
                  <div className="text-sm text-navy-400">Display watch time and message counts</div>
                </div>
                <input
                  type="checkbox"
                  checked={profile.showActivity}
                  onChange={(e) => updateField('showActivity', e.target.checked)}
                  className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
                />
              </label>

              <label className="flex items-center justify-between p-3 bg-navy-800 rounded-lg cursor-pointer">
                <div>
                  <div className="font-medium text-sky-100">Show Communities</div>
                  <div className="text-sm text-navy-400">Display communities you belong to</div>
                </div>
                <input
                  type="checkbox"
                  checked={profile.showCommunities}
                  onChange={(e) => updateField('showCommunities', e.target.checked)}
                  className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
                />
              </label>
            </div>
          </div>
        </div>

        {/* Save Button */}
        <div className="flex justify-end gap-4">
          <button
            type="submit"
            disabled={saving}
            className="btn btn-primary px-8 disabled:opacity-50"
          >
            {saving ? 'Saving...' : 'Save Profile'}
          </button>
        </div>
      </form>
    </div>
  );
}

export default UserProfileEdit;
