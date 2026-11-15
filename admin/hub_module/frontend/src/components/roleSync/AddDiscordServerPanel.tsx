import { useState, type FormEvent } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  extractErrorInfo,
  extractErrorMessage,
  roleSyncApi,
  roleSyncKeys,
  type GuildPairing,
} from '../../services/roleSyncApi';

/** Props for {@link AddDiscordServerPanel}. */
export interface AddDiscordServerPanelProps {
  communityId: number;
  canManage: boolean;
  onPaired?: ((pairing: GuildPairing) => void) | undefined;
}

/**
 * Maps a failed install-authorize call to admin-facing text. hub-api owns
 * the decision; the webui only translates the well-known codes.
 */
function installErrorText(err: unknown): string {
  const { status, code } = extractErrorInfo(err);
  if (status === 503 || code === 'DISCORD_NOT_CONFIGURED') {
    return 'Discord bot is not configured for this tenant.';
  }
  if (status === 403) {
    return "You don't have permission to add servers to this community.";
  }
  return extractErrorMessage(err, 'Failed to start the Discord server install.');
}

/**
 * Maps a failed pair-by-ID call to admin-facing text by status/code,
 * falling back to hub-api's own message for anything unrecognised.
 */
function pairErrorText(err: unknown): string {
  const { status, code } = extractErrorInfo(err);
  if (status === 409 && code === 'DISCORD_BOT_NOT_IN_GUILD') {
    return 'The Waddles bot is not in that server. Use Add Discord server to invite it first.';
  }
  if (status === 409 && code === 'CONFLICT') {
    return 'That server is already paired with this community.';
  }
  if (status === 403) {
    return "You don't have permission to pair servers for this community.";
  }
  return extractErrorMessage(err, 'Failed to pair the Discord server.');
}

/**
 * Lets a community admin attach a Discord server live: either invite the bot
 * via Discord's OAuth install flow, or pair a server the bot is already in by
 * its ID. All validation is hub-api's; this only shapes input and shows results.
 */
export default function AddDiscordServerPanel({
  communityId,
  canManage,
  onPaired,
}: AddDiscordServerPanelProps) {
  const queryClient = useQueryClient();
  const [installSync, setInstallSync] = useState(true);
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);

  const [guildId, setGuildId] = useState('');
  const [pairSync, setPairSync] = useState(true);
  const [pairing, setPairing] = useState(false);
  const [pairError, setPairError] = useState<string | null>(null);
  const [pairSuccess, setPairSuccess] = useState<string | null>(null);

  if (!canManage) {
    return null;
  }

  /** Requests the install URL from hub-api and sends the browser to Discord. */
  async function handleInstall() {
    setInstallError(null);
    setInstalling(true);
    try {
      const url = await roleSyncApi.startServerInstall(communityId, { sync_enabled: installSync });
      console.debug('[AddDiscordServer] InstallStarted', { communityId, syncEnabled: installSync });
      window.location.assign(url);
    } catch (err) {
      console.error('[AddDiscordServer] InstallFailed', {
        communityId,
        error: extractErrorMessage(err, 'unknown error'),
      });
      setInstallError(installErrorText(err));
      setInstalling(false);
    }
  }

  /** Pairs the entered guild ID, refreshes the pairings query, and resets the form. */
  async function handlePair(e: FormEvent) {
    e.preventDefault();
    setPairError(null);
    setPairSuccess(null);
    setPairing(true);
    try {
      const created = await roleSyncApi.createPairing(communityId, {
        discord_guild_id: guildId.trim(),
        sync_enabled: pairSync,
        direction: 'discord_to_twitch',
      });
      console.debug('[AddDiscordServer] Paired', { communityId, pairingId: created.id });
      await queryClient.invalidateQueries({ queryKey: roleSyncKeys.pairings(communityId) });
      setGuildId('');
      setPairSuccess(`Discord server ${created.discord_guild_id} paired with this community.`);
      onPaired?.(created);
    } catch (err) {
      console.error('[AddDiscordServer] PairFailed', {
        communityId,
        error: extractErrorMessage(err, 'unknown error'),
      });
      setPairError(pairErrorText(err));
    } finally {
      setPairing(false);
    }
  }

  return (
    <section
      aria-label="Add a Discord server"
      className="mb-4 rounded border border-line-strong bg-surface-raised p-4"
    >
      <h2 className="mb-2 text-base font-semibold text-fg-secondary">Add a Discord server</h2>

      <div className="mb-4">
        <label className="mb-2 flex items-center gap-2 text-sm text-fg-secondary">
          <input
            type="checkbox"
            checked={installSync}
            onChange={(e) => setInstallSync(e.target.checked)}
          />
          Enable role sync for this server
        </label>
        <button
          type="button"
          onClick={() => void handleInstall()}
          disabled={installing}
          className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50"
        >
          Add Discord server
        </button>
        {installError && (
          <p role="alert" className="mt-2 text-sm text-red-400">
            {installError}
          </p>
        )}
      </div>

      <form onSubmit={(e) => void handlePair(e)} aria-label="Pair server by ID">
        <label htmlFor="pair-guild-id" className="mb-1 block text-sm font-medium text-fg-secondary">
          Discord server ID
        </label>
        <input
          id="pair-guild-id"
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          value={guildId}
          onChange={(e) => setGuildId(e.target.value)}
          placeholder="e.g. 555000111222333444"
          className="mb-2 w-full max-w-sm rounded border border-line-strong bg-surface-raised px-3 py-2 text-fg focus:outline-none focus:ring-2 focus:ring-sky-500"
        />
        <label className="mb-2 flex items-center gap-2 text-sm text-fg-secondary">
          <input
            type="checkbox"
            checked={pairSync}
            onChange={(e) => setPairSync(e.target.checked)}
          />
          Enable role sync for this server
        </label>
        <button
          type="submit"
          disabled={pairing || guildId.trim() === ''}
          className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50"
        >
          Pair server by ID
        </button>
        <p className="mt-1 text-xs text-fg-muted">
          The Waddles bot must already be in the server. Otherwise use Add Discord server.
        </p>
        {pairError && (
          <p role="alert" className="mt-2 text-sm text-red-400">
            {pairError}
          </p>
        )}
        {pairSuccess && (
          <p role="status" className="mt-2 text-sm text-emerald-400">
            {pairSuccess}
          </p>
        )}
      </form>
    </section>
  );
}
