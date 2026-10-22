import { useState, useEffect, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import {
  PhoneIcon,
  PlusIcon,
  UserGroupIcon,
  HandRaisedIcon,
  MicrophoneIcon,
  LockClosedIcon,
  LockOpenIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

function AdminCommunityCalls() {
  const { communityId } = useParams();
  const [rooms, setRooms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedRoom, setSelectedRoom] = useState(null);

  useEffect(() => {
    loadRooms();
  }, [communityId]);

  const loadRooms = async () => {
    try {
      setLoading(true);
      const response = await adminApi.getCallRooms(communityId);
      setRooms(response.data?.rooms || []);
    } catch (err) {
      setError('Failed to load call rooms');
    } finally {
      setLoading(false);
    }
  };

  const createRoom = async (data) => {
    try {
      await adminApi.createCallRoom(communityId, {
        room_name: data.room_name?.trim(),
        max_participants: data.max_participants,
      });
      setMessage({ type: 'success', text: 'Room created' });
      setShowCreateModal(false);
      loadRooms();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create room');
      throw err;
    }
  };

  // Build fields for FormModalBuilder
  const roomFields = useMemo(() => [
    {
      name: 'room_name',
      type: 'text',
      label: 'Room Name',
      required: true,
      placeholder: 'Weekly Community Call',
    },
    {
      name: 'max_participants',
      type: 'number',
      label: 'Max Participants',
      required: true,
      defaultValue: 100,
      min: 2,
      max: 1000,
      helpText: 'Minimum 2, maximum 1000 participants',
    },
  ], []);

  const deleteRoom = async (roomName) => {
    if (!window.confirm('Delete this room?')) return;
    try {
      await adminApi.deleteCallRoom(communityId, roomName);
      setMessage({ type: 'success', text: 'Room deleted' });
      loadRooms();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to delete room');
    }
  };

  const toggleLock = async (roomName, isLocked) => {
    try {
      if (isLocked) {
        await adminApi.unlockCallRoom(communityId, roomName);
      } else {
        await adminApi.lockCallRoom(communityId, roomName);
      }
      setMessage({ type: 'success', text: `Room ${isLocked ? 'unlocked' : 'locked'}` });
      loadRooms();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to toggle lock');
    }
  };

  const kickParticipant = async (roomName, userId) => {
    try {
      await adminApi.kickCallParticipant(communityId, roomName, userId);
      setMessage({ type: 'success', text: 'Participant removed' });
      if (selectedRoom?.room_name === roomName) {
        loadRoomDetails(roomName);
      }
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to kick participant');
    }
  };

  const muteAll = async (roomName) => {
    try {
      await adminApi.muteAllCallParticipants(communityId, roomName);
      setMessage({ type: 'success', text: 'All participants muted' });
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to mute all');
    }
  };

  const loadRoomDetails = async (roomName) => {
    try {
      const [roomRes, participantsRes, handsRes] = await Promise.all([
        adminApi.getCallRoom(communityId, roomName),
        adminApi.getCallParticipants(communityId, roomName),
        adminApi.getRaisedHands(communityId, roomName),
      ]);
      setSelectedRoom({
        ...roomRes.data,
        participants: participantsRes.data?.participants || [],
        raised_hands: handsRes.data?.raised_hands || [],
      });
    } catch (err) {
      setError('Failed to load room details');
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center items-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-sky-500"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white flex items-center gap-2">
          <PhoneIcon className="h-8 w-8 text-sky-500" />
          Community Calls
        </h1>
        <button onClick={() => setShowCreateModal(true)} className="btn btn-primary">
          <PlusIcon className="h-5 w-5 mr-2" />
          Create Room
        </button>
      </div>

      {error && (
        <div className="bg-red-500/20 border border-red-500 text-red-300 px-4 py-3 rounded">
          {error}
          <button onClick={() => setError(null)} className="float-right">&times;</button>
        </div>
      )}

      {message && (
        <div className={`px-4 py-3 rounded ${message.type === 'success' ? 'bg-green-500/20 text-green-300' : 'bg-red-500/20 text-red-300'}`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">&times;</button>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Room List */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-white mb-4">Active Rooms</h2>
          {rooms.length === 0 ? (
            <p className="text-fg-muted text-center py-8">No active rooms</p>
          ) : (
            <div className="space-y-3">
              {rooms.map((room) => (
                <div
                  key={room.room_id}
                  data-click-delegate
                  className={`p-4 bg-surface-raised rounded-lg cursor-pointer hover:bg-navy-700 transition ${
                    selectedRoom?.room_name === room.room_name ? 'ring-2 ring-sky-500' : ''
                  }`}
                  onClick={() => loadRoomDetails(room.room_name)}
                >
                  <div className="flex justify-between items-start">
                    <div>
                      <h3 className="text-white font-medium"><button type="button" className="text-left hover:underline focus-visible:underline">{room.room_name}</button></h3>
                      <p className="text-fg-muted text-sm flex items-center gap-2 mt-1">
                        <UserGroupIcon className="h-4 w-4" />
                        {room.participants || 0} participants
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      {room.is_locked && (
                        <LockClosedIcon className="h-5 w-5 text-yellow-500" />
                      )}
                      <button
                        aria-label={room.is_locked ? `Unlock room ${room.room_name}` : `Lock room ${room.room_name}`}
                        onClick={(e) => { e.stopPropagation(); toggleLock(room.room_name, room.is_locked); }}
                        className="btn btn-sm btn-secondary"
                      >
                        {room.is_locked ? <LockOpenIcon className="h-4 w-4" /> : <LockClosedIcon className="h-4 w-4" />}
                      </button>
                      <button
                        aria-label={`Delete room ${room.room_name}`}
                        onClick={(e) => { e.stopPropagation(); deleteRoom(room.room_name); }}
                        className="btn btn-sm btn-danger"
                      >
                        <XMarkIcon className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Room Details */}
        <div className="card p-6">
          {selectedRoom ? (
            <>
              <div className="flex justify-between items-center mb-4">
                <h2 className="text-lg font-semibold text-white">{selectedRoom.room_name}</h2>
                <button onClick={() => muteAll(selectedRoom.room_name)} className="btn btn-sm btn-warning">
                  <MicrophoneIcon className="h-4 w-4 mr-1" />
                  Mute All
                </button>
              </div>

              {/* Raised Hands */}
              {selectedRoom.raised_hands?.length > 0 && (
                <div className="mb-4">
                  <h3 className="text-sm font-medium text-fg-muted mb-2 flex items-center gap-1">
                    <HandRaisedIcon className="h-4 w-4 text-yellow-500" />
                    Raised Hands ({selectedRoom.raised_hands.length})
                  </h3>
                  <div className="space-y-2">
                    {selectedRoom.raised_hands.map((hand) => (
                      <div key={hand.user_id} className="flex items-center justify-between p-2 bg-yellow-500/10 rounded">
                        <span className="text-white text-sm">{hand.user_name}</span>
                        <button
                          onClick={() => adminApi.acknowledgeHand(communityId, selectedRoom.room_name, hand.user_id)}
                          className="btn btn-xs btn-primary"
                        >
                          Acknowledge
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Participants */}
              <h3 className="text-sm font-medium text-fg-muted mb-2">
                Participants ({selectedRoom.participants?.length || 0})
              </h3>
              <div className="space-y-2 max-h-64 overflow-y-auto">
                {selectedRoom.participants?.map((p) => (
                  <div key={p.user_id} className="flex items-center justify-between p-2 bg-surface-raised rounded">
                    <div>
                      <span className="text-white text-sm">{p.identity}</span>
                      <span className={`ml-2 text-xs px-2 py-0.5 rounded ${
                        p.role === 'host' ? 'bg-purple-500/20 text-purple-400' :
                        p.role === 'moderator' ? 'bg-blue-500/20 text-blue-400' :
                        p.role === 'speaker' ? 'bg-green-500/20 text-green-400' :
                        'bg-navy-500/20 text-fg-muted'
                      }`}>
                        {p.role}
                      </span>
                    </div>
                    <button
                      onClick={() => kickParticipant(selectedRoom.room_name, p.identity)}
                      className="btn btn-xs btn-danger"
                    >
                      Kick
                    </button>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="text-fg-muted text-center py-8">Select a room to view details</p>
          )}
        </div>
      </div>

      {/* Create Room Modal */}
      <FormModalBuilder
        title="Create Call Room"
        fields={roomFields}
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={createRoom}
        submitButtonText="Create"
        cancelButtonText="Cancel"
        width="md"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />
    </div>
  );
}

export default AdminCommunityCalls;
