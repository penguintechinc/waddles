/**
 * Chat Message Component
 * Individual chat message with avatar, username, and timestamp
 */
import { useAuth } from '../../contexts/AuthContext';
import { getPlatformConfig } from '../../utils/platformConfig';

// Build platform badges from shared config, mapping to the shape used below
const buildBadge = (platform) => {
  const cfg = getPlatformConfig(platform);
  // Extract just the first bg- class for the solid badge background
  const bgClass = cfg.color.split(' ').find((c) => c.startsWith('bg-'))?.replace('/20', '') || 'bg-navy-700';
  return { emoji: cfg.icon, color: bgClass, name: cfg.label };
};
const PLATFORM_BADGES = {
  discord: buildBadge('discord'),
  twitch: buildBadge('twitch'),
  slack: buildBadge('slack'),
  youtube: buildBadge('youtube'),
  kick: buildBadge('kick'),
  telegram: buildBadge('telegram'),
  matrix: buildBadge('matrix'),
  guilded: buildBadge('guilded'),
  revolt: buildBadge('revolt'),
  hub: buildBadge('hub'),
};

export default function ChatMessage({ message }) {
  const { user } = useAuth();
  const isOwnMessage = user && message.senderId === user.id;
  const platform = message.senderPlatform || 'hub';
  const badge = PLATFORM_BADGES[platform] || PLATFORM_BADGES.hub;

  // Format timestamp
  const formatTime = (timestamp) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diff = now - date;

    // Less than 1 minute
    if (diff < 60000) return 'just now';

    // Less than 1 hour
    if (diff < 3600000) {
      const mins = Math.floor(diff / 60000);
      return `${mins}m ago`;
    }

    // Less than 1 day
    if (diff < 86400000) {
      const hours = Math.floor(diff / 3600000);
      return `${hours}h ago`;
    }

    // Show date
    return date.toLocaleDateString();
  };

  return (
    <div className={`flex gap-3 p-3 hover:bg-surface-raised/50 transition-colors ${isOwnMessage ? 'bg-surface-raised/30' : ''}`}>
      {/* Avatar */}
      <div className="flex-shrink-0">
        {message.senderAvatarUrl ? (
          <img
            src={message.senderAvatarUrl}
            alt={message.senderUsername}
            className="w-10 h-10 rounded-full"
          />
        ) : (
          <div className="w-10 h-10 rounded-full bg-navy-700 flex items-center justify-center text-fg-secondary font-semibold">
            {message.senderUsername?.[0]?.toUpperCase() || '?'}
          </div>
        )}
      </div>

      {/* Message content */}
      <div className="flex-1 min-w-0">
        {/* Header: Username + Platform + Timestamp */}
        <div className="flex items-center gap-2 mb-1">
          <span className={`font-semibold ${isOwnMessage ? 'text-blue-400' : 'text-fg'}`}>
            {message.senderUsername}
          </span>

          {/* Platform badge */}
          <span
            className={`${badge.color} text-white text-xs px-1.5 py-0.5 rounded font-medium`}
            title={badge.name}
          >
            {badge.emoji}
          </span>

          {/* Timestamp */}
          <span className="text-xs text-fg-muted" title={new Date(message.createdAt).toLocaleString()}>
            {formatTime(message.createdAt)}
          </span>
        </div>

        {/* Message text */}
        <div className="text-fg-secondary break-words whitespace-pre-wrap">
          {message.content}
        </div>
      </div>
    </div>
  );
}
