import { useState, useEffect, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi, publicApi } from '../../services/api';
import {
  PhotoIcon,
  GlobeAltIcon,
  ChatBubbleLeftRightIcon,
  EyeIcon,
  TrashIcon,
  LinkIcon,
} from '@heroicons/react/24/outline';

const VISIBILITY_OPTIONS = [
  { value: 'public', label: 'Public', description: 'Anyone can view your community profile' },
  { value: 'registered', label: 'Registered Users', description: 'Only verified logged-in users can view' },
  { value: 'members_only', label: 'Members Only', description: 'Only community members can view' },
];

const JOIN_MODE_OPTIONS = [
  { value: 'open', label: 'Open', description: 'Anyone can join immediately' },
  { value: 'approval', label: 'Requires Approval', description: 'New members require admin review before joining' },
  { value: 'invite', label: 'Invite Only', description: 'Members can only join via direct invite by an admin' },
];

const SOCIAL_PLATFORMS = [
  { key: 'twitter', label: 'Twitter/X', placeholder: 'https://twitter.com/...' },
  { key: 'youtube', label: 'YouTube', placeholder: 'https://youtube.com/...' },
  { key: 'tiktok', label: 'TikTok', placeholder: 'https://tiktok.com/@...' },
  { key: 'instagram', label: 'Instagram', placeholder: 'https://instagram.com/...' },
];

function AdminCommunityProfile() {
  const { communityId } = useParams();
  const logoInputRef = useRef(null);
  const bannerInputRef = useRef(null);

  const [profile, setProfile] = useState({
    displayName: '',
    description: '',
    aboutExtended: '',
    websiteUrl: '',
    discordInviteUrl: '',
    socialLinks: {},
    visibility: 'public',
    join_mode: 'open',
  });
  const [logoPreview, setLogoPreview] = useState(null);
  const [bannerPreview, setBannerPreview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uploadingLogo, setUploadingLogo] = useState(false);
  const [uploadingBanner, setUploadingBanner] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    loadProfile();
  }, [communityId]);

  async function loadProfile() {
    try {
      setLoading(true);
      const response = await publicApi.getCommunityProfile(communityId);
      if (response.data.success) {
        const c = response.data.community;
        setProfile({
          displayName: c.displayName || '',
          description: c.description || '',
          aboutExtended: c.aboutExtended || '',
          websiteUrl: c.websiteUrl || '',
          discordInviteUrl: c.discordInviteUrl || '',
          socialLinks: c.socialLinks || {},
          visibility: c.visibility || 'public',
          join_mode: c.join_mode || 'open',
        });
        setLogoPreview(c.logoUrl);
        setBannerPreview(c.bannerUrl);
      }
    } catch (err) {
      console.error('Failed to load profile:', err);
      setMessage({ type: 'error', text: 'Failed to load community profile' });
    } finally {
      setLoading(false);
    }
  }

  async function handleSave(e) {
    e.preventDefault();
    setSaving(true);
    setMessage(null);

    try {
      await adminApi.updateCommunityProfile(communityId, profile);
      setMessage({ type: 'success', text: 'Profile saved successfully' });
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

  async function handleLogoSelect(e) {
    const file = e.target.files?.[0];
    if (!file) return;

    if (!['image/jpeg', 'image/png', 'image/gif', 'image/webp'].includes(file.type)) {
      setMessage({ type: 'error', text: 'Please select a valid image file' });
      return;
    }

    if (file.size > 5 * 1024 * 1024) {
      setMessage({ type: 'error', text: 'Logo must be less than 5MB' });
      return;
    }

    const reader = new FileReader();
    reader.onload = () => setLogoPreview(reader.result);
    reader.readAsDataURL(file);

    setUploadingLogo(true);
    setMessage(null);

    try {
      const response = await adminApi.uploadCommunityLogo(communityId, file);
      if (response.data.success) {
        setLogoPreview(response.data.logoUrl);
        setMessage({ type: 'success', text: 'Logo uploaded successfully' });
      }
    } catch (err) {
      console.error('Failed to upload logo:', err);
      setMessage({ type: 'error', text: 'Failed to upload logo' });
    } finally {
      setUploadingLogo(false);
    }
  }

  async function handleDeleteLogo() {
    if (!confirm('Delete community logo?')) return;

    setUploadingLogo(true);
    try {
      await adminApi.deleteCommunityLogo(communityId);
      setLogoPreview(null);
      setMessage({ type: 'success', text: 'Logo deleted' });
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to delete logo' });
    } finally {
      setUploadingLogo(false);
    }
  }

  async function handleBannerSelect(e) {
    const file = e.target.files?.[0];
    if (!file) return;

    if (!['image/jpeg', 'image/png', 'image/gif', 'image/webp'].includes(file.type)) {
      setMessage({ type: 'error', text: 'Please select a valid image file' });
      return;
    }

    if (file.size > 10 * 1024 * 1024) {
      setMessage({ type: 'error', text: 'Banner must be less than 10MB' });
      return;
    }

    const reader = new FileReader();
    reader.onload = () => setBannerPreview(reader.result);
    reader.readAsDataURL(file);

    setUploadingBanner(true);
    setMessage(null);

    try {
      const response = await adminApi.uploadCommunityBanner(communityId, file);
      if (response.data.success) {
        setBannerPreview(response.data.bannerUrl);
        setMessage({ type: 'success', text: 'Banner uploaded successfully' });
      }
    } catch (err) {
      console.error('Failed to upload banner:', err);
      setMessage({ type: 'error', text: 'Failed to upload banner' });
    } finally {
      setUploadingBanner(false);
    }
  }

  async function handleDeleteBanner() {
    if (!confirm('Delete community banner?')) return;

    setUploadingBanner(true);
    try {
      await adminApi.deleteCommunityBanner(communityId);
      setBannerPreview(null);
      setMessage({ type: 'success', text: 'Banner deleted' });
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to delete banner' });
    } finally {
      setUploadingBanner(false);
    }
  }

  function updateField(field, value) {
    setProfile((prev) => ({ ...prev, [field]: value }));
  }

  function updateSocialLink(key, value) {
    setProfile((prev) => ({
      ...prev,
      socialLinks: { ...prev.socialLinks, [key]: value },
    }));
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
        <h1 className="text-2xl font-bold text-sky-100">Community Profile</h1>
        <p className="text-navy-400 mt-1">Customize your community's public presence</p>
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
          <button onClick={() => setMessage(null)} className="float-right">&times;</button>
        </div>
      )}

      <form onSubmit={handleSave} className="space-y-6">
        {/* Banner Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <PhotoIcon className="w-5 h-5" />
            Banner Image
          </h2>
          <div className="space-y-4">
            <div className="relative rounded-lg overflow-hidden bg-navy-800 border border-navy-700">
              {bannerPreview ? (
                <img
                  src={bannerPreview}
                  alt="Banner"
                  className="w-full h-48 object-cover"
                />
              ) : (
                <div className="w-full h-48 flex items-center justify-center">
                  <PhotoIcon className="w-16 h-16 text-navy-600" />
                </div>
              )}
              {uploadingBanner && (
                <div className="absolute inset-0 bg-navy-900/80 flex items-center justify-center">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
                </div>
              )}
            </div>
            <div className="flex gap-3">
              <input
                aria-label="Upload banner image"
                ref={bannerInputRef}
                type="file"
                accept="image/*"
                onChange={handleBannerSelect}
                className="hidden"
              />
              <button
                type="button"
                onClick={() => bannerInputRef.current?.click()}
                disabled={uploadingBanner}
                className="btn btn-secondary text-sm"
              >
                Upload Banner
              </button>
              {bannerPreview && (
                <button
                  aria-label="Delete banner image"
                  type="button"
                  onClick={handleDeleteBanner}
                  disabled={uploadingBanner}
                  className="btn bg-red-500/20 text-red-300 hover:bg-red-500/30 text-sm"
                >
                  <TrashIcon className="w-4 h-4" />
                </button>
              )}
            </div>
            <p className="text-xs text-navy-500">Recommended: 1200x400px. Max 10MB.</p>
          </div>
        </div>

        {/* Logo Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <PhotoIcon className="w-5 h-5" />
            Community Logo
          </h2>
          <div className="flex items-center gap-6">
            <div className="relative">
              {logoPreview ? (
                <img
                  src={logoPreview}
                  alt="Logo"
                  className="w-24 h-24 rounded-lg object-cover border-2 border-navy-600"
                />
              ) : (
                <div className="w-24 h-24 rounded-lg bg-navy-700 flex items-center justify-center border-2 border-navy-600">
                  <PhotoIcon className="w-12 h-12 text-navy-500" />
                </div>
              )}
              {uploadingLogo && (
                <div className="absolute inset-0 bg-navy-900/80 rounded-lg flex items-center justify-center">
                  <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-gold-400"></div>
                </div>
              )}
            </div>
            <div className="space-y-2">
              <input
                aria-label="Upload logo image"
                ref={logoInputRef}
                type="file"
                accept="image/*"
                onChange={handleLogoSelect}
                className="hidden"
              />
              <button
                type="button"
                onClick={() => logoInputRef.current?.click()}
                disabled={uploadingLogo}
                className="btn btn-secondary text-sm"
              >
                Upload Logo
              </button>
              {logoPreview && (
                <button
                  aria-label="Delete logo image"
                  type="button"
                  onClick={handleDeleteLogo}
                  disabled={uploadingLogo}
                  className="btn bg-red-500/20 text-red-300 hover:bg-red-500/30 text-sm ml-2"
                >
                  <TrashIcon className="w-4 h-4" />
                </button>
              )}
              <p className="text-xs text-navy-500">Min 96x96px. Max 5MB.</p>
            </div>
          </div>
        </div>

        {/* Basic Info Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <ChatBubbleLeftRightIcon className="w-5 h-5" />
            About
          </h2>
          <div className="space-y-4">
            <div>
              <label htmlFor="admin-community-profile-display-name" className="block text-sm font-medium text-navy-300 mb-2">Display Name</label>
              <input
                id="admin-community-profile-display-name"
                type="text"
                value={profile.displayName}
                onChange={(e) => updateField('displayName', e.target.value)}
                maxLength={100}
                placeholder="Community display name"
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
            </div>

            <div>
              <label htmlFor="admin-community-profile-short-description" className="block text-sm font-medium text-navy-300 mb-2">Short Description</label>
              <input
                id="admin-community-profile-short-description"
                type="text"
                value={profile.description}
                onChange={(e) => updateField('description', e.target.value)}
                maxLength={200}
                placeholder="Brief description of your community"
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <p className="text-xs text-navy-500 mt-1">{profile.description.length}/200</p>
            </div>

            <div>
              <label htmlFor="admin-community-profile-about-extended" className="block text-sm font-medium text-navy-300 mb-2">About (Extended)</label>
              <textarea
                id="admin-community-profile-about-extended"
                value={profile.aboutExtended}
                onChange={(e) => updateField('aboutExtended', e.target.value)}
                maxLength={5000}
                rows={6}
                placeholder="Tell visitors about your community, its history, goals, and what makes it special..."
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500 resize-none"
              />
              <p className="text-xs text-navy-500 mt-1">{profile.aboutExtended.length}/5000</p>
            </div>
          </div>
        </div>

        {/* Links Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <LinkIcon className="w-5 h-5" />
            Links
          </h2>
          <div className="space-y-4">
            <div className="grid md:grid-cols-2 gap-4">
              <div>
                <label htmlFor="admin-community-profile-input" className="block text-sm font-medium text-navy-300 mb-2">
                  <GlobeAltIcon className="w-4 h-4 inline mr-1" />
                  Website
                </label>
                <input
                  id="admin-community-profile-input"
                  type="url"
                  value={profile.websiteUrl}
                  onChange={(e) => updateField('websiteUrl', e.target.value)}
                  placeholder="https://yourwebsite.com"
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
              </div>

              <div>
                <label htmlFor="admin-community-profile-discord-invite" className="block text-sm font-medium text-navy-300 mb-2">
                  Discord Invite
                </label>
                <input
                  id="admin-community-profile-discord-invite"
                  type="url"
                  value={profile.discordInviteUrl}
                  onChange={(e) => updateField('discordInviteUrl', e.target.value)}
                  placeholder="https://discord.gg/..."
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
              </div>
            </div>

            <div className="border-t border-navy-700 pt-4">
              <label className="block text-sm font-medium text-navy-300 mb-3">Social Media</label>
              <div className="grid md:grid-cols-2 gap-4">
                {SOCIAL_PLATFORMS.map((platform) => (
                  <div key={platform.key}>
                    <label className="block text-xs text-navy-400 mb-1">{platform.label}</label>
                    <input
                      aria-label={platform.label}
                      type="url"
                      value={profile.socialLinks[platform.key] || ''}
                      onChange={(e) => updateSocialLink(platform.key, e.target.value)}
                      placeholder={platform.placeholder}
                      className="w-full px-3 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500 text-sm"
                    />
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* Visibility Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <EyeIcon className="w-5 h-5" />
            Profile Visibility
          </h2>
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

        {/* Member Joining Section */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <EyeIcon className="w-5 h-5" />
            Member Joining
          </h2>
          <div className="space-y-2">
            {JOIN_MODE_OPTIONS.map((option) => (
              <label
                key={option.value}
                className={`flex items-center p-4 rounded-lg cursor-pointer transition-colors ${
                  (profile.join_mode || 'open') === option.value
                    ? 'bg-sky-500/20 border border-sky-500/30'
                    : 'bg-navy-800 border border-navy-700 hover:border-navy-600'
                }`}
              >
                <input
                  type="radio"
                  name="join_mode"
                  value={option.value}
                  checked={(profile.join_mode || 'open') === option.value}
                  onChange={(e) => updateField('join_mode', e.target.value)}
                  className="sr-only"
                />
                <div className="flex-1">
                  <div className="font-medium text-sky-100">{option.label}</div>
                  <div className="text-sm text-navy-400">{option.description}</div>
                </div>
                {(profile.join_mode || 'open') === option.value && (
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

export default AdminCommunityProfile;
