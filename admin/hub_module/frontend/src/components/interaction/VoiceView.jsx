import { useState, useEffect, useCallback } from 'react';
import {
  SpeakerWaveIcon,
  MicrophoneIcon,
  PhoneXMarkIcon,
  UserGroupIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';
import { interactionApi } from '../../services/api';

// NOTE: Actual LiveKit integration requires `livekit-client` package.
// This component provides the full UI structure. To complete integration:
//   npm install livekit-client
// Then use Room, connect(), LocalParticipant, RemoteParticipant from 'livekit-client'.

function CreateRoomModal({ onClose, onSubmit }) {
  const [roomName, setRoomName] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!roomName.trim()) return;
    setSubmitting(true);
    await onSubmit(roomName.trim());
    setSubmitting(false);
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div role="dialog" aria-modal="true" aria-labelledby="voice-view-dialog-1-title" className="bg-navy-800 border border-navy-700 rounded-xl p-6 w-full max-w-md shadow-xl">
        <h3 id="voice-view-dialog-1-title" className="text-sky-100 font-semibold text-lg mb-4">Create Ad-Hoc Voice Room</h3>
        <form onSubmit={handleSubmit}>
          <label className="block text-navy-300 text-sm mb-1" htmlFor="room-name">
            Room Name
          </label>
          <input
            id="room-name"
            type="text"
            value={roomName}
            onChange={(e) => setRoomName(e.target.value)}
            placeholder="e.g. hangout-1"
            className="w-full bg-navy-700 border border-navy-600 rounded-lg px-3 py-2 text-sky-100 placeholder-navy-400 focus:border-gold-500 mb-4"
            maxLength={64}
            autoFocus
          />
          <div className="flex justify-end gap-3">
            <button
              type="button"
              onClick={onClose}
              className="bg-navy-700 text-sky-300 hover:bg-navy-600 border border-navy-600 rounded-lg px-4 py-2 text-sm"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!roomName.trim() || submitting}
              className="bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? 'Creating...' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function RoomCard({ room, onJoin, joining }) {
  return (
    <div className="bg-navy-800 border border-navy-700 rounded-lg p-4 flex items-center justify-between">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 text-sky-400">
          <SpeakerWaveIcon className="w-5 h-5" />
        </div>
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sky-100 font-medium">{room.name}</span>
            {room.isAdHoc && (
              <span className="text-xs bg-navy-700 text-navy-300 border border-navy-600 rounded px-1.5 py-0.5">
                ad-hoc
              </span>
            )}
          </div>
          <div className="flex items-center gap-1 mt-1">
            <UserGroupIcon className="w-3.5 h-3.5 text-navy-400" />
            <span className="text-navy-400 text-sm">
              {room.participantCount ?? 0}{' '}
              {room.participantCount === 1 ? 'participant' : 'participants'}
            </span>
          </div>
          {room.metadata && (
            <p className="text-navy-400 text-xs mt-1 truncate max-w-xs">{room.metadata}</p>
          )}
        </div>
      </div>
      <button
        onClick={() => onJoin(room.name)}
        disabled={joining}
        className="bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
      >
        {joining ? 'Joining...' : 'Join'}
      </button>
    </div>
  );
}

function ParticipantCard({ username }) {
  return (
    <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 flex flex-col items-center justify-center gap-2 min-h-[120px]">
      <div className="w-12 h-12 rounded-full bg-navy-700 border border-navy-600 flex items-center justify-center">
        <span className="text-sky-200 font-semibold text-lg">
          {username ? username.charAt(0).toUpperCase() : '?'}
        </span>
      </div>
      <span className="text-sky-100 text-sm font-medium truncate max-w-[8rem]">
        {username || 'Unknown'}
      </span>
    </div>
  );
}

function InCallView({ room, token, url, onLeave }) {
  const [muted, setMuted] = useState(false);
  const [leavingCall, setLeavingCall] = useState(false);

  // Placeholder participants — real integration would populate from LiveKit room events
  const placeholderParticipants = [
    { id: 'local', username: 'You (local)' },
    ...(room.participantCount > 1
      ? Array.from({ length: room.participantCount - 1 }, (_, i) => ({
          id: `remote-${i}`,
          username: `Participant ${i + 2}`,
        }))
      : []),
  ];

  const handleLeave = async () => {
    setLeavingCall(true);
    await onLeave();
    setLeavingCall(false);
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-navy-700">
        <SpeakerWaveIcon className="w-5 h-5 text-gold-400" />
        <div>
          <span className="text-sky-100 font-semibold">{room.name}</span>
          {room.isAdHoc && (
            <span className="ml-2 text-xs bg-navy-700 text-navy-300 border border-navy-600 rounded px-1.5 py-0.5">
              ad-hoc
            </span>
          )}
        </div>
        <div className="ml-auto flex items-center gap-1 text-navy-400 text-sm">
          <UserGroupIcon className="w-4 h-4" />
          <span>{room.participantCount ?? 0}</span>
        </div>
      </div>

      {/* LiveKit placeholder notice */}
      <div className="mx-4 mt-4 bg-navy-700/50 border border-navy-600 rounded-lg px-4 py-3 text-navy-300 text-xs">
        <strong className="text-sky-300">LiveKit integration pending.</strong> Install{' '}
        <code className="bg-navy-900 rounded px-1">livekit-client</code> and connect using the
        token/URL provided by the server to enable real-time audio/video.
        {url && (
          <span className="block mt-1 truncate text-navy-400">Server: {url}</span>
        )}
      </div>

      {/* Participant grid */}
      <div className="flex-1 overflow-y-auto p-4">
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
          {placeholderParticipants.map((p) => (
            <ParticipantCard key={p.id} username={p.username} />
          ))}
        </div>
      </div>

      {/* Control bar */}
      <div className="bg-navy-800 border-t border-navy-700 p-4 flex justify-center gap-4">
        <button
          onClick={() => setMuted((m) => !m)}
          title={muted ? 'Unmute' : 'Mute'}
          className={`rounded-full p-3 transition-colors ${
            muted
              ? 'bg-red-700 text-white hover:bg-red-600'
              : 'bg-navy-700 text-sky-100 hover:bg-navy-600'
          }`}
        >
          <MicrophoneIcon className="w-5 h-5" />
        </button>
        <button
          onClick={handleLeave}
          disabled={leavingCall}
          title="Leave"
          className="bg-red-600 text-white hover:bg-red-500 rounded-full p-3 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <PhoneXMarkIcon className="w-5 h-5" />
        </button>
      </div>
    </div>
  );
}

export default function VoiceView({ channel }) {
  const [rooms, setRooms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [joiningRoom, setJoiningRoom] = useState(null);
  const [activeRoom, setActiveRoom] = useState(null);
  const [callToken, setCallToken] = useState(null);
  const [callUrl, setCallUrl] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);

  const fetchRooms = useCallback(async () => {
    if (!channel?.communityId) return;
    try {
      setError(null);
      const response = await interactionApi.getVoiceRooms(channel.communityId);
      setRooms(response?.data?.rooms ?? []);
    } catch (err) {
      setError(err?.message ?? 'Failed to load voice rooms.');
    } finally {
      setLoading(false);
    }
  }, [channel?.communityId]);

  useEffect(() => {
    setLoading(true);
    fetchRooms();
  }, [fetchRooms]);

  const handleJoin = async (roomName) => {
    setJoiningRoom(roomName);
    try {
      const response = await interactionApi.joinVoiceRoom(channel.communityId, roomName);
      const { token, url } = response?.data ?? {};
      const room = rooms.find((r) => r.name === roomName) ?? { name: roomName, participantCount: 1 };
      setCallToken(token);
      setCallUrl(url);
      setActiveRoom(room);
    } catch (err) {
      setError(err?.message ?? 'Failed to join room.');
    } finally {
      setJoiningRoom(null);
    }
  };

  const handleLeave = async () => {
    if (!activeRoom) return;
    try {
      await interactionApi.leaveVoiceRoom(channel.communityId, activeRoom.name);
    } catch {
      // Best-effort leave
    } finally {
      setActiveRoom(null);
      setCallToken(null);
      setCallUrl(null);
      fetchRooms();
    }
  };

  const handleCreateRoom = async (roomName) => {
    try {
      await interactionApi.createAdHocVoiceRoom(channel.communityId, { name: roomName });
      setShowCreateModal(false);
      await fetchRooms();
    } catch (err) {
      setError(err?.message ?? 'Failed to create room.');
      setShowCreateModal(false);
    }
  };

  if (activeRoom) {
    return (
      <div className="flex flex-col h-full">
        <InCallView
          room={activeRoom}
          token={callToken}
          url={callUrl}
          onLeave={handleLeave}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-navy-700">
        <div className="flex items-center gap-2">
          <SpeakerWaveIcon className="w-5 h-5 text-gold-400" />
          <h2 className="text-sky-100 font-semibold">{channel?.name ?? 'Voice'}</h2>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchRooms}
            disabled={loading}
            className="text-navy-400 hover:text-sky-300 text-xs px-2 py-1 rounded transition-colors disabled:opacity-50"
          >
            Refresh
          </button>
          {channel?.allowAdHocVoice && (
            <button
              onClick={() => setShowCreateModal(true)}
              className="bg-navy-700 text-sky-300 hover:bg-navy-600 border border-navy-600 rounded-lg px-4 py-2 text-sm flex items-center gap-1.5 transition-colors"
            >
              <PlusIcon className="w-4 h-4" />
              Create Room
            </button>
          )}
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4">
        {error && (
          <div className="bg-red-900/30 border border-red-700 text-red-300 rounded-lg px-4 py-3 mb-4 text-sm">
            {error}
          </div>
        )}

        {loading ? (
          <div className="flex flex-col gap-3">
            {[1, 2, 3].map((i) => (
              <div
                key={i}
                className="bg-navy-800 border border-navy-700 rounded-lg p-4 animate-pulse h-16"
              />
            ))}
          </div>
        ) : rooms.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <SpeakerWaveIcon className="w-10 h-10 text-navy-600 mb-3" />
            <p className="text-navy-400 text-sm">No voice rooms available.</p>
            {channel?.allowAdHocVoice && (
              <p className="text-navy-500 text-xs mt-1">
                Create an ad-hoc room to start a conversation.
              </p>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {rooms.map((room) => (
              <RoomCard
                key={room.name}
                room={room}
                onJoin={handleJoin}
                joining={joiningRoom === room.name}
              />
            ))}
          </div>
        )}
      </div>

      {showCreateModal && (
        <CreateRoomModal
          onClose={() => setShowCreateModal(false)}
          onSubmit={handleCreateRoom}
        />
      )}
    </div>
  );
}
