import { useQuery } from '@tanstack/react-query';
import { ArrowTopRightOnSquareIcon } from '@heroicons/react/24/outline';
import { useFeatureFlag } from '../../lib/useFeatureFlag';
import { discordBotInstallApi, isProviderNotConfigured } from '../../services/discordBotInstallApi';

/**
 * "Add Waddles to Discord" bot-install card -- the install link Discord
 * requires every listed app provider to expose (separate from the
 * `identify guilds` OAuth connection card below it on this same page,
 * which links an admin's own Discord *account*, not the bot, to a
 * server). Renders a plain external link to `hub_api/blueprints/v1/
 * discord_bot_install.py`'s resolved URL -- no popup/`postMessage`
 * handshake like `CommunityConnections.jsx`'s OAuth connect flow needs,
 * because adding a bot to a guild is a one-shot Discord-hosted consent
 * screen with no authorization code for this app to exchange.
 *
 * Gated behind `waddles.webui.discord_bot_install_link` (defaults OFF
 * until added to hub-api's `CLIENT_FLAG_KEYS` -- see PR description).
 */
export default function DiscordBotInstallLink() {
  const enabled = useFeatureFlag('waddles.webui.discord_bot_install_link');

  const {
    data: installUrl,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['discord-bot-install-url'],
    queryFn: discordBotInstallApi.getInstallUrl,
    enabled,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  if (!enabled) {
    return null;
  }

  if (error) {
    const notConfigured = isProviderNotConfigured(error);
    console.error('[DiscordBotInstallLink] Load failed', { notConfigured });
    return (
      <div
        data-testid="discord-bot-install-error"
        className="bg-navy-800 border border-navy-700 rounded-lg p-6"
      >
        <h3 className="font-semibold text-sky-100 mb-1">Add Waddles to Discord</h3>
        <p className="text-sm text-yellow-300">
          {notConfigured
            ? 'Ask the platform admin to configure the Discord application before the bot can be installed.'
            : 'Failed to load the Discord install link. Try refreshing the page.'}
        </p>
      </div>
    );
  }

  return (
    <div
      data-testid="discord-bot-install-card"
      className="bg-navy-800 border border-navy-700 rounded-lg p-6"
    >
      <h3 className="font-semibold text-sky-100 mb-1">Add Waddles to Discord</h3>
      <p className="text-sm text-navy-400 mb-4">
        Invite the Waddles bot to a Discord server to enable commands, role sync, and event sync.
        Requires the &quot;Manage Server&quot; permission on the target server.
      </p>
      <a
        href={installUrl}
        target="_blank"
        rel="noopener noreferrer"
        aria-label="Add Waddles to Discord"
        data-testid="discord-bot-install-link"
        aria-disabled={isLoading || !installUrl}
        className={`btn btn-primary inline-flex items-center gap-2 text-sm ${
          isLoading || !installUrl ? 'pointer-events-none opacity-50' : ''
        }`}
      >
        <ArrowTopRightOnSquareIcon className="w-4 h-4" />
        {isLoading ? 'Loading…' : 'Add Waddles to Discord'}
      </a>
    </div>
  );
}
