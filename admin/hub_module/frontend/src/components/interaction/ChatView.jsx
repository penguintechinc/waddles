import { useState, useEffect, useRef } from 'react';
import { HashtagIcon, PaperAirplaneIcon } from '@heroicons/react/24/outline';

export default function ChatView({ channel, socket }) {
  const [messages, setMessages] = useState([]);
  const [inputValue, setInputValue] = useState('');
  const messagesEndRef = useRef(null);
  const channelName = channel ? 'hub-channel-' + channel.id : null;

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    if (!socket || !channel) return;

    const roomName = 'hub-channel-' + channel.id;

    socket.emit('chat:join', {
      communityId: channel.communityId,
      channelName: roomName,
    });

    socket.emit('chat:history', {
      communityId: channel.communityId,
      channelName: roomName,
    });

    const handleMessage = (msg) => {
      if (msg.channelName === roomName) {
        setMessages((prev) => [...prev, msg]);
      }
    };

    const handleBridgedMessage = (msg) => {
      if (msg.channelName === roomName) {
        setMessages((prev) => [...prev, { ...msg, bridged: true }]);
      }
    };

    const handleHistory = (data) => {
      if (data.channelName === roomName) {
        setMessages(data.messages || []);
      }
    };

    socket.on('chat:message', handleMessage);
    socket.on('chat:bridged-message', handleBridgedMessage);
    socket.on('chat:history', handleHistory);

    return () => {
      socket.emit('chat:leave', {
        communityId: channel.communityId,
        channelName: roomName,
      });
      socket.off('chat:message', handleMessage);
      socket.off('chat:bridged-message', handleBridgedMessage);
      socket.off('chat:history', handleHistory);
      setMessages([]);
    };
  }, [socket, channel?.id, channel?.communityId]);

  const handleSend = () => {
    const trimmed = inputValue.trim();
    if (!trimmed || !socket || !channel) return;

    socket.emit('chat:message', {
      communityId: channel.communityId,
      channelName: 'hub-channel-' + channel.id,
      content: trimmed,
    });

    setInputValue('');
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const formatTimestamp = (ts) => {
    if (!ts) return '';
    const date = new Date(ts);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  const getInitial = (username) => {
    return username ? username.charAt(0).toUpperCase() : '?';
  };

  if (!channel) {
    return (
      <div className="flex flex-1 items-center justify-center text-navy-400">
        Select a channel to start chatting
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-3 border-b border-navy-700 bg-navy-900">
        <HashtagIcon className="w-5 h-5 text-gold-400 flex-shrink-0" />
        <div>
          <span className="font-semibold text-sky-100">{channel.name}</span>
          {channel.description && (
            <p className="text-xs text-navy-400 mt-0.5">{channel.description}</p>
          )}
        </div>
      </div>

      {/* Messages area */}
      <div className="bg-navy-950 flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 && (
          <div className="text-center text-navy-500 text-sm mt-8">
            No messages yet. Be the first to say something!
          </div>
        )}
        {messages.map((msg, index) => (
          <div key={msg.id || index} className="flex items-start gap-3">
            {/* Avatar */}
            <div className="flex-shrink-0">
              {msg.senderAvatarUrl ? (
                <img
                  src={msg.senderAvatarUrl}
                  alt={msg.senderUsername}
                  className="w-8 h-8 rounded-full object-cover"
                />
              ) : (
                <div className="w-8 h-8 rounded-full bg-navy-700 flex items-center justify-center text-sky-300 text-sm font-semibold">
                  {getInitial(msg.senderUsername)}
                </div>
              )}
            </div>

            {/* Message content */}
            <div className="flex-1 min-w-0">
              <div className="flex items-baseline gap-2 flex-wrap">
                <span className="text-sm font-semibold text-sky-300">
                  {msg.senderUsername || 'Unknown'}
                </span>
                {(msg.bridged || msg.senderPlatform) && msg.senderPlatform !== 'hub' && (
                  <span className="text-xs text-navy-400">
                    via {msg.senderPlatform
                      ? msg.senderPlatform.charAt(0).toUpperCase() + msg.senderPlatform.slice(1)
                      : 'External'}
                  </span>
                )}
                <span className="text-xs text-navy-500">
                  {formatTimestamp(msg.createdAt)}
                </span>
              </div>
              <p className="text-sm text-navy-200 break-words mt-0.5">
                {msg.content}
              </p>
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <div className="px-4 py-3 bg-navy-900 border-t border-navy-700">
        <div className="flex items-center gap-2">
          <input
            aria-label={`Message #${channel.name}`}
            type="text"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`Message #${channel.name}`}
            className="flex-1 bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm placeholder-navy-500 focus:outline-none focus:border-gold-500 focus:ring-1 focus:ring-gold-500"
          />
          <button
            aria-label="Send message"
            onClick={handleSend}
            disabled={!inputValue.trim()}
            className="bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-4 py-2 flex items-center gap-1.5 text-sm font-semibold transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <PaperAirplaneIcon className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
