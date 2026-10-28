import { useState, useEffect, useMemo } from 'react';
import { superAdminApi } from '../../services/api';
import {
  FolderGit2,
  Plus,
  Trash2,
  RefreshCw,
  Check,
  X,
  AlertCircle,
  GitBranch,
  Package,
  Clock,
  ExternalLink,
  Loader2,
} from 'lucide-react';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_GOLD_COLORS } from '../../theme/waddlebotTheme';

// Git provider icons
const GitHubIcon = () => (
  <svg viewBox="0 0 24 24" className="w-6 h-6" fill="currentColor">
    <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
  </svg>
);

const GitLabIcon = () => (
  <svg viewBox="0 0 24 24" className="w-6 h-6" fill="currentColor">
    <path d="M22.65 14.39L12 22.13 1.35 14.39a.84.84 0 0 1-.3-.94l1.22-3.78 2.44-7.51A.42.42 0 0 1 4.82 2a.43.43 0 0 1 .58 0 .42.42 0 0 1 .11.18l2.44 7.49h8.1l2.44-7.51A.42.42 0 0 1 18.6 2a.43.43 0 0 1 .58 0 .42.42 0 0 1 .11.18l2.44 7.51L23 13.45a.84.84 0 0 1-.35.94z"/>
  </svg>
);

const BitbucketIcon = () => (
  <svg viewBox="0 0 24 24" className="w-6 h-6" fill="currentColor">
    <path d="M.778 1.213a.768.768 0 0 0-.768.892l3.263 19.81c.084.5.515.868 1.022.873H19.95a.772.772 0 0 0 .77-.646l3.27-20.03a.768.768 0 0 0-.768-.891zM14.52 15.53H9.522L8.17 8.466h7.561z"/>
  </svg>
);

const AzureDevOpsIcon = () => (
  <svg viewBox="0 0 24 24" className="w-6 h-6" fill="currentColor">
    <path d="M0 8.877L2.247 5.91l8.405-3.416V.022l7.37 5.393L2.966 8.338v8.225L0 15.707zm24-4.45v14.651l-5.753 4.9-9.303-3.057v3.056l-5.978-7.416 15.057 1.13V5.588z"/>
  </svg>
);

const PROVIDER_CONFIG = {
  github: {
    name: 'GitHub',
    icon: GitHubIcon,
    color: 'bg-surface-raised',
    urlPlaceholder: 'https://github.com/owner/repo',
    authFields: [
      { key: 'access_token', label: 'Personal Access Token (PAT)', secret: true, placeholder: 'ghp_xxxxxxxxxxxx', required: false },
    ],
    description: 'Connect to public or private GitHub repositories',
  },
  gitlab: {
    name: 'GitLab',
    icon: GitLabIcon,
    color: 'bg-orange-600',
    urlPlaceholder: 'https://gitlab.com/owner/repo',
    authFields: [
      { key: 'access_token', label: 'Access Token', secret: true, placeholder: 'glpat-xxxxxxxxxxxx', required: false },
    ],
    description: 'Connect to GitLab.com or self-hosted GitLab instances',
  },
  bitbucket: {
    name: 'Bitbucket',
    icon: BitbucketIcon,
    color: 'bg-blue-600',
    urlPlaceholder: 'https://bitbucket.org/owner/repo',
    authFields: [
      { key: 'username', label: 'Username', secret: false, placeholder: 'Your Bitbucket username', required: false },
      { key: 'app_password', label: 'App Password', secret: true, placeholder: 'App password with repo access', required: false },
    ],
    description: 'Connect to Bitbucket Cloud or Server repositories',
  },
  azure: {
    name: 'Azure DevOps',
    icon: AzureDevOpsIcon,
    color: 'bg-sky-600',
    urlPlaceholder: 'https://dev.azure.com/org/project/_git/repo',
    authFields: [
      { key: 'access_token', label: 'Personal Access Token', secret: true, placeholder: 'Azure DevOps PAT', required: false },
    ],
    description: 'Connect to Azure DevOps Git repositories',
  },
};

export default function SuperAdminSoftwareDiscovery() {
  const [repositories, setRepositories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState({});
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [showAddModal, setShowAddModal] = useState(false);

  // Provider options for select field
  const providerOptions = useMemo(() => [
    { value: 'github', label: 'GitHub' },
    { value: 'gitlab', label: 'GitLab' },
    { value: 'bitbucket', label: 'Bitbucket' },
    { value: 'azure', label: 'Azure DevOps' },
  ], []);

  // Scan interval options
  const scanIntervalOptions = useMemo(() => [
    { value: '1', label: 'Every hour' },
    { value: '6', label: 'Every 6 hours' },
    { value: '12', label: 'Every 12 hours' },
    { value: '24', label: 'Daily' },
    { value: '168', label: 'Weekly' },
  ], []);

  // Field definitions for add repository modal
  const addRepositoryFields = useMemo(() => [
    {
      name: 'provider',
      type: 'select',
      label: 'Git Provider',
      required: true,
      defaultValue: 'github',
      options: providerOptions,
      helpText: 'Select the git hosting provider for your repository',
    },
    {
      name: 'url',
      type: 'url',
      label: 'Repository URL',
      required: true,
      placeholder: 'https://github.com/owner/repo',
      helpText: 'The full URL to your git repository',
    },
    {
      name: 'name',
      type: 'text',
      label: 'Display Name',
      placeholder: 'Auto-detected from URL',
      helpText: 'Optional friendly name for the repository',
    },
    {
      name: 'branch',
      type: 'text',
      label: 'Default Branch',
      defaultValue: 'main',
      placeholder: 'main',
      helpText: 'The branch to scan for dependencies',
    },
    // GitHub authentication
    {
      name: 'github_access_token',
      type: 'password',
      label: 'Personal Access Token (PAT)',
      placeholder: 'ghp_xxxxxxxxxxxx',
      helpText: 'Required for private repositories. Leave empty for public repos.',
      showWhen: (values) => values.provider === 'github',
      tab: 'Authentication',
    },
    // GitLab authentication
    {
      name: 'gitlab_access_token',
      type: 'password',
      label: 'Access Token',
      placeholder: 'glpat-xxxxxxxxxxxx',
      helpText: 'Required for private repositories. Leave empty for public repos.',
      showWhen: (values) => values.provider === 'gitlab',
      tab: 'Authentication',
    },
    // Bitbucket authentication
    {
      name: 'bitbucket_username',
      type: 'text',
      label: 'Username',
      placeholder: 'Your Bitbucket username',
      helpText: 'Your Bitbucket account username',
      showWhen: (values) => values.provider === 'bitbucket',
      tab: 'Authentication',
    },
    {
      name: 'bitbucket_app_password',
      type: 'password',
      label: 'App Password',
      placeholder: 'App password with repo access',
      helpText: 'Create an app password in Bitbucket settings with repository read access',
      showWhen: (values) => values.provider === 'bitbucket',
      tab: 'Authentication',
    },
    // Azure DevOps authentication
    {
      name: 'azure_access_token',
      type: 'password',
      label: 'Personal Access Token',
      placeholder: 'Azure DevOps PAT',
      helpText: 'Required for private repositories. Leave empty for public repos.',
      showWhen: (values) => values.provider === 'azure',
      tab: 'Authentication',
    },
    // Auto-scan options
    {
      name: 'auto_scan',
      type: 'checkbox',
      label: 'Auto-scan for dependencies',
      defaultValue: true,
      helpText: 'Automatically scan for package.json, requirements.txt, go.mod, and other dependency files',
      tab: 'Scan Settings',
    },
    {
      name: 'scan_interval_hours',
      type: 'select',
      label: 'Scan Interval',
      defaultValue: '24',
      options: scanIntervalOptions,
      helpText: 'How often to automatically scan for dependency changes',
      showWhen: (values) => values.auto_scan === true,
      tab: 'Scan Settings',
    },
  ], [providerOptions, scanIntervalOptions]);

  useEffect(() => {
    loadRepositories();
  }, []);

  const loadRepositories = async () => {
    try {
      setLoading(true);
      const response = await superAdminApi.getSoftwareRepositories();
      setRepositories(response.data.repositories || []);
    } catch (err) {
      setError(err.response?.data?.message || 'Failed to load repositories');
    } finally {
      setLoading(false);
    }
  };

  const extractRepoName = (url) => {
    try {
      const parts = url.replace(/\.git$/, '').split('/');
      return parts[parts.length - 1] || 'repository';
    } catch {
      return 'repository';
    }
  };

  const handleAddRepository = async (data) => {
    try {
      setError(null);

      // Build auth object based on provider
      let auth = null;
      if (data.provider === 'github' && data.github_access_token) {
        auth = { access_token: data.github_access_token };
      } else if (data.provider === 'gitlab' && data.gitlab_access_token) {
        auth = { access_token: data.gitlab_access_token };
      } else if (data.provider === 'bitbucket' && (data.bitbucket_username || data.bitbucket_app_password)) {
        auth = { username: data.bitbucket_username, app_password: data.bitbucket_app_password };
      } else if (data.provider === 'azure' && data.azure_access_token) {
        auth = { access_token: data.azure_access_token };
      }

      const payload = {
        provider: data.provider,
        url: data.url.trim(),
        name: data.name?.trim() || extractRepoName(data.url),
        branch: data.branch?.trim() || 'main',
        auto_scan: data.auto_scan,
        scan_interval_hours: parseInt(data.scan_interval_hours) || 24,
        auth,
      };

      await superAdminApi.addSoftwareRepository(payload);
      setSuccess('Repository added successfully');
      setShowAddModal(false);
      loadRepositories();

      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.message || 'Failed to add repository');
      throw err; // Re-throw to prevent modal from closing
    }
  };

  const handleScanRepository = async (repoId) => {
    try {
      setScanning(prev => ({ ...prev, [repoId]: true }));
      setError(null);

      await superAdminApi.scanSoftwareRepository(repoId);
      setSuccess('Repository scan initiated');
      loadRepositories();

      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.message || 'Failed to scan repository');
    } finally {
      setScanning(prev => ({ ...prev, [repoId]: false }));
    }
  };

  const handleDeleteRepository = async (repoId) => {
    if (!confirm('Are you sure you want to remove this repository? This will delete all discovered dependencies.')) {
      return;
    }

    try {
      setError(null);
      await superAdminApi.deleteSoftwareRepository(repoId);
      setSuccess('Repository removed successfully');
      loadRepositories();

      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.message || 'Failed to remove repository');
    }
  };

  const getStatusBadge = (status) => {
    const badges = {
      active: { bg: 'bg-emerald-500/20', text: 'text-emerald-400', border: 'border-emerald-500/30', label: 'Active' },
      scanning: { bg: 'bg-blue-500/20', text: 'text-blue-400', border: 'border-blue-500/30', label: 'Scanning' },
      error: { bg: 'bg-red-500/20', text: 'text-red-400', border: 'border-red-500/30', label: 'Error' },
      pending: { bg: 'bg-yellow-500/20', text: 'text-yellow-400', border: 'border-yellow-500/30', label: 'Pending' },
    };
    const badge = badges[status] || badges.pending;
    return (
      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${badge.bg} ${badge.text} border ${badge.border}`}>
        {badge.label}
      </span>
    );
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-gold-400" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-sky-100 flex items-center gap-3">
            <FolderGit2 className="w-8 h-8 text-gold-400" />
            Software & Repository Discovery
          </h1>
          <p className="text-navy-400 mt-1">
            Connect git repositories to automatically discover and track dependencies
          </p>
        </div>
        <button
          onClick={() => setShowAddModal(true)}
          className="btn btn-primary flex items-center gap-2"
        >
          <Plus className="w-5 h-5" />
          Add Repository
        </button>
      </div>

      {/* Alerts */}
      {error && (
        <div className="bg-red-500/20 border border-red-500/30 rounded-lg p-4 flex items-start gap-3">
          <AlertCircle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="text-red-300">{error}</p>
          </div>
          <button onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            <X className="w-5 h-5" />
          </button>
        </div>
      )}

      {success && (
        <div className="bg-emerald-500/20 border border-emerald-500/30 rounded-lg p-4 flex items-start gap-3">
          <Check className="w-5 h-5 text-emerald-400 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="text-emerald-300">{success}</p>
          </div>
          <button onClick={() => setSuccess(null)} className="text-emerald-400 hover:text-emerald-300">
            <X className="w-5 h-5" />
          </button>
        </div>
      )}

      {/* Repository List */}
      {repositories.length === 0 ? (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <FolderGit2 className="w-16 h-16 text-navy-600 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-sky-100 mb-2">No repositories connected</h3>
          <p className="text-navy-400 mb-6">
            Add your first git repository to start discovering dependencies automatically.
          </p>
          <button
            onClick={() => setShowAddModal(true)}
            className="btn btn-primary inline-flex items-center gap-2"
          >
            <Plus className="w-5 h-5" />
            Add Repository
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4">
          {repositories.map((repo) => {
            const providerConfig = PROVIDER_CONFIG[repo.provider] || PROVIDER_CONFIG.github;
            const IconComponent = providerConfig.icon;

            return (
              <div
                key={repo.id}
                className="bg-navy-800 border border-navy-700 rounded-lg p-6 hover:border-navy-600 transition-colors"
              >
                <div className="flex items-start justify-between">
                  <div className="flex items-start gap-4">
                    <div className={`p-3 rounded-lg ${providerConfig.color}`}>
                      <IconComponent />
                    </div>
                    <div>
                      <div className="flex items-center gap-3">
                        <h3 className="text-lg font-semibold text-sky-100">{repo.name}</h3>
                        {getStatusBadge(repo.status)}
                      </div>
                      <a
                        href={repo.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-navy-400 hover:text-sky-300 text-sm flex items-center gap-1 mt-1"
                      >
                        {repo.url}
                        <ExternalLink className="w-3 h-3" />
                      </a>
                      <div className="flex items-center gap-4 mt-3 text-sm text-navy-400">
                        <span className="flex items-center gap-1">
                          <GitBranch className="w-4 h-4" />
                          {repo.branch || 'main'}
                        </span>
                        {repo.dependency_count > 0 && (
                          <span className="flex items-center gap-1">
                            <Package className="w-4 h-4" />
                            {repo.dependency_count} dependencies
                          </span>
                        )}
                        {repo.last_scanned && (
                          <span className="flex items-center gap-1">
                            <Clock className="w-4 h-4" />
                            Last scanned: {new Date(repo.last_scanned).toLocaleDateString()}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => handleScanRepository(repo.id)}
                      disabled={scanning[repo.id]}
                      className="p-2 text-navy-400 hover:text-sky-300 hover:bg-navy-700 rounded-lg transition-colors disabled:opacity-50"
                      title="Scan repository"
                    >
                      {scanning[repo.id] ? (
                        <Loader2 className="w-5 h-5 animate-spin" />
                      ) : (
                        <RefreshCw className="w-5 h-5" />
                      )}
                    </button>
                    <button
                      onClick={() => handleDeleteRepository(repo.id)}
                      className="p-2 text-navy-400 hover:text-red-400 hover:bg-navy-700 rounded-lg transition-colors"
                      title="Remove repository"
                    >
                      <Trash2 className="w-5 h-5" />
                    </button>
                  </div>
                </div>

                {/* Dependency Summary */}
                {repo.dependencies && repo.dependencies.length > 0 && (
                  <div className="mt-4 pt-4 border-t border-navy-700">
                    <h4 className="text-sm font-medium text-navy-300 mb-2">Discovered Dependencies</h4>
                    <div className="flex flex-wrap gap-2">
                      {repo.dependencies.slice(0, 10).map((dep, idx) => (
                        <span
                          key={idx}
                          className="px-2 py-1 bg-navy-700 rounded text-xs text-sky-300"
                        >
                          {dep.name}@{dep.version}
                        </span>
                      ))}
                      {repo.dependencies.length > 10 && (
                        <span className="px-2 py-1 bg-navy-700 rounded text-xs text-navy-400">
                          +{repo.dependencies.length - 10} more
                        </span>
                      )}
                    </div>
                  </div>
                )}

                {/* Error Message */}
                {repo.status === 'error' && repo.error_message && (
                  <div className="mt-4 pt-4 border-t border-navy-700">
                    <div className="flex items-start gap-2 text-sm text-red-400">
                      <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                      <span>{repo.error_message}</span>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Add Repository Modal */}
      <FormModalBuilder
        title="Add Git Repository"
        fields={addRepositoryFields}
        isOpen={showAddModal}
        onClose={() => setShowAddModal(false)}
        onSubmit={handleAddRepository}
        submitButtonText="Add Repository"
        cancelButtonText="Cancel"
        width="lg"
        themeMode="dark"
        colors={WADDLES_GOLD_COLORS}
      />

      {/* Info Section */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <h3 className="text-lg font-semibold text-sky-100 mb-4">About Software Discovery</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 text-sm text-navy-300">
          <div>
            <h4 className="font-medium text-sky-100 mb-2">Supported Dependency Files</h4>
            <ul className="list-disc list-inside space-y-1">
              <li>package.json (Node.js/npm)</li>
              <li>requirements.txt (Python/pip)</li>
              <li>go.mod (Go modules)</li>
              <li>Cargo.toml (Rust)</li>
              <li>pom.xml (Java/Maven)</li>
              <li>composer.json (PHP)</li>
            </ul>
          </div>
          <div>
            <h4 className="font-medium text-sky-100 mb-2">Security</h4>
            <ul className="list-disc list-inside space-y-1">
              <li>Access tokens are stored encrypted</li>
              <li>Only read-only access is required</li>
              <li>Tokens can be revoked at any time</li>
              <li>Scans use short-lived credentials</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
