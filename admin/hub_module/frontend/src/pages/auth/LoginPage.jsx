import { useEffect } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { LoginPageBuilder } from '@penguintechinc/react-libs';
import SocialLoginButtons from './SocialLoginButtons';
import { useAuth } from '../../contexts/AuthContext';
import { KeyIcon } from '@heroicons/react/24/outline';
import { passkeyApi, publicApi, tenantApi } from '../../services/api';
import { resolveBranding } from './tenantBranding';
import { useState } from 'react';

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
          <SocialLoginButtons />
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
