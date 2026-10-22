/**
 * Community Chat Page
 * Full chat interface with channels, messages, and input
 */
import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeftIcon } from '@heroicons/react/24/outline';
import { useSocket } from '../../contexts/SocketContext';
import api from '../../services/api';
import ChatChannelList from '../../components/chat/ChatChannelList';
import ChatWindow from '../../components/chat/ChatWindow';
import ChatInput from '../../components/chat/ChatInput';

export default function CommunityChat() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { socket, connected } = useSocket();

  const [channels, setChannels] = useState([]);
  const [activeChannel, setActiveChannel] = useState('general');
  const [messages, setMessages] = useState([]);
  const [typingUsers, setTypingUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Load channels on mount
  useEffect(() => {
    loadChannels();
  }, [id]);

  // Join channel when active channel changes
  useEffect(() => {
    if (!socket || !connected) return;

    // Join the new channel
    socket.emit('chat:join', {
      communityId: parseInt(id),
      channelName: activeChannel,
    });

    // Load message history
    loadMessages();

    // Cleanup: leave channel on unmount or channel change
    return () => {
      socket.emit('chat:leave', {
        communityId: parseInt(id),
        channelName: activeChannel,
      });
    };
  }, [socket, connected, id, activeChannel]);

  // Setup socket event listeners
  useEffect(() => {
    if (!socket) return;

    // New message received
    const handleMessage = (message) => {
      setMessages((prev) => [...prev, message]);
    };

    // Chat history received
    const handleHistory = (data) => {
      setMessages(data.messages);
      setLoading(false);
    };

    // Typing indicator
    const handleTyping = (data) => {
      if (data.isTyping) {
        setTypingUsers((prev) => {
          if (prev.find((u) => u.userId === data.userId)) return prev;
          return [...prev, { userId: data.userId, username: data.username }];
        });
      } else {
        setTypingUsers((prev) => prev.filter((u) => u.userId !== data.userId));
      }

      // Auto-clear typing indicator after 3 seconds
      setTimeout(() => {
        setTypingUsers((prev) => prev.filter((u) => u.userId !== data.userId));
      }, 3000);
    };

    // User joined/left
    const handleUserJoined = (data) => {
      console.log('User joined:', data.username);
    };

    const handleUserLeft = (data) => {
      console.log('User left:', data.username);
    };

    // Error handling
    const handleError = (data) => {
      console.error('Chat error:', data.error);
      setError(data.error);
    };

    // Register listeners
    socket.on('chat:message', handleMessage);
    socket.on('chat:history', handleHistory);
    socket.on('chat:typing', handleTyping);
    socket.on('chat:user-joined', handleUserJoined);
    socket.on('chat:user-left', handleUserLeft);
    socket.on('chat:error', handleError);

    // Cleanup
    return () => {
      socket.off('chat:message', handleMessage);
      socket.off('chat:history', handleHistory);
      socket.off('chat:typing', handleTyping);
      socket.off('chat:user-joined', handleUserJoined);
      socket.off('chat:user-left', handleUserLeft);
      socket.off('chat:error', handleError);
    };
  }, [socket]);

  // Load available channels
  const loadChannels = async () => {
    try {
      const response = await api.get(`/api/v1/community/${id}/chat/channels`);
      if (response.data.success) {
        setChannels(response.data.data.channels);
      }
    } catch (err) {
      console.error('Failed to load channels:', err);
      setError('Failed to load channels');
    }
  };

  // Load message history
  const loadMessages = () => {
    if (!socket) return;

    setLoading(true);
    socket.emit('chat:history', {
      communityId: parseInt(id),
      channelName: activeChannel,
      limit: 50,
    });
  };

  // Send message
  const handleSendMessage = useCallback(
    (content) => {
      if (!socket || !connected) {
        setError('Not connected to chat server');
        return;
      }

      socket.emit('chat:message', {
        communityId: parseInt(id),
        channelName: activeChannel,
        content,
        type: 'text',
      });
    },
    [socket, connected, id, activeChannel]
  );

  // Change channel
  const handleSelectChannel = (channelName) => {
    setActiveChannel(channelName);
    setMessages([]);
    setTypingUsers([]);
    setLoading(true);
  };

  return (
    <div className="min-h-screen bg-gray-900 flex flex-col">
      {/* Header */}
      <div className="bg-gray-800 border-b border-gray-700 px-6 py-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <button
              aria-label="Back"
              onClick={() => navigate(`/dashboard/community/${id}`)}
              className="text-gray-400 hover:text-white transition-colors"
            >
              <ArrowLeftIcon className="w-6 h-6" />
            </button>
            <div>
              <h1 className="text-2xl font-bold text-white">Community Chat</h1>
              <p className="text-sm text-gray-400">
                #{activeChannel}
                {!connected && <span className="text-red-400 ml-2">(Disconnected)</span>}
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="bg-red-900/30 border-b border-red-800 px-6 py-3 text-red-300">
          {error}
        </div>
      )}

      {/* Main chat interface */}
      <div className="flex-1 flex overflow-hidden">
        {/* Channel sidebar */}
        <ChatChannelList
          channels={channels}
          activeChannel={activeChannel}
          onSelectChannel={handleSelectChannel}
        />

        {/* Chat area */}
        <div className="flex-1 flex flex-col">
          {/* Messages */}
          <ChatWindow messages={messages} typingUsers={typingUsers} loading={loading} />

          {/* Input */}
          <ChatInput onSendMessage={handleSendMessage} disabled={!connected} />
        </div>
      </div>
    </div>
  );
}
