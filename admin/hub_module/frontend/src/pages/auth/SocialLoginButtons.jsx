import { useState } from 'react';
import { oauthApi } from '../../services/api';

/**
 * Server-side OAuth social login buttons. hub-api owns the real client_id and
 * redirect URI: a click asks `GET /api/v1/auth/oauth/<platform>` for the
 * provider authorize URL and navigates the browser there. The frontend never
 * builds a provider URL or sends a client_id. Only platforms hub-api supports
 * (`oauth_service.VALID_PLATFORMS`) are listed -- no dead buttons.
 */
const GOOGLE_ICON = (
    <svg className="w-5 h-5" viewBox="0 0 24 24">
      <path fill="#4285F4" d="M23.49 12.27c0-.79-.07-1.54-.19-2.27H12v4.51h6.47a5.54 5.54 0 0 1-2.4 3.63v3h3.88c2.27-2.09 3.54-5.17 3.54-8.87z"/>
      <path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3c-1.08.72-2.45 1.16-4.05 1.16-3.12 0-5.77-2.11-6.71-4.95H1.29v3.09A12 12 0 0 0 12 24z"/>
      <path fill="#FBBC05" d="M5.29 14.3a7.2 7.2 0 0 1 0-4.6V6.61H1.29a12 12 0 0 0 0 10.78l4-3.09z"/>
      <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0A12 12 0 0 0 1.29 6.61l4 3.09C6.23 6.86 8.88 4.75 12 4.75z"/>
    </svg>
);

const SLACK_ICON = (
    <svg className="w-5 h-5" viewBox="0 0 24 24" fill="#36C5F0">
      <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/>
    </svg>
);

const DISCORD_ICON = (
  <svg className="w-5 h-5" viewBox="0 0 24 24" fill="#5865F2" aria-hidden="true">
    <path d="M20.317 4.37a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.74 19.74 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.1 13.1 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.292a.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.1.246.198.373.292a.077.077 0 0 1-.006.127 12.3 12.3 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.095 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.095 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/>
  </svg>
);

const TWITCH_ICON = (
  <svg className="w-5 h-5" viewBox="0 0 24 24" fill="#9146FF" aria-hidden="true">
    <path d="M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714z"/>
  </svg>
);

const SOCIAL_PROVIDERS = [
  { platform: 'discord', label: 'Continue with Discord', icon: DISCORD_ICON },
  { platform: 'twitch', label: 'Continue with Twitch', icon: TWITCH_ICON },
  { platform: 'google', label: 'Sign in with Google', icon: GOOGLE_ICON },
  { platform: 'slack', label: 'Continue with Slack', icon: SLACK_ICON },
];

/**
 * Renders one button per supported OAuth platform; on click redirects the
 * browser to the authorize URL returned by hub-api.
 */
function SocialLoginButtons() {
  const [pending, setPending] = useState(null);
  const [error, setError] = useState('');

  const start = async (platform) => {
    setPending(platform);
    setError('');
    try {
      const { data } = await oauthApi.start(platform);
      if (!data?.authorizeUrl) {
        throw new Error('missing authorizeUrl');
      }
      window.location.assign(data.authorizeUrl);
    } catch (err) {
      console.error('[SocialLoginButtons] oauth start failed', { platform, message: err?.message });
      setError('Could not start sign-in. Please try again.');
      setPending(null);
    }
  };

  return (
    <div className="mb-4 space-y-2" data-testid="social-logins">
      {error && (
        <div role="alert" className="p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm">
          {error}
        </div>
      )}
      {SOCIAL_PROVIDERS.map(({ platform, label, icon }) => (
        <button
          key={platform}
          type="button"
          data-testid={`oauth-${platform}`}
          onClick={() => start(platform)}
          disabled={pending !== null}
          className="w-full flex items-center justify-center space-x-3 px-4 py-3 bg-slate-800 border border-navy-600 rounded-lg hover:bg-navy-700 hover:border-gold-500 transition-all disabled:opacity-50 focus:ring-2 focus:ring-sky-500"
        >
          {icon}
          <span className="font-medium text-sky-50">{pending === platform ? 'Redirecting...' : label}</span>
        </button>
      ))}
    </div>
  );
}

export default SocialLoginButtons;
