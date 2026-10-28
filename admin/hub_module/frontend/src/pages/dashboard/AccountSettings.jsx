import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import api, { passkeyApi, userOAuthApi } from '../../services/api';
import {
  UserCircleIcon,
  LinkIcon,
  CheckCircleIcon,
  KeyIcon,
  ShieldCheckIcon,
} from '@heroicons/react/24/outline';

// Platform icons and colors
const PLATFORMS = {
  discord: {
    name: 'Discord',
    color: '#5865F2',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z"/>
      </svg>
    ),
  },
  twitch: {
    name: 'Twitch',
    color: '#9146FF',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714Z"/>
      </svg>
    ),
  },
  slack: {
    name: 'Slack',
    color: '#36C5F0',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/>
      </svg>
    ),
  },
  youtube: {
    name: 'YouTube',
    color: '#FF0000',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/>
      </svg>
    ),
  },
  kick: {
    name: 'KICK',
    color: '#53FC18',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm-1-13h2v6h-2zm0 8h2v2h-2z"/>
      </svg>
    ),
  },
};

function AccountSettings() {
  const { user } = useAuth();
  const [searchParams] = useSearchParams();
  const [linkedPlatforms, setLinkedPlatforms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [linking, setLinking] = useState(null);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Passkey state
  const [passkeys, setPasskeys] = useState([]);
  const [passkeyLoading, setPasskeyLoading] = useState(false);
  const [passkeyError, setPasskeyError] = useState('');
  const [registeringPasskey, setRegisteringPasskey] = useState(false);

  // OAuth credentials state
  const [oauthCredentials, setOauthCredentials] = useState([]);
  const [oauthLoading, setOauthLoading] = useState(true);
  const [oauthError, setOauthError] = useState(null);
  const [oauthSuccess, setOauthSuccess] = useState(null);
  const [showOAuthForm, setShowOAuthForm] = useState(false);
  const [oauthForm, setOauthForm] = useState({ platform: '', clientId: '', clientSecret: '', scopes: '' });

  // Password form state
  const [showPasswordForm, setShowPasswordForm] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [savingPassword, setSavingPassword] = useState(false);

  useEffect(() => {
    // Check for OAuth callback messages
    const linked = searchParams.get('linked');
    const linkError = searchParams.get('error');

    if (linked) {
      setSuccess(`Successfully linked your ${PLATFORMS[linked]?.name || linked} account!`);
    } else if (linkError === 'link_denied') {
      setError('Platform linking was cancelled');
    } else if (linkError === 'platform_already_linked') {
      setError('This platform account is already linked to another user');
    } else if (linkError === 'link_failed') {
      setError('Failed to link platform. Please try again.');
    }

    loadUserData();
    loadOAuthCredentials();
  }, [searchParams]);

  const loadOAuthCredentials = async () => {
    try {
      setOauthLoading(true);
      const response = await userOAuthApi.getCredentials();
      setOauthCredentials(response.data?.data || []);
    } catch {
      // Non-critical — silently fail if endpoint not yet available
      setOauthCredentials([]);
    } finally {
      setOauthLoading(false);
    }
  };

  const handleOAuthCreate = async (e) => {
    e.preventDefault();
    try {
      setOauthError(null);
      await userOAuthApi.createCredential({
        platform: oauthForm.platform,
        clientId: oauthForm.clientId,
        clientSecret: oauthForm.clientSecret,
        scopes: oauthForm.scopes ? oauthForm.scopes.split(',').map(s => s.trim()) : [],
      });
      setOauthSuccess('OAuth credential created');
      setShowOAuthForm(false);
      setOauthForm({ platform: '', clientId: '', clientSecret: '', scopes: '' });
      loadOAuthCredentials();
      setTimeout(() => setOauthSuccess(null), 3000);
    } catch (err) {
      setOauthError(err.response?.data?.error || 'Failed to create credential');
    }
  };

  const handleOAuthDelete = async (id) => {
    if (!window.confirm('Delete this OAuth credential?')) return;
    try {
      setOauthError(null);
      await userOAuthApi.deleteCredential(id);
      setOauthSuccess('Credential deleted');
      loadOAuthCredentials();
      setTimeout(() => setOauthSuccess(null), 3000);
    } catch (err) {
      setOauthError(err.response?.data?.error || 'Failed to delete credential');
    }
  };

  const loadUserData = async () => {
    try {
      setLoading(true);
      const response = await api.get('/api/v1/auth/me');
      if (response.data.success && response.data.user) {
        setLinkedPlatforms(response.data.user.linkedPlatforms || []);
      }
      const pkRes = await passkeyApi.listCredentials().catch(() => ({ data: { credentials: [] } }));
      setPasskeys(pkRes.data?.credentials || []);
    } catch (err) {
      console.error('Failed to load user data:', err);
      setError('Failed to load account information');
    } finally {
      setLoading(false);
    }
  };

  const handleRegisterPasskey = async () => {
    setRegisteringPasskey(true);
    setPasskeyError('');
    try {
      const optRes = await passkeyApi.startRegistration();
      const options = optRes.data;
      const credential = await navigator.credentials.create({ publicKey: {
        ...options,
        challenge: Uint8Array.from(atob(options.challenge.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0)),
        user: { ...options.user, id: Uint8Array.from(atob(options.user.id.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0)) },
        excludeCredentials: (options.excludeCredentials || []).map(ec => ({ ...ec, id: Uint8Array.from(atob(ec.id.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0)) })),
      }});
      const encoded = {
        id: credential.id,
        rawId: btoa(String.fromCharCode(...new Uint8Array(credential.rawId))),
        type: credential.type,
        response: {
          attestationObject: btoa(String.fromCharCode(...new Uint8Array(credential.response.attestationObject))),
          clientDataJSON: btoa(String.fromCharCode(...new Uint8Array(credential.response.clientDataJSON))),
        },
      };
      await passkeyApi.finishRegistration(encoded);
      const updated = await passkeyApi.listCredentials();
      setPasskeys(updated.data?.credentials || []);
    } catch (err) {
      setPasskeyError(err?.response?.data?.error || err?.message || 'Passkey registration failed.');
    } finally {
      setRegisteringPasskey(false);
    }
  };

  const handleRemovePasskey = async (id) => {
    if (!confirm('Are you sure you want to remove this passkey?')) return;
    setPasskeyLoading(true);
    try {
      await passkeyApi.removeCredential(id);
      const updated = await passkeyApi.listCredentials();
      setPasskeys(updated.data?.credentials || []);
    } catch (err) {
      setPasskeyError(err?.response?.data?.error || err?.message || 'Failed to remove passkey.');
    } finally {
      setPasskeyLoading(false);
    }
  };

  const handleLinkPlatform = async (platform) => {
    try {
      setError(null);
      setSuccess(null);
      setLinking(platform);

      const response = await api.get(`/api/v1/auth/oauth/${platform}/link`);
      if (response.data.authorizeUrl) {
        window.location.href = response.data.authorizeUrl;
      }
    } catch (err) {
      console.error('Failed to start linking:', err);
      setError(err.response?.data?.error?.message || `Failed to link ${PLATFORMS[platform]?.name}`);
      setLinking(null);
    }
  };

  const handleUnlinkPlatform = async (platform) => {
    if (!confirm(`Are you sure you want to unlink your ${PLATFORMS[platform]?.name} account?`)) {
      return;
    }

    try {
      setError(null);
      setSuccess(null);

      await api.delete(`/api/v1/auth/oauth/${platform}`);
      setSuccess(`Successfully unlinked ${PLATFORMS[platform]?.name}`);
      loadUserData();
    } catch (err) {
      console.error('Failed to unlink:', err);
      setError(err.response?.data?.error?.message || `Failed to unlink ${PLATFORMS[platform]?.name}`);
    }
  };

  const handleSetPassword = async (e) => {
    e.preventDefault();

    if (newPassword !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    if (newPassword.length < 8) {
      setError('Password must be at least 8 characters');
      return;
    }

    try {
      setSavingPassword(true);
      setError(null);

      await api.post('/api/v1/auth/password', {
        currentPassword: user?.hasPassword ? currentPassword : undefined,
        newPassword,
      });

      setSuccess('Password updated successfully');
      setShowPasswordForm(false);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      loadUserData();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to update password');
    } finally {
      setSavingPassword(false);
    }
  };

  const isLinked = (platform) => {
    return linkedPlatforms.some(p => p.platform === platform);
  };

  const getLinkedInfo = (platform) => {
    return linkedPlatforms.find(p => p.platform === platform);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="max-w-4xl">
      <h1 className="text-2xl font-bold gradient-text mb-6">Account Settings</h1>

      {error && (
        <div className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 mb-6">
          {error}
        </div>
      )}

      {success && (
        <div className="p-4 bg-emerald-500/20 border border-emerald-500/30 rounded-lg text-emerald-300 mb-6 flex items-center gap-2">
          <CheckCircleIcon className="w-5 h-5" />
          {success}
        </div>
      )}

      {/* Profile Information */}
      <div className="card p-6 mb-6">
        <div className="flex items-center gap-3 mb-6">
          <UserCircleIcon className="w-6 h-6 text-gold-400" />
          <h2 className="text-xl font-semibold text-sky-100">Profile Information</h2>
        </div>

        <div className="space-y-4">
          <div className="flex items-center gap-4">
            {user?.avatarUrl ? (
              <img src={user.avatarUrl} alt={user.username} className="w-16 h-16 rounded-full" />
            ) : (
              <div className="w-16 h-16 rounded-full bg-navy-700 flex items-center justify-center border border-navy-600">
                <span className="text-2xl text-sky-400 font-bold">
                  {user?.username?.charAt(0).toUpperCase()}
                </span>
              </div>
            )}
            <div>
              <div className="text-lg font-semibold text-sky-100">{user?.username}</div>
              <div className="text-sm text-navy-400">{user?.email}</div>
            </div>
          </div>
        </div>
      </div>

      {/* Connected Platforms */}
      <div className="card p-6 mb-6">
        <div className="flex items-center gap-3 mb-6">
          <LinkIcon className="w-6 h-6 text-gold-400" />
          <h2 className="text-xl font-semibold text-sky-100">Connected Platforms</h2>
        </div>

        <p className="text-navy-300 mb-6">
          Connect your accounts to use Waddles across different platforms. When you log in with a platform,
          it will automatically be linked to your account.
        </p>

        <div className="space-y-3">
          {Object.entries(PLATFORMS).map(([key, platform]) => {
            const linked = isLinked(key);
            const info = getLinkedInfo(key);

            return (
              <div
                key={key}
                className={`flex items-center justify-between p-4 rounded-lg border transition-colors ${
                  linked
                    ? 'bg-navy-800 border-navy-600'
                    : 'bg-navy-900 border-navy-700 hover:border-navy-600'
                }`}
              >
                <div className="flex items-center gap-4">
                  <div
                    className="w-10 h-10 rounded-lg flex items-center justify-center"
                    style={{ backgroundColor: `${platform.color}20`, color: platform.color }}
                  >
                    {platform.icon}
                  </div>
                  <div>
                    <div className="font-medium text-sky-100">{platform.name}</div>
                    {linked && info?.username && (
                      <div className="text-sm text-navy-400">@{info.username}</div>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  {linked ? (
                    <>
                      <span className="text-sm text-emerald-400 flex items-center gap-1">
                        <CheckCircleIcon className="w-4 h-4" />
                        Connected
                      </span>
                      <button
                        onClick={() => handleUnlinkPlatform(key)}
                        className="btn btn-secondary text-sm py-1 px-3"
                      >
                        Disconnect
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={() => handleLinkPlatform(key)}
                      disabled={linking === key}
                      className="btn btn-primary text-sm py-1 px-3"
                      style={{ borderColor: platform.color }}
                    >
                      {linking === key ? 'Connecting...' : 'Connect'}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Password & Security */}
      <div className="card p-6">
        <div className="flex items-center gap-3 mb-6">
          <ShieldCheckIcon className="w-6 h-6 text-gold-400" />
          <h2 className="text-xl font-semibold text-sky-100">Password & Security</h2>
        </div>

        <div className="space-y-4">
          <div className="flex items-center justify-between p-4 bg-navy-800 rounded-lg border border-navy-700">
            <div className="flex items-center gap-3">
              <KeyIcon className="w-5 h-5 text-navy-400" />
              <div>
                <div className="font-medium text-sky-100">Password</div>
                <div className="text-sm text-navy-400">
                  {user?.hasPassword
                    ? 'Password is set. You can change it anytime.'
                    : 'No password set. Add one to enable email login.'}
                </div>
              </div>
            </div>
            <button
              onClick={() => setShowPasswordForm(!showPasswordForm)}
              className="btn btn-secondary text-sm"
            >
              {user?.hasPassword ? 'Change' : 'Set Password'}
            </button>
          </div>

          {showPasswordForm && (
            <form onSubmit={handleSetPassword} className="p-4 bg-navy-900 rounded-lg border border-navy-700 space-y-4">
              {user?.hasPassword && (
                <div>
                  <label htmlFor="account-settings-current-password" className="block text-sm font-medium text-sky-200 mb-1">
                    Current Password
                  </label>
                  <input
                    id="account-settings-current-password"
                    type="password"
                    value={currentPassword}
                    onChange={(e) => setCurrentPassword(e.target.value)}
                    className="input"
                    required={user?.hasPassword}
                  />
                </div>
              )}
              <div>
                <label htmlFor="account-settings-new-password" className="block text-sm font-medium text-sky-200 mb-1">
                  New Password
                </label>
                <input
                  id="account-settings-new-password"
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  className="input"
                  placeholder="At least 8 characters"
                  minLength={8}
                  required
                />
              </div>
              <div>
                <label htmlFor="account-settings-confirm-new-password" className="block text-sm font-medium text-sky-200 mb-1">
                  Confirm New Password
                </label>
                <input
                  id="account-settings-confirm-new-password"
                  type="password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  className="input"
                  required
                />
              </div>
              <div className="flex gap-2">
                <button
                  type="submit"
                  disabled={savingPassword}
                  className="btn btn-primary"
                >
                  {savingPassword ? 'Saving...' : 'Save Password'}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setShowPasswordForm(false);
                    setCurrentPassword('');
                    setNewPassword('');
                    setConfirmPassword('');
                  }}
                  className="btn btn-secondary"
                >
                  Cancel
                </button>
              </div>
            </form>
          )}

          <p className="text-sm text-navy-500">
            You can always sign in using any connected platform. Setting a password allows you to also sign in with your email.
          </p>
        </div>
      </div>

      {/* Passkeys */}
      <div className="card p-6 mt-6">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <KeyIcon className="w-6 h-6 text-gold-400" />
            <h2 className="text-xl font-semibold text-sky-100">Passkeys</h2>
          </div>
          <button
            onClick={handleRegisterPasskey}
            disabled={registeringPasskey || passkeyLoading}
            className="btn btn-primary text-sm"
          >
            {registeringPasskey ? 'Registering...' : 'Add Passkey'}
          </button>
        </div>

        {passkeyError && (
          <div className="p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm mb-4">
            {passkeyError}
          </div>
        )}

        <p className="text-navy-300 mb-4 text-sm">
          Passkeys let you sign in securely using your device's biometrics or PIN, without a password.
        </p>

        {passkeys.length === 0 ? (
          <div className="p-4 bg-navy-900 rounded-lg border border-navy-700 text-navy-400 text-sm text-center">
            No passkeys registered yet.
          </div>
        ) : (
          <div className="space-y-2">
            {passkeys.map((pk) => (
              <div
                key={pk.id}
                className="flex items-center justify-between p-4 bg-navy-800 rounded-lg border border-navy-700"
              >
                <div className="flex items-center gap-3">
                  <KeyIcon className="w-5 h-5 text-navy-400" />
                  <div>
                    <div className="font-medium text-sky-100">{pk.device_name || 'Unnamed device'}</div>
                    <div className="text-xs text-navy-400">
                      Added {pk.created_at ? new Date(pk.created_at).toLocaleDateString() : 'unknown'}
                      {pk.last_used_at && ` · Last used ${new Date(pk.last_used_at).toLocaleDateString()}`}
                    </div>
                  </div>
                </div>
                <button
                  onClick={() => handleRemovePasskey(pk.id)}
                  disabled={passkeyLoading}
                  className="btn btn-secondary text-sm py-1 px-3"
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
      {/* OAuth Credentials */}
      <div className="card p-6 mt-6">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <KeyIcon className="w-6 h-6 text-gold-400" />
            <h2 className="text-xl font-semibold text-sky-100">OAuth Credentials</h2>
          </div>
          <button
            onClick={() => setShowOAuthForm(v => !v)}
            className="btn btn-primary text-sm"
          >
            Add Credential
          </button>
        </div>

        <p className="text-navy-300 mb-4 text-sm">
          Personal OAuth tokens for platform integrations tied to your account.
        </p>

        {oauthError && (
          <div className="p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm mb-4">{oauthError}</div>
        )}
        {oauthSuccess && (
          <div className="p-3 bg-emerald-500/20 border border-emerald-500/30 rounded-lg text-emerald-300 text-sm mb-4">{oauthSuccess}</div>
        )}

        {showOAuthForm && (
          <form onSubmit={handleOAuthCreate} className="p-4 bg-navy-900 rounded-lg border border-navy-700 space-y-4 mb-4">
            <h3 className="font-medium text-sky-100">New OAuth Credential</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label htmlFor="account-settings-platform" className="block text-sm font-medium text-sky-200 mb-1">Platform</label>
                <input
                  id="account-settings-platform"
                  className="input w-full"
                  placeholder="e.g. twitch, discord"
                  value={oauthForm.platform}
                  onChange={e => setOauthForm(f => ({ ...f, platform: e.target.value }))}
                  required
                />
              </div>
              <div>
                <label htmlFor="account-settings-scopes-comma-separated" className="block text-sm font-medium text-sky-200 mb-1">Scopes (comma-separated)</label>
                <input
                  id="account-settings-scopes-comma-separated"
                  className="input w-full"
                  placeholder="e.g. read:user"
                  value={oauthForm.scopes}
                  onChange={e => setOauthForm(f => ({ ...f, scopes: e.target.value }))}
                />
              </div>
              <div>
                <label htmlFor="account-settings-client-id" className="block text-sm font-medium text-sky-200 mb-1">Client ID</label>
                <input
                  id="account-settings-client-id"
                  className="input w-full"
                  placeholder="Client ID"
                  value={oauthForm.clientId}
                  onChange={e => setOauthForm(f => ({ ...f, clientId: e.target.value }))}
                  required
                />
              </div>
              <div>
                <label htmlFor="account-settings-client-secret" className="block text-sm font-medium text-sky-200 mb-1">Client Secret</label>
                <input
                  id="account-settings-client-secret"
                  type="password"
                  className="input w-full"
                  placeholder="Client Secret"
                  value={oauthForm.clientSecret}
                  onChange={e => setOauthForm(f => ({ ...f, clientSecret: e.target.value }))}
                />
              </div>
            </div>
            <div className="flex gap-2">
              <button type="submit" className="btn btn-primary text-sm">Save</button>
              <button type="button" onClick={() => setShowOAuthForm(false)} className="btn btn-secondary text-sm">Cancel</button>
            </div>
          </form>
        )}

        {oauthLoading ? (
          <div className="flex items-center justify-center py-6">
            <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-gold-400"></div>
          </div>
        ) : oauthCredentials.length === 0 ? (
          <div className="p-4 bg-navy-900 rounded-lg border border-navy-700 text-navy-400 text-sm text-center">
            No OAuth credentials configured yet.
          </div>
        ) : (
          <div className="space-y-2">
            {oauthCredentials.map(cred => (
              <div
                key={cred.id}
                className="flex items-center justify-between p-4 bg-navy-800 rounded-lg border border-navy-700"
              >
                <div className="flex items-center gap-3">
                  <KeyIcon className="w-5 h-5 text-navy-400" />
                  <div>
                    <div className="font-medium text-sky-100 capitalize">{cred.platform}</div>
                    <div className="text-xs text-navy-400">
                      Client ID: {cred.clientId || '—'}
                      {cred.scopes?.length > 0 && ` · Scopes: ${cred.scopes.join(', ')}`}
                    </div>
                  </div>
                </div>
                <button
                  onClick={() => handleOAuthDelete(cred.id)}
                  className="btn btn-secondary text-sm py-1 px-3"
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default AccountSettings;
