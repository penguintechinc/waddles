import { useState } from 'react';
import { HashtagIcon, ChatBubbleLeftRightIcon, SpeakerWaveIcon, PlusIcon, XMarkIcon } from '@heroicons/react/24/outline';

const CHANNEL_GROUPS = [
  { label: 'Chat Channels', type: 'chat', icon: HashtagIcon },
  { label: 'Forums', type: 'forum', icon: ChatBubbleLeftRightIcon },
  { label: 'Voice / Video', type: 'voice', icon: SpeakerWaveIcon },
];

const CHANNEL_TYPES = [
  { value: 'chat', label: 'Chat' },
  { value: 'forum', label: 'Forum' },
  { value: 'voice', label: 'Voice' },
];

export default function ChannelSidebar({
  channels = [],
  activeChannelId,
  onSelectChannel,
  canCreateChannel = false,
  onCreateChannel,
}) {
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newType, setNewType] = useState('chat');
  const [creating, setCreating] = useState(false);

  async function handleCreate(e) {
    e.preventDefault();
    if (!newName.trim() || !onCreateChannel) return;
    setCreating(true);
    try {
      await onCreateChannel({ name: newName.trim(), channel_type: newType });
      setNewName('');
      setNewType('chat');
      setShowCreate(false);
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="flex flex-col w-60 h-full bg-navy-900 border-r border-navy-700 overflow-y-auto">
      {canCreateChannel && (
        <div className="px-2 pt-3">
          {showCreate ? (
            <form onSubmit={handleCreate} className="bg-navy-800 border border-navy-600 rounded p-2 space-y-2">
              <input
                aria-label="Channel name"
                data-testid="channel-name-input"
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="Channel name"
                required
                autoFocus
                className="w-full bg-navy-900 border border-navy-600 text-sky-100 rounded px-2 py-1 text-sm focus:border-sky-500"
              />
              <select
                aria-label="Channel type"
                data-testid="channel-type-select"
                value={newType}
                onChange={(e) => setNewType(e.target.value)}
                className="w-full bg-navy-900 border border-navy-600 text-sky-100 rounded px-2 py-1 text-sm focus:border-sky-500"
              >
                {CHANNEL_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
              <div className="flex gap-1">
                <button
                  data-testid="create-channel-submit"
                  type="submit"
                  disabled={creating || !newName.trim()}
                  className="flex-1 bg-gold-500 text-navy-900 hover:bg-gold-400 rounded px-2 py-1 text-xs font-medium disabled:opacity-50 transition-colors"
                >
                  {creating ? 'Creating…' : 'Create'}
                </button>
                <button
                  aria-label="Close"
                  type="button"
                  onClick={() => setShowCreate(false)}
                  className="p-1 text-navy-400 hover:text-sky-300 rounded transition-colors"
                >
                  <XMarkIcon className="h-4 w-4" />
                </button>
              </div>
            </form>
          ) : (
            <button
              data-testid="add-channel-btn"
              type="button"
              onClick={() => setShowCreate(true)}
              className="w-full flex items-center gap-1.5 px-2 py-1.5 rounded text-sm text-navy-300 hover:bg-navy-800 hover:text-gold-400 transition-colors"
            >
              <PlusIcon className="h-4 w-4" />
              <span>New Channel</span>
            </button>
          )}
        </div>
      )}

      {CHANNEL_GROUPS.map(({ label, type, icon: Icon }) => {
        const group = channels.filter((ch) => ch.channelType === type);
        if (group.length === 0) return null;

        return (
          <div key={type} className="mt-4">
            <div className="px-3 mb-1">
              <span className="text-xs font-semibold text-navy-500 uppercase tracking-wider">
                {label}
              </span>
            </div>

            <ul className="space-y-0.5 px-2">
              {group.map((channel) => {
                const isActive = channel.id === activeChannelId;

                return (
                  <li key={channel.id}>
                    <button
                      type="button"
                      onClick={() => onSelectChannel?.(channel)}
                      className={[
                        'w-full flex items-center gap-2 px-2 py-1.5 rounded text-sm text-left transition-colors',
                        isActive
                          ? 'bg-gold-500/20 text-gold-400 border border-gold-500/30'
                          : 'text-navy-300 hover:bg-navy-800 hover:text-sky-300',
                      ].join(' ')}
                    >
                      <Icon className="h-4 w-4 shrink-0" />
                      <span className="truncate flex-1">{channel.name}</span>
                      {type === 'voice' && channel.participantCount > 0 && (
                        <span className="text-xs text-navy-400 shrink-0">
                          {channel.participantCount}
                        </span>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </div>
  );
}
