import { useEffect } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { LoginPageBuilder } from '@penguintechinc/react-libs';
import { useAuth } from '../../contexts/AuthContext';
import { KeyIcon } from '@heroicons/react/24/outline';
import { passkeyApi, publicApi, tenantApi } from '../../services/api';
import { resolveBranding } from './tenantBranding';
import { useState } from 'react';

// Social login providers — all route through the backend OAuth proxy.
// The LoginPageBuilder appends client_id/state params to the authUrl,
// but the backend ignores them and generates its own state UUID.
const SOCIAL_PROVIDERS = [
  { provider: 'discord', clientId: 'server-side', authUrl: '/api/v1/auth/oauth/discord' },
  { provider: 'twitch',  clientId: 'server-side', authUrl: '/api/v1/auth/oauth/twitch' },
  {
    provider: 'oauth2',
    clientId: 'server-side',
    authUrl: '/api/v1/auth/oauth/slack',
    label: 'Continue with Slack',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="#36C5F0">
        <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/>
      </svg>
    ),
    buttonColor: '#1e293b',
    textColor: '#f0f9ff',
  },
  {
    provider: 'oauth2',
    clientId: 'server-side',
    authUrl: '/api/v1/auth/oauth/youtube',
    label: 'Continue with YouTube',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="#FF0000">
        <path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/>
      </svg>
    ),
    buttonColor: '#1e293b',
    textColor: '#f0f9ff',
  },
  {
    provider: 'oauth2',
    clientId: 'server-side',
    authUrl: '/api/v1/auth/oauth/kick',
    label: 'Continue with KICK',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="#53FC18">
        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm-1-13h2v6h-2zm0 8h2v2h-2z"/>
      </svg>
    ),
    buttonColor: '#1e293b',
    textColor: '#f0f9ff',
  },
];

// Waddlebot color overrides — match the existing navy/gold theme palette
const WADDLEBOT_COLORS = {
  pageBackground: 'bg-transparent',
  cardBackground: 'bg-navy-800',
  cardBorder: 'border-navy-600',
  titleText: 'text-gold-400',
  subtitleText: 'text-navy-300',
  labelText: 'text-sky-200',
  inputBackground: 'bg-navy-900',
  inputBorder: 'border-navy-600',
  inputFocusBorder: 'border-sky-400',
  inputText: 'text-sky-100',
  primaryButton: 'bg-sky-600 hover:bg-sky-500',
  primaryButtonText: 'text-white',
  linkText: 'text-sky-400',
  linkHoverText: 'text-sky-300',
};

function PasskeyButton({ onLogin }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handlePasskey = async () => {
    setLoading(true);
    setError('');
    try {
      const optRes = await passkeyApi.startLogin({});
      const options = optRes.data;
      const credential = await navigator.credentials.get({
        publicKey: {
          ...options,
          challenge: Uint8Array.from(
            atob(options.challenge.replace(/-/g, '+').replace(/_/g, '/')),
            c => c.charCodeAt(0)
          ),
          allowCredentials: (options.allowCredentials || []).map(ac => ({
            ...ac,
            id: Uint8Array.from(
              atob(ac.id.replace(/-/g, '+').replace(/_/g, '/')),
              c => c.charCodeAt(0)
            ),
          })),
        },
      });
      const encoded = {
        id: credential.id,
        rawId: btoa(String.fromCharCode(...new Uint8Array(credential.rawId))),
        type: credential.type,
        response: {
          authenticatorData: btoa(String.fromCharCode(...new Uint8Array(credential.response.authenticatorData))),
          clientDataJSON: btoa(String.fromCharCode(...new Uint8Array(credential.response.clientDataJSON))),
          signature: btoa(String.fromCharCode(...new Uint8Array(credential.response.signature))),
          userHandle: credential.response.userHandle
            ? btoa(String.fromCharCode(...new Uint8Array(credential.response.userHandle)))
            : null,
        },
      };
      const loginRes = await passkeyApi.finishLogin(encoded);
      onLogin(loginRes.data.token);
    } catch (err) {
      const apiErr = err?.response?.data?.error;
      setError(
        typeof apiErr === 'string' ? apiErr :
        apiErr?.message || err?.message || 'Passkey login failed.'
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mb-4">
      {error && (
        <div className="mb-2 p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm">
          {error}
        </div>
      )}
      <button
        onClick={handlePasskey}
        disabled={loading}
        className="w-full flex items-center justify-center space-x-3 px-4 py-3 bg-navy-800 border border-navy-600 rounded-lg hover:bg-navy-700 hover:border-gold-500 transition-all disabled:opacity-50"
      >
        <KeyIcon className="w-5 h-5 text-gold-400" />
        <span className="font-medium text-sky-100">
          {loading ? 'Waiting for passkey...' : 'Sign in with Passkey'}
        </span>
      </button>
    </div>
  );
}

function LoginPage() {
  const navigate = useNavigate();
  const { tenantSlug } = useParams();
  const { refreshUser, isAuthenticated } = useAuth();
  const [signupEnabled, setSignupEnabled] = useState(false);
  const [tenantInfo, setTenantInfo] = useState(null);

  // Tenant branding (whitelabel). Fails soft -- any error keeps stock branding.
  useEffect(() => {
    if (!tenantSlug) {
      setTenantInfo(null);
      return undefined;
    }
    let cancelled = false;
    tenantApi.getLoginInfo(tenantSlug)
      .then(({ data }) => {
        if (!cancelled) setTenantInfo(data?.tenant ?? null);
      })
      .catch(() => {
        if (!cancelled) setTenantInfo(null);
      });
    return () => {
      cancelled = true;
    };
  }, [tenantSlug]);

  useEffect(() => {
    publicApi.getSignupSettings()
      .then(({ data }) => setSignupEnabled(data.signupEnabled === true))
      .catch(() => setSignupEnabled(false));
  }, []);

  useEffect(() => {
    if (isAuthenticated) {
      navigate('/dashboard');
    }
  }, [isAuthenticated, navigate]);

  // LoginPageBuilder's onSuccess hands back the parsed login response
  // ({success, token, user}) directly -- it already completed the
  // email/password (or MFA) POST itself. hub-api's own response already
  // set the HttpOnly session cookie (security.md C4 -- the JWT is never
  // persisted client-side), so there's nothing to store here; just refresh
  // user state rather than routing it through handleOAuthCallback (which
  // POSTs to /api/v1/auth/exchange expecting a short-lived OAuth code and
  // would reject a JWT, silently leaving the user unauthenticated).
  const handleSuccess = async (response) => {
    if (response.token) {
      await refreshUser(true);
    }
    navigate('/dashboard');
  };

  const handlePasskeyLogin = async (token) => {
    if (token) {
      await refreshUser(true);
    }
    navigate('/dashboard');
  };

  return (
    <div className="min-h-[80vh] flex items-center justify-center py-12 px-4">
      <div className="max-w-md w-full">
        {/* Passkey login — shown above the OAuth/email sections */}
        <div className="bg-navy-800 border border-navy-600 border-b-0 rounded-t-xl px-6 pt-6 pb-4">
          <PasskeyButton onLogin={handlePasskeyLogin} />
          <div className="relative mt-2">
            <div className="absolute inset-0 flex items-center">
              <div className="w-full border-t border-navy-600" />
            </div>
            <div className="relative flex justify-center text-sm">
              <span className="px-4 bg-navy-800 text-navy-400">or continue with platform or email</span>
            </div>
          </div>
        </div>

        {/* LoginPageBuilder: OAuth social logins, email/password, tenant field, GDPR */}
        <LoginPageBuilder
          api={{ loginUrl: '/api/v1/auth/login' }}
          branding={{
            ...resolveBranding(tenantInfo, tenantSlug),
            logoHeight: 72,
            githubRepo: 'penguintechinc/waddlebot',
          }}
          onSuccess={handleSuccess}
          transformErrorMessage={(err) => {
            if (typeof err === 'string') return err;
            if (err?.message) return err.message;
            return 'Login failed. Please try again.';
          }}
          socialLogins={SOCIAL_PROVIDERS}
          tenantField={{
            show: true,
            label: 'Tenant (optional)',
            placeholder: 'your-org-slug',
            helpText: 'Enter your organization slug, or leave blank for personal login',
            defaultValue: tenantSlug || '',
          }}
          gdpr={{
            enabled: true,
            privacyPolicyUrl: '/privacy',
          }}
          showSignUp={false}
          className="!rounded-t-none !border-t-0 !mt-0"
          colors={WADDLEBOT_COLORS}
        />
        {signupEnabled && (
          <p className="mt-4 text-center text-navy-400">
            Don&apos;t have an account?{' '}
            <Link
              to="/register"
              className="text-sky-400 hover:text-sky-300 font-medium"
              data-testid="register-link"
            >
              Create one
            </Link>
          </p>
        )}
      </div>
    </div>
  );
}

export default LoginPage;
